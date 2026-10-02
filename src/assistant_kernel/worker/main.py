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
from assistant_kernel.services.feishu import channel_worker_loop, feishu_gateway_loop
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
    terminate_process_group,
    terminate_recorded_process_group,
)


async def _notify_terminal_failure(
    repository: CoreRepository, run: dict, settings: KernelSettings | None = None,
    error: Exception | None = None,
) -> None:
    """Queue one user-safe terminal failure notice on the triggering channel."""
    trigger_message_id = run.get("trigger_message_id")
    if not trigger_message_id:
        return
    trigger = await repository.get_message(str(trigger_message_id))
    if trigger is None or str(trigger.get("channel")) not in {"feishu", "discord"}:
        return
    run_id = str(run["run_id"])
    request = dict(run.get("request_json") or {})
    is_supervisor = request.get("interaction_kind") == "supervisor_consultation"
    if is_supervisor and request.get("task_id"):
        await repository.resume_supervision_degraded(
            task_id=str(request["task_id"]),
            turn_number=int(request.get("turn_number") or 0),
            reason="Supervisor Run reached terminal failure",
        )
    if request.get("interaction_kind") == "child_task":
        await repository.settle_terminal_child_failure(str(run["run_id"]))
        # Child Runs are parent-only on every terminal path.  The parent Task
        # owns user communication after its delegation state is reconciled.
        return
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
                "你可以稍后重新发送，或让维护人员检查该 Run。"
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
    prefix = f"vault://{project_id}/"
    source_uri = str(request.get("source_uri") or "")
    if not source_uri.startswith(prefix):
        raise ValueError("python source_uri is outside its Project Vault")
    source = runtime_manager.resolve_vault_file(project_id, source_uri[len(prefix):])
    if not source.is_file() or source.suffix.lower() != ".py":
        raise ValueError("python source is not an existing .py file")

    output_root = runtime.vault_root / "runs" / str(run["run_id"])
    output_root.mkdir(parents=True, exist_ok=True)
    stdout_path = output_root / "stdout.log"
    stderr_path = output_root / "stderr.log"
    result_path = output_root / "result.json"
    input_path = output_root / "input.json"
    input_path.write_text(
        json.dumps(request.get("input", {}), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ASSISTANT_RUN_ID": str(run["run_id"]),
            "ASSISTANT_PROJECT_ID": project_id,
            "ASSISTANT_RESULT_PATH": str(result_path),
            "ASSISTANT_INPUT_PATH": str(input_path),
        }
    )
    args = [str(item) for item in request.get("args", [])]
    timeout_seconds = min(max(int(request.get("timeout_seconds", 300)), 1), 3600)
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                str(settings.python_executable), str(source), *args,
                cwd=str(runtime.vault_root), env=environment,
                stdout=stdout, stderr=stderr, start_new_session=True,
            )
            if not await repository.record_process_identity(
                str(run["run_id"]), worker_instance_id, claim_token,
                process.pid, process_start_ticks(process.pid),
            ):
                await terminate_process_group(process)
                raise asyncio.CancelledError("run lease was lost as Python started")
            while process.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                if cancel_event.is_set():
                    await terminate_process_group(process)
                    await repository.finish_run(
                        str(run["run_id"]), worker_instance_id, claim_token,
                        status="cancelled",
                        error={"type": "Cancelled", "message": "cancellation was requested"},
                    )
                    return
                if time.monotonic() - started > timeout_seconds:
                    await terminate_process_group(process)
                    raise TimeoutError(f"python execution exceeded {timeout_seconds} seconds")
        if process.returncode != 0:
            raise RuntimeError(f"python script exited with code {process.returncode}")
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
        refs = [
            f"vault://{project_id}/runs/{run['run_id']}/stdout.log",
            f"vault://{project_id}/runs/{run['run_id']}/stderr.log",
        ]
        if result_path.is_file():
            refs.append(f"vault://{project_id}/runs/{run['run_id']}/result.json")
        if not await repository.finish_run(
            str(run["run_id"]), worker_instance_id, claim_token, status="succeeded",
            result=result, artifact_refs=refs,
        ):
            raise RuntimeError("python run lease was lost before completion")
    except Exception:
        if process is not None and process.returncode is None:
            await terminate_process_group(process)
        raise


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
    while shutdown_event is None or not shutdown_event.is_set():
        if not await repository.database.ping():
            await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
            continue
        try:
            expired = await repository.quarantine_expired_runs()
            for abandoned in expired:
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
            await reconciliations.notify_due()
        except Exception:
            logging.exception("run maintenance failed")
        if shutdown_event is not None and shutdown_event.is_set():
            break
        await _poll_or_shutdown(shutdown_event, settings.worker_poll_seconds)
    logging.info("run maintenance loop drained and stopped")


async def run_worker_loop(
    settings: KernelSettings, shutdown_event: asyncio.Event | None = None,
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
    claimed_runs = 0
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
            run = await repository.claim_run(worker_id, worker_instance_id, settings.worker_lease_seconds)
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
                result, artifacts = CapabilityBundleService(settings).publish(
                    project_id=str(run["project_id"]), publish_run_id=run_id,
                    request=dict(run["request_json"] or {}),
                )
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
                if next_status == "dead_letter":
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


async def worker_loop() -> None:
    settings = KernelSettings.from_environment()
    claim_loops = [
        run_worker_loop(settings) for _ in range(max(1, settings.worker_claim_loops))
    ]
    await asyncio.gather(
        run_maintenance_loop(settings),
        *claim_loops,
        channel_worker_loop(settings), feishu_gateway_loop(settings)
    )


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(worker_loop())


if __name__ == "__main__":
    run()
