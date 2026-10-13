from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
import socket
import time
import uuid

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import CodexCliHarness
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.runtime_admission import RuntimeCapacityUnavailable
from assistant_kernel.services.recovery import RecoveryService
from assistant_kernel.services.run_processor import RunProcessor
from assistant_kernel.services.incidents import IncidentService
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.reconciliation import ReconciliationService
from assistant_kernel.services.supervisor_processor import SupervisorProcessor
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry
from assistant_kernel.services.channel_health import WorkerHealthReporter, now_iso
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker.process_control import (
    process_start_ticks,
    terminate_recorded_process_group,
)
from assistant_kernel.worker.python_process import (
    cleanup_python_group, python_process_identity, wait_python_group,
)


async def _resume_operation_parent(run: dict, repository: CoreRepository) -> None:
    if (run.get("request_json") or {}).get("operation_action"):
        try:
            await repository.resume_completed_operation(str(run["run_id"]))
        except Exception:
            logging.exception("Managed operation settled; parent receipt handoff remains pending")


async def _notify_terminal_failure(
    repository: CoreRepository, run: dict, settings: KernelSettings | None = None,
    error: Exception | None = None,
) -> None:
    """Queue one user-safe terminal failure notice on the triggering channel."""
    latest_reader = getattr(repository, "get_run", None)
    latest = await latest_reader(str(run["run_id"])) if callable(latest_reader) else None
    run = latest or run
    request = dict(run.get("request_json") or {})
    await _resume_operation_parent(run, repository)
    is_supervisor = request.get("interaction_kind") == "supervisor_consultation"
    if is_supervisor and request.get("task_id"):
        await repository.pause_supervision(
            task_id=str(request["task_id"]), turn_number=int(request.get("turn_number") or 0),
            reason="Supervisor Run exhausted bounded retries", retry_run_id=str(run["run_id"]),
        )
    elif request.get("task_protocol") == "single-agent.v2":
        await repository.settle_failed_pm_turn(
            task_id=str(run["run_id"]), reason="PM Run reached terminal failure",
            source_run_id=str(run["run_id"]),
        )
    trigger_message_id = run.get("trigger_message_id")
    if not trigger_message_id:
        return
    trigger = await repository.get_message(str(trigger_message_id))
    if trigger is None or str(trigger.get("channel")) not in {"feishu", "discord"}:
        return
    run_id = str(run["run_id"])
    request = dict(run.get("request_json") or {})
    is_supervisor = request.get("interaction_kind") == "supervisor_consultation"
    chat_id = str(trigger["chat_id"])
    delivery_route = "trigger_channel"
    if is_supervisor and settings is not None and str(trigger["channel"]) == "discord":
        try:
            interface = DiscordInterfaceRegistry(settings).find(
                project_id=str(run["project_id"]), role="supervisor"
            )
        except (OSError, ValueError):
            interface = None
        if interface is not None:
            chat_id = str(interface["channel_id"])
            delivery_route = "project_supervisor_interface"
    await repository.create_outbound(
        project_id=str(run["project_id"]),
        session_id=(str(run["session_id"]) if run.get("session_id") else None),
        run_id=run_id,
        channel=str(trigger["channel"]),
        chat_id=chat_id,
        user_id=(str(trigger["user_id"]) if trigger.get("user_id") else None),
        message_type="reply",
        content={
            "text": (
                "这次处理连续失败，已停止自动重试，尚未完成你的请求。\n"
                f"Run ID：{run_id}\n"
                "主线未放行。请使用监督恢复卡重新评估，或查看并核对该 Run。"
            ),
            "terminal_status": "dead_letter",
            "interaction_kind": (
                "supervisor_failure" if is_supervisor else "run_failure"
            ),
            "failure_step": (
                str(getattr(error, "step", "main_evaluation"))
                if is_supervisor else "run_processing"
            ),
            "error_type": type(error).__name__ if error is not None else "RunFailure",
            "delivery_route": delivery_route,
            **({"interaction_kind": "supervision_recovery", "task_id": None,
                "action_status": "waiting",
                "supervisor_run_id": run_id, "turn_number": 0, "phase": "consultation",
                "context_epoch": 0, "cancel_only_mainline": False,
                "actions": [{"id": "retry", "label": "重试监督"},
                            {"id": "wait", "label": "暂时等待"},
                            {"id": "cancel", "label": "取消咨询"}]}
               if is_supervisor and not request.get("task_id") else {}),
        },
        reply_to_message_id=(
            str(trigger["message_id"])
            if delivery_route == "trigger_channel" else None
        ),
        idempotency_key=f"dead-letter-notice:{run_id}",
    )


async def _heartbeat(
    repository: CoreRepository, run_id: str, worker_instance_id: str,
    claim_token: str, lease_seconds: int, cancel_event: asyncio.Event,
    health: WorkerHealthReporter | None = None,
) -> None:
    interval = max(5.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        try:
            state = await repository.heartbeat_run(
                run_id, worker_instance_id, claim_token, lease_seconds
            )
            if health is not None:
                health.update(
                    last_process_heartbeat_at=now_iso(),
                    active_run_id=run_id,
                    last_error_type=None,
                )
        except Exception:
            logging.exception("run heartbeat failed; stopping the active process")
            cancel_event.set()
            return
        if state != "active":
            cancel_event.set()
            return


async def execute_python(
    run: dict, settings: KernelSettings, repository: CoreRepository,
    worker_instance_id: str, cancel_event: asyncio.Event,
) -> None:
    project_id = str(run["project_id"])
    claim_token = str(run["claim_token"])
    runtime_manager = ProjectRuntimeManager(settings.data_root)
    runtime = runtime_manager.ensure(project_id)
    request = run["request_json"] or {}
    if request.get("operation_action"):
        from assistant_kernel.services.operations import ProjectOperations
        await ProjectOperations(settings, repository).validate_dispatch(run)
    prefix = f"vault://{project_id}/"
    source_uri = str(request.get("source_uri") or "")
    fixed=request.get('python_protocol')=='fixed-manifest.v1'
    if not fixed and not source_uri.startswith(prefix):
        raise ValueError("python source_uri is outside its Project Vault")
    source = runtime_manager.resolve_vault_file(project_id, source_uri[len(prefix):]) if not fixed else None
    if not fixed and (not source.is_file() or source.suffix.lower() != ".py"):
        raise ValueError("python source is not an existing .py file")

    attempt = int(run["attempt"])
    if attempt < 1:
        raise ValueError("Python execution requires a claimed attempt")
    output_root = runtime.vault_root / "runs" / str(run["run_id"]) / "attempts" / str(attempt)
    # A repeated invocation of the same claim must never overwrite its evidence.
    output_root.mkdir(parents=True, exist_ok=False)
    stdout_path = output_root / "stdout.log"
    stderr_path = output_root / "stderr.log"
    result_path = output_root / "result.json"
    input_path = output_root / "input.json"
    from assistant_kernel.services.contracts import canonical_json
    input_path.write_bytes(canonical_json(request.get('input',{})))
    receipt_path = output_root / "execution.json"
    uri_root = f"vault://{project_id}/runs/{run['run_id']}/attempts/{attempt}"
    receipt = {
        "schema_version": "python-attempt.v1", "run_id": str(run["run_id"]),
        "project_id": project_id, "attempt": attempt, "started_at": now_iso(),
        "ended_at": None, "exit_code": None, "outcome": "running",
        "stop_reason": None, "result_present": False, "process_identity": None,
        "cleanup": {"status": "unknown", "reason": "launch not yet settled"},
    }

    def save_receipt() -> list[str]:
        pending = receipt_path.with_suffix(".tmp")
        pending.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        pending.replace(receipt_path)
        return [f"{uri_root}/{name}" for name in
                ("stdout.log", "stderr.log", "result.json", "execution.json")
                if (output_root / name).is_file()]

    from assistant_kernel.worker.python_environment import python_environment
    environment = python_environment(run,output_root)
    environment.update(
        {
            "ASSISTANT_RUN_ID": str(run["run_id"]),
            "ASSISTANT_PROJECT_ID": project_id,
            "ASSISTANT_ATTEMPT": str(attempt),
            "ASSISTANT_OUTPUT_DIR": str(output_root),
            "ASSISTANT_RESULT_PATH": str(result_path),
            "ASSISTANT_INPUT_PATH": str(input_path),
        }
    )
    args = [str(item) for item in request.get("args", [])]
    timeout_seconds = min(max(int(request.get("timeout_seconds", 300)), 1), 3600)
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    result = {}
    failure: BaseException | None = None
    prepared=None;executable=settings.python_executable;working_directory=runtime.vault_root
    evidence=None
    if fixed:
        from assistant_kernel.services.python_evidence import PythonEvidence
        evidence=PythonEvidence(settings,run)
    try:
        if fixed:
            from assistant_kernel.services.project_python import ProjectPython
            prepared=await ProjectPython(settings,repository).validate_dispatch(run)
            source=prepared['source'];executable=prepared['interpreter'];working_directory=prepared['cwd'];args=prepared['args']
            result_path=prepared['business_output_root']/'result.json'
            environment.update(ASSISTANT_OUTPUT_DIR=str(prepared['business_output_root']),ASSISTANT_RESULT_PATH=str(result_path))
            receipt.update(manifest_ref=request['manifest_ref'],logical_execution_id=request['logical_execution_id'],
                result_path=str(result_path),contract_status='unverified',business_status='unverified',effect_status='unknown')
            from assistant_kernel.services.business_runtime import BusinessRuntime
            client_path=await BusinessRuntime(settings,repository).assemble(run,prepared)
            if client_path:environment['ASSISTANT_RUNTIME_CLIENT_PATH']=str(client_path)
            receipt['binding_ref']=evidence.binding(prepared)
            from assistant_kernel.worker.python_environment import business_credentials
            secrets,bindings=business_credentials(settings.data_root,project_id,prepared['manifest']['io']['secret_refs'])
            environment.update(secrets)
            receipt['credential_bindings']=bindings
        elif request.get('source_sha256'):
            import hashlib
            if hashlib.sha256(source.read_bytes()).hexdigest()!=request['source_sha256']:
                raise ValueError('Legacy auxiliary source changed after its dispatch binding')
        if not await repository.record_python_attempt(
            str(run["run_id"]), worker_instance_id, claim_token, receipt, save_receipt(),
        ):
            raise asyncio.CancelledError("run lease was lost before Python launch")
        if cancel_event.is_set():
            raise asyncio.CancelledError("Python stop was requested before launch")
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                str(executable), str(source), *args,
                cwd=str(working_directory), env=environment,
                stdout=stdout, stderr=stderr, start_new_session=True,
            )
            receipt["process_identity"] = python_process_identity(process.pid, str(run["run_id"]), attempt)
            if not await repository.record_python_attempt(
                str(run["run_id"]), worker_instance_id, claim_token, receipt, save_receipt(),
            ):
                raise asyncio.CancelledError("run lease was lost as Python started")
            while process.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                if prepared and request.get('lifecycle_relation')=='invocation_dependent':
                    async with repository.database.connection() as connection:
                        active=await (await connection.execute("""SELECT 1 FROM assistant.model_invocations i
                            JOIN assistant.runs p ON p.run_id=i.run_id WHERE i.invocation_id=%s AND i.status='running'
                            AND p.status='running' AND p.cancel_requested_at IS NULL""",
                            (request['creating_invocation_id'],))).fetchone()
                    if not active:cancel_event.set()
                if cancel_event.is_set():
                    raise asyncio.CancelledError("Python stop was requested")
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError(f"python execution exceeded {timeout_seconds} seconds")
                if prepared and stdout_path.stat().st_size+stderr_path.stat().st_size>prepared['resources']['log_limit_bytes']:
                    raise RuntimeError('Python captured logs exceed the declared limit')
        if cancel_event.is_set():
            raise asyncio.CancelledError("Python stop was requested")
        if process.returncode != 0:
            raise RuntimeError(f"python script exited with code {process.returncode}")
        if (await wait_python_group(receipt["process_identity"], 1))["status"] != "verified":
            raise RuntimeError("Python parent exited without verified child cleanup")
        # Missing JSON remains compatible with legacy auxiliary scripts. Never fall back.
        if prepared:
            receipt['contract_status']='failed'  # Only the checks below can pass it.
            if result_path.is_file() and result_path.stat().st_size>prepared['resources']['max_output_bytes']:
                raise ValueError('Python result exceeds its declared limit')
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
        current=await repository.get_run(str(run['run_id'])) if prepared and prepared.get('definition') else None
        pending=(current['executor_json'] or {}).get('runtime_wait') if current else None
        if pending:
            if result.get('status')!='needs_input':raise ValueError('Registered graph wait needs its explicit needs_input result')
            receipt.update(contract_status='passed',business_status='unverified',effect_status='unknown',runtime_wait=pending)
        elif prepared:
            if not result_path.is_file():raise ValueError('Fixed Python requires this attempt result.json')
            if stdout_path.stat().st_size+stderr_path.stat().st_size>prepared['resources']['log_limit_bytes']:
                raise ValueError('Python captured logs exceed the declared limit')
            from assistant_kernel.services.fixed_python import FixedPython
            receipt.update(FixedPython(settings).check_outputs(prepared,result,prepared['business_output_root']))
            if receipt['business_status']=='failed':raise ValueError('Fixed Python business assertion failed')
    except (Exception, asyncio.CancelledError) as exc:
        failure = exc
    finally:
        cleanup = (await cleanup_python_group(receipt["process_identity"]) if process is not None
                   else {"status": "verified", "reason": "no process was launched"})
        if process is not None and cleanup["status"] == "verified":
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                cleanup = {"status": "unknown", "reason": "parent exit was not acknowledged"}
        receipt.update(
            ended_at=now_iso(), exit_code=process.returncode if process else None,
            result_present=result_path.is_file(), cleanup=cleanup,
            outcome=("reconciling" if cleanup["status"] != "verified" else
                     "cancelled" if isinstance(failure, asyncio.CancelledError) else
                     "failed" if failure else "succeeded"),
            stop_reason=type(failure).__name__ if failure else None,
        )
        if evidence:
            settlement,artifacts=evidence.settle(receipt,prepared)
            receipt.update(settlement_ref=settlement,output_artifact_refs=artifacts)
        refs = save_receipt()
        if evidence:
            refs += [receipt['settlement_ref'],*receipt['output_artifact_refs']]
        recorded = await repository.record_python_attempt(
            str(run["run_id"]), worker_instance_id, claim_token, receipt, refs,
        )
    if not recorded:
        raise asyncio.CancelledError("Python evidence kept locally; run lease was lost")
    if cleanup["status"] != "verified":
        # retry_run keeps a started Python Run in reconciliation, including cancellation.
        raise RuntimeError(f"Python cleanup is {cleanup['status']}: {cleanup['reason']}") from failure
    if failure is not None and not isinstance(failure, asyncio.CancelledError):
        raise failure
    cancelled = isinstance(failure, asyncio.CancelledError)
    if not await repository.finish_run(
        str(run["run_id"]), worker_instance_id, claim_token,
        status="cancelled" if cancelled else "waiting_user" if receipt.get('runtime_wait') else "succeeded",
        result=None if cancelled else result, artifact_refs=refs,
        error={"type": "Cancelled", "message": str(failure)} if cancelled else None,
    ):
        raise RuntimeError("python run lease was lost before completion")
    await _resume_operation_parent(run, repository)
    if receipt.get('runtime_wait'):
        from assistant_kernel.services.business_interactions import BusinessInteractions
        await BusinessInteractions(settings,repository).resume(str(run['run_id']))


async def _poll_or_shutdown(
    shutdown_event: asyncio.Event | None, timeout_seconds: float,
) -> None:
    """Wait for the next poll without delaying a graceful shutdown."""
    if shutdown_event is None:
        await asyncio.sleep(timeout_seconds)
        return
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass


async def run_maintenance_loop(
    settings: KernelSettings, shutdown_event: asyncio.Event | None = None,
) -> None:
    """Periodic maintenance: tiered recovery, quarantine, and cards.

    v6.1 order matters: expired runs are quarantined and their processes
    terminated first, then the recovery classifier sweeps every unresolved
    reconciling Run (covering both the lease-expiry and the exception-path
    entries), and only the remaining T3 Runs reach the card queue.
    """
    repository = CoreRepository(settings)
    reconciliations = ReconciliationService(
        settings, CoreRepository(settings)
    )
    recovery = RecoveryService(settings, repository)
    incidents = IncidentService(settings)
    from assistant_kernel.services.config_consumers import ConfigConsumers
    configuration=ConfigConsumers(settings)
    while shutdown_event is None or not shutdown_event.is_set():
        if not await repository.database.ping():
            await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
            continue
        try:
            expired = await repository.quarantine_expired_runs()
            await configuration.apply_dynamic(settings,'maintenance')
            await configuration.reconcile()
            for abandoned in expired:
                python_attempt = (abandoned.get("executor_json") or {}).get("python_attempt")
                if abandoned.get("run_type") == "python" and python_attempt:
                    cleanup = python_attempt.get("cleanup") or {}
                    if cleanup.get("status") != "verified":
                        cleanup = await cleanup_python_group(python_attempt.get("process_identity"))
                        await repository.record_python_cleanup(
                            str(abandoned["run_id"]), int(abandoned["attempt"]), cleanup,
                        )
                    continue
                await terminate_recorded_process_group(
                    abandoned.get("process_pid"), abandoned.get("process_start_ticks")
                )
            for decision in await recovery.sweep():
                if decision.get("notify"):
                    dead = await repository.get_run(str(decision["run_id"]))
                    if dead is not None:
                        incidents.record(
                            project_id=str(dead["project_id"]),
                            incident_key=f"reconciliation:{dead['run_id']}",
                            severity="high",
                            summary=str(decision.get("reason") or "Run requires reconciliation"),
                            evidence_refs=[{
                                "kind": "run", "run_id": str(dead["run_id"]),
                                "status": str(dead["status"]),
                            }],
                            source_run_id=str(dead["run_id"]),
                        )
                        await _notify_terminal_failure(repository, dead)
            try:
                await repository.reconcile_pending_supervision()
            except Exception:
                logging.exception("Pending supervision recovery remains deferred")
            try:
                await _reconcile_project_changes(settings, repository)
            except Exception:
                logging.exception("Project evidence/queue maintenance remains deferred")
            await reconciliations.notify_due()
            from assistant_kernel.services.runtime_notifications import RuntimeNotifications
            await RuntimeNotifications(settings,repository).route_pending()
            from assistant_kernel.services.business_interactions import BusinessInteractions
            await BusinessInteractions(settings,repository).recover_answered()
            from assistant_kernel.services.service_runtime import ServiceRuntime
            await ServiceRuntime(settings,repository).reconcile()
            from assistant_kernel.services.operation_records import OperationRecords
            await OperationRecords(settings,repository).route_pending()
        except Exception:
            logging.exception("run maintenance failed")
        if shutdown_event is not None and shutdown_event.is_set():
            break
        await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
    logging.info("run maintenance loop drained and stopped")


async def _reconcile_project_changes(settings: KernelSettings, repository: CoreRepository) -> None:
    """Bounded evidence/Map repair; publication and model business code never run."""
    from assistant_kernel.services.project_changes import ProjectChangeService
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    from assistant_kernel.services.document_runtime import resource_ref
    from psycopg.types.json import Jsonb
    changes = ProjectChangeService(settings, repository.database)
    scopes = WorkspaceScopeService(settings)
    # Accepted semantic revisions can commit without a Publisher sidecar.
    # Repair only the derived generation from the committed ledger.
    from assistant_kernel.repositories import change_ledger
    async with repository.database.connection() as connection:
        semantic_pending=[]
        if await change_ledger.enabled(connection):
            semantic_pending=await (await connection.execute("""SELECT DISTINCT ON(project_id) project_id,record_json
                FROM assistant.project_file_changes WHERE map_applied_generation IS NULL AND scope_kind='artifact'
                AND record_json ? 'accepted_head_guard'
                ORDER BY project_id,recorded_at LIMIT 10""")).fetchall()
    for entry in semantic_pending:
        try:
            from assistant_kernel.services.semantic_map_v2 import SemanticMapV2
            owner=str(entry['record_json'].get('task_id') or entry['record_json'].get('run_id'))
            roots=scopes.resolve(entry['project_id'],owner,None,shared_agreement=True)
            await asyncio.to_thread(SemanticMapV2(settings).refresh,entry['project_id'],roots=roots)
        except Exception:
            logging.exception('Accepted document Map generation remains pending; no business replay')
    async with repository.database.connection() as connection:
        pending = list(await (await connection.execute(
            """SELECT run_id,project_id,parent_run_id,result_json FROM assistant.runs
                WHERE run_type='publish' AND status='succeeded'
                AND result_json ? 'map_change_batch'
                AND (result_json ? 'map_refresh_pending' OR
                     result_json#>>'{project_change_batch,ledger_committed}'='false')
                ORDER BY updated_at,run_id LIMIT 10"""
        )).fetchall())
    repaired_projects = set()
    for row in pending:
        project_id = str(row["project_id"])
        result = dict(row["result_json"] or {})
        batch = dict(result["map_change_batch"])
        parent_id = str(batch.get("task_id") or row.get("parent_run_id") or row["run_id"])
        try:
            roots = scopes.resolve(project_id, parent_id, "build")
            recovered = await changes.reconcile_pending(project_id, roots=roots, max_batches=20)
            if recovered.get("map_refresh_pending") or recovered.get("pending_batches_remaining"):
                continue
            if not (result.get("project_change_batch") or {}).get("ledger_committed"):
                parent = await repository.get_run(parent_id)
                request = dict((parent or {}).get("request_json") or {})
                refs = list((request.get("accepted_documents") or {}).values())
                if request.get("source_message_id"):
                    refs.append(resource_ref(kind="message", identity=str(request["source_message_id"]), purpose="publication_user_source"))
                captured = await changes.capture_batch(project_id, batch_id=str(batch["batch_id"]),
                    roots=roots, changes=list(batch.get("changes") or []), task_id=parent_id,
                    run_id=str(row["run_id"]), actor="publisher", scope_kind="published",
                    reason="Publisher applied the accepted Task candidate changes.", reason_source_refs=refs)
                result["project_change_batch"] = captured
                if not captured.get("ledger_committed") or captured.get("map_refresh_pending"):
                    continue
            refreshed = await changes.refresh_map(project_id, roots=roots, source_uris=[
                str(item.get("target_uri") or "") for item in batch.get("changes") or []])
            if refreshed.get("map_refresh_pending"):
                continue
            result.pop("map_refresh_pending", None)
            result["project_change_batch"].pop("map_refresh_pending", None)
            async with repository.database.connection() as connection:
                await connection.execute("UPDATE assistant.runs SET result_json=%s,updated_at=now() WHERE run_id=%s AND status='succeeded'",
                    (Jsonb(result), row["run_id"]))
                await connection.commit()
            repaired_projects.add(project_id)
        except Exception:
            logging.exception("Published change evidence remains pending; no Publisher replay")
    # Candidate/artifact ledger sidecars can exist without a publication Run.
    for vault in sorted((settings.data_root / "vaults").glob("*"))[:10]:
        if not vault.is_dir() or vault.name in repaired_projects:
            continue
        directory = vault / "runtime" / "change-pending"
        first = next(directory.glob("*.json"), None) if directory.is_dir() else None
        if first is not None:
            try:
                payload = json.loads(first.read_text(encoding="utf-8"))
                records = payload.get("changes") or []
                owner = str((records[0] if records else {}).get("task_id") or (records[0] if records else {}).get("run_id") or "00000000-0000-0000-0000-000000000000")
                await changes.reconcile_pending(vault.name, roots=scopes.resolve(vault.name, owner, "build"), max_batches=20)
            except Exception:
                logging.exception("Project change sidecar reconciliation remains pending")
    from assistant_kernel.services.task_queue import ProjectTaskQueue
    async with repository.database.connection() as connection:
        queued_projects = list(await (await connection.execute(
            """SELECT DISTINCT m.project_id FROM assistant.messages m
                WHERE m.direction='system' AND m.message_type='work_request'
                AND m.content_json->>'schema_version'='project-work-request.v1'
                AND m.content_json->>'work_status'='queued'
                AND ((m.content_json->>'priority'='urgent' AND m.content_json->>'interrupts_task_id' IS NOT NULL)
                  OR NOT EXISTS (SELECT 1 FROM assistant.runs r WHERE r.project_id=m.project_id
                    AND r.agent_key='project:' || m.project_id || ':manager'
                    AND r.run_type='chat' AND r.status IN ('queued','running','waiting_user','waiting_supervisor','blocked','reconciling')
                    AND COALESCE(r.request_json->>'task_activation','active')='active'))
                UNION
                SELECT r.project_id FROM assistant.runs r WHERE r.run_type='chat' AND r.status='blocked'
                    AND r.request_json->>'task_activation'='suspended'
                    AND NOT EXISTS (SELECT 1 FROM assistant.runs a WHERE a.project_id=r.project_id
                        AND a.agent_key='project:' || r.project_id || ':manager' AND a.run_type='chat'
                        AND a.status IN ('queued','running','waiting_user','waiting_supervisor','blocked','reconciling')
                        AND COALESCE(a.request_json->>'task_activation','active')='active')
                ORDER BY project_id LIMIT 10"""
        )).fetchall())
    for row in queued_projects:
        try:
            await ProjectTaskQueue(settings, repository).activate_next(str(row["project_id"]))
        except Exception:
            logging.exception("Queued Project work activation remains pending")


async def run_worker_loop(
    settings: KernelSettings, shutdown_event: asyncio.Event | None = None,
    queue_kind: str = "interactive",
) -> None:
    """One claim loop: claim a single Run at a time and process it to completion."""
    repository = CoreRepository(settings)
    runtime = ProjectRuntimeManager(settings.data_root)
    processor = RunProcessor(settings, repository, CodexCliHarness(runtime))
    supervisor_processor = SupervisorProcessor(settings, repository)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:runs"
    worker_instance_id = str(uuid.uuid4())
    process_name = os.getenv("SUPERVISOR_PROCESS_NAME") or f"run-worker-{os.getpid()}"
    health = WorkerHealthReporter(settings, process_name)
    health.update(queue_kind=queue_kind)
    claimed_runs = 0
    from assistant_kernel.services.config_consumers import ConfigConsumers
    configuration=ConfigConsumers(settings)
    while shutdown_event is None or not shutdown_event.is_set():
        health.update(
            process_running=True, last_process_heartbeat_at=now_iso(),
            active_run_id=None,
        )
        if not await repository.database.ping():
            logging.error("database unavailable; run worker is waiting")
            health.update(
                last_error_type="DatabaseUnavailable", last_error_at=now_iso(),
            )
            await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
            continue
        try:
            await configuration.apply_dynamic(settings,'worker')
            run = await repository.claim_run(worker_id, worker_instance_id, settings.worker_lease_seconds, queue_kind=queue_kind)
        except Exception as exc:
            logging.exception("run claim failed")
            health.update(
                last_error_type=type(exc).__name__, last_error_at=now_iso(),
            )
            await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
            continue
        if run is None:
            await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
            continue
        claimed_runs += 1
        run_id = str(run["run_id"])
        claim_token = str(run["claim_token"])
        cancel_event = asyncio.Event()
        heartbeat = asyncio.create_task(
            _heartbeat(
                repository, run_id, worker_instance_id, claim_token,
                settings.worker_lease_seconds, cancel_event, health,
            )
        )
        health.update(
            claimed_runs=claimed_runs, last_claim_at=now_iso(),
            active_run_id=run_id, last_error_type=None,
        )
        try:
            if run["run_type"] == "python":
                await execute_python(run, settings, repository, worker_instance_id, cancel_event)
            elif run["run_type"] == "publish":
                publisher_pid = os.getpid()
                publisher_ticks = process_start_ticks(publisher_pid)
                if not await repository.record_process_identity(
                    run_id, worker_instance_id, claim_token, publisher_pid, publisher_ticks,
                ) or not await repository.run_claim_is_active(run_id, worker_instance_id, claim_token):
                    raise asyncio.CancelledError("Publisher lease was lost before file effects")
                try:
                    result, artifacts = await asyncio.to_thread(
                        CapabilityBundleService(settings).publish,
                        project_id=str(run["project_id"]), publish_run_id=run_id,
                        request=dict(run["request_json"] or {}), cancel_event=cancel_event,
                    )
                finally:
                    await repository.clear_publisher_process(run_id, publisher_pid, publisher_ticks)
                if result.get("map_change_batch"):
                    from assistant_kernel.services.project_changes import ProjectChangeService
                    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
                    from assistant_kernel.services.document_runtime import resource_ref
                    changes = ProjectChangeService(settings, repository.database)
                    batch = dict(result["map_change_batch"])
                    parent_id = str(batch.get("task_id") or run.get("parent_run_id") or run_id)
                    try:
                        parent = await repository.get_run(parent_id)
                        parent_request = dict((parent or {}).get("request_json") or {})
                        refs = list((parent_request.get("accepted_documents") or {}).values())
                        if parent_request.get("source_message_id"):
                            refs.append(resource_ref(kind="message", identity=str(parent_request["source_message_id"]), purpose="publication_user_source"))
                        roots = WorkspaceScopeService(settings).resolve(
                            str(run["project_id"]), parent_id, "build",
                        )
                        result["project_change_batch"] = await changes.capture_batch(
                            str(run["project_id"]), batch_id=str(batch["batch_id"]), roots=roots,
                            changes=list(batch.get("changes") or []), task_id=parent_id,
                            run_id=run_id, actor="publisher", scope_kind="published",
                            reason="Publisher applied the accepted Task candidate changes.",
                            reason_source_refs=refs,
                        )
                        if result["project_change_batch"].get("map_refresh_pending"):
                            result["map_refresh_pending"] = result["project_change_batch"]["map_refresh_pending"]
                            changes.maps.mark_stale(str(run["project_id"]), reason="Publisher change capture pending")
                    except Exception as map_error:
                        logging.exception("Publisher succeeded; change ledger/Map refresh remains pending")
                        result["map_refresh_pending"] = {"error_type": type(map_error).__name__,
                                                         "message": str(map_error)[:500]}
                        with suppress(Exception):
                            changes.maps.mark_stale(str(run["project_id"]), reason="Publisher change capture pending")
                await repository.finish_publish_run(
                    run_id, worker_instance_id, claim_token,
                    result=result, artifact_refs=artifacts,
                )
            elif (
                (run.get("request_json") or {}).get("interaction_kind")
                == "supervisor_consultation"
            ):
                await supervisor_processor.process(
                    run, worker_instance_id, cancel_event
                )
            else:
                await processor.process(run, worker_instance_id, cancel_event)
        except RuntimeCapacityUnavailable as exc:
            if not await repository.defer_model_capacity(run_id,worker_instance_id,claim_token,str(exc)):
                raise RuntimeError('Cannot defer a launched or superseded model claim') from exc
        except asyncio.CancelledError:
            await repository.finish_cancelled_run(
                run_id, worker_instance_id, claim_token,
            )
        except Exception as exc:
            logging.exception("run %s failed", run_id)
            health.update(
                last_error_type=type(exc).__name__, last_error_at=now_iso(),
                active_run_id=run_id,
            )
            with suppress(Exception):
                next_status = await repository.retry_run(
                    run_id, worker_instance_id,
                    claim_token,
                    {"type": type(exc).__name__, "message": str(exc)[:2000]},
                )
                latest = await repository.get_run(run_id)
                is_pm = str((latest or run).get("agent_key") or "").endswith(":manager") and run.get("run_type") == "chat"
                modern_pm = is_pm and repository._five_stage_task(latest or run)
                if modern_pm:
                    # Every failed call is a settled Turn. Keep reconciliation
                    # when effects are uncertain, but never auto-replay the PM.
                    await repository.settle_failed_pm_turn(
                        task_id=run_id, reason=f"PM call failed: {type(exc).__name__}", source_run_id=run_id,
                    )
                elif next_status == "dead_letter":
                    await _notify_terminal_failure(repository, run, settings, exc)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
            health.update(
                last_completion_at=now_iso(), active_run_id=None,
                last_process_heartbeat_at=now_iso(),
            )
    logging.info("run worker drained and stopped claiming new Runs")
