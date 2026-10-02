from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.storage import ProjectRuntimeManager


BASE_URL = "http://127.0.0.1:8001"
SUPERVISOR_CONFIG = "/home/v/AI/assistant-kernel/deploy/supervisord.conf"


def supervisor(action: str) -> None:
    subprocess.run(
        ["supervisorctl", "-c", SUPERVISOR_CONFIG, action, "kernel-worker"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


async def verify_repository(settings: KernelSettings, suffix: str) -> None:
    repository = ControlPlaneRepository(settings)
    frontend = "v2-smoke"
    user_id = "smoke-user"
    channel = f"fifo-{suffix}"
    await repository.ensure_default_agent(None)
    concurrent_results = await asyncio.gather(
        *[
            repository.ingest_message(
                frontend=frontend,
                user_id=user_id,
                channel_conversation_id=f"concurrent-{suffix}",
                external_message_id=f"same-{suffix}",
                content={"text": "concurrent duplicate"},
            )
            for _ in range(2)
        ]
    )
    assert str(concurrent_results[0][1]["message_id"]) == str(
        concurrent_results[1][1]["message_id"]
    ), "concurrent duplicate intake created multiple messages"
    concurrent_job_id = str(concurrent_results[0][1]["job_id"])
    concurrent_worker = str(uuid.uuid4())
    concurrent_claim = await repository.claim_job(
        "v2-smoke", concurrent_worker, 60, job_types=["conversation_turn"]
    )
    assert str(concurrent_claim["job_id"]) == concurrent_job_id
    assert await repository.finish_job(concurrent_job_id, concurrent_worker, "cancelled")
    first_conversation, first, _ = await repository.ingest_message(
        frontend=frontend,
        user_id=user_id,
        channel_conversation_id=channel,
        external_message_id=f"first-{suffix}",
        content={"text": "first"},
    )
    _, second, _ = await repository.ingest_message(
        frontend=frontend,
        user_id=user_id,
        channel_conversation_id=channel,
        external_message_id=f"second-{suffix}",
        content={"text": "second"},
    )
    worker_instance = str(uuid.uuid4())
    claimed = await repository.claim_job(
        "v2-smoke", worker_instance, 60, job_types=["conversation_turn"]
    )
    assert str(claimed["job_id"]) == str(first["job_id"]), "FIFO head was not claimed first"
    assert await repository.finish_job(str(claimed["job_id"]), worker_instance, "cancelled")
    claimed = await repository.claim_job(
        "v2-smoke", worker_instance, 60, job_types=["conversation_turn"]
    )
    assert str(claimed["job_id"]) == str(second["job_id"]), "FIFO second item was not released"
    assert await repository.finish_job(str(claimed["job_id"]), worker_instance, "cancelled")

    _, leased_message, _ = await repository.ingest_message(
        frontend=frontend,
        user_id=user_id,
        channel_conversation_id=f"lease-{suffix}",
        external_message_id=f"lease-{suffix}",
        content={"text": "lease fencing"},
    )
    stale_instance = str(uuid.uuid4())
    claimed = await repository.claim_job(
        "v2-smoke-stale", stale_instance, 1, job_types=["conversation_turn"]
    )
    assert str(claimed["job_id"]) == str(leased_message["job_id"])
    await asyncio.sleep(1.2)
    current_instance = str(uuid.uuid4())
    reclaimed = await repository.claim_job(
        "v2-smoke-current", current_instance, 60, job_types=["conversation_turn"]
    )
    assert str(reclaimed["job_id"]) == str(leased_message["job_id"])
    assert not await repository.finish_job(
        str(reclaimed["job_id"]), stale_instance, "succeeded"
    ), "stale worker bypassed the fencing instance id"
    assert await repository.finish_job(
        str(reclaimed["job_id"]), current_instance, "cancelled"
    )

    agent_id = await repository.ensure_default_agent("kernel-v2-smoke")
    logical = await repository.resolve_logical_session(
        agent_id=agent_id,
        scope_type="conversation",
        scope_id=str(first_conversation["conversation_id"]),
        project_id="kernel-v2-smoke",
    )
    physical = await repository.record_successful_turn(
        logical_session_id=str(logical["logical_session_id"]),
        expected_current_record_id=None,
        native_session_id=f"native-old-{suffix}",
        harness="smoke",
        provider=None,
        model=None,
        role_version_id=None,
        prompt_manifest={"smoke": True, "turn": 1},
        message_id=None,
        active_input_tokens=60,
        context_window_tokens=100,
        archive_uri=None,
        archive_sha256=None,
        starts_new_physical_session=True,
    )
    await repository.require_rotation(
        logical_session_id=str(logical["logical_session_id"]),
        physical_session_record_id=str(physical["physical_session_record_id"]),
        project_id="kernel-v2-smoke",
        task_id=None,
        checkpoint_state={"schema_version": "2.0", "next_action": "smoke rotation"},
    )
    before_replacement = await repository.resolve_logical_session(
        agent_id=agent_id,
        scope_type="conversation",
        scope_id=str(first_conversation["conversation_id"]),
        project_id="kernel-v2-smoke",
    )
    assert str(before_replacement["physical_session"]["physical_session_record_id"]) == str(
        physical["physical_session_record_id"]
    ), "rotation moved the pointer before a replacement turn succeeded"
    replacement = await repository.record_successful_turn(
        logical_session_id=str(logical["logical_session_id"]),
        expected_current_record_id=str(physical["physical_session_record_id"]),
        native_session_id=f"native-new-{suffix}",
        harness="smoke",
        provider=None,
        model=None,
        role_version_id=None,
        prompt_manifest={"smoke": True, "turn": 2},
        message_id=None,
        active_input_tokens=10,
        context_window_tokens=100,
        archive_uri=None,
        archive_sha256=None,
        starts_new_physical_session=True,
    )
    assert str(replacement["parent_physical_session_record_id"]) == str(
        physical["physical_session_record_id"]
    )


def main() -> None:
    settings = KernelSettings.from_environment()
    token = settings.load_provider_secrets().internal.kernel_api_token.get_secret_value()
    if not token:
        raise RuntimeError("kernel API token is not configured")
    headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid.uuid4().hex[:12]
    project_id = "kernel-v2-smoke"
    workflow_id = "echo"
    runtime = ProjectRuntimeManager(settings.data_root).ensure(project_id)
    workflow_path = runtime.vault_root / "workflows" / workflow_id / f"v-{suffix}" / "run.py"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        """import json\nimport os\nfrom pathlib import Path\n\ninput_data = json.loads(Path(os.environ['ASSISTANT_INPUT_PATH']).read_text(encoding='utf-8'))\nPath(os.environ['ASSISTANT_RESULT_PATH']).write_text(json.dumps({'echo': input_data['value']}), encoding='utf-8')\n""",
        encoding="utf-8",
    )
    relative_workflow_path = workflow_path.relative_to(runtime.vault_root).as_posix()
    supervisor("stop")
    try:
        with httpx.Client(base_url=BASE_URL, headers=headers, timeout=20) as client:
            binding = client.put(
                "/v2/channel-bindings",
                json={
                    "frontend": "v2-smoke",
                    "user_id": "smoke-user",
                    "channel_conversation_id": f"api-{suffix}",
                    "project_id": project_id,
                    "confirmed_by": "v2-smoke",
                },
            )
            binding.raise_for_status()
            message_payload = {
                "frontend": "v2-smoke",
                "user_id": "smoke-user",
                "channel_conversation_id": f"api-{suffix}",
                "external_message_id": f"message-{suffix}",
                "text": "This message is intentionally cancelled by the V2 smoke test.",
            }
            accepted = client.post("/v2/intake/messages", json=message_payload)
            accepted.raise_for_status()
            duplicate = client.post("/v2/intake/messages", json=message_payload)
            duplicate.raise_for_status()
            first = accepted.json()
            second = duplicate.json()
            assert first["message_id"] == second["message_id"] and second["duplicate"] is True
            cancel = client.post(f"/v2/jobs/{first['job_id']}/cancel")
            cancel.raise_for_status()
            n8n_intake = httpx.post(
                "http://n8n:5678/webhook/kernel-v2/intake",
                json={
                    "frontend": "v2-smoke",
                    "user_id": "smoke-user",
                    "channel_conversation_id": f"n8n-api-{suffix}",
                    "external_message_id": f"n8n-message-{suffix}",
                    "text": "This n8n intake smoke message is intentionally cancelled.",
                },
                timeout=20,
            )
            n8n_intake.raise_for_status()
            n8n_cancel = client.post(f"/v2/jobs/{n8n_intake.json()['job_id']}/cancel")
            n8n_cancel.raise_for_status()

            context = client.put(
                f"/v2/projects/{project_id}/context/D-SMOKE",
                json={
                    "entry_type": "D",
                    "statement": "V2 smoke workflow uses the Kernel as its only state source.",
                    "rationale": "deployment verification",
                    "evidence_refs": ["test://verify_v2"],
                },
            )
            context.raise_for_status()

            version = client.post(
                f"/v2/projects/{project_id}/workflows/{workflow_id}/versions",
                json={
                    "definition_relative_path": relative_workflow_path,
                    "input_schema": {
                        "type": "object",
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "output_schema": {
                        "type": "object",
                        "required": ["echo"],
                        "properties": {"echo": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "spec": {"executor": {"type": "python_script", "timeout_seconds": 60}},
                },
            )
            version.raise_for_status()
            workflow_version_id = version.json()["workflow_version_id"]
            activation = client.post(
                f"/v2/workflow-versions/{workflow_version_id}/activate",
                json={
                    "tests_uri": "test://verify_v2/unit",
                    "review_uri": "test://verify_v2/review",
                    "accepted_by": "v2-smoke",
                    "acceptance_evidence": {"smoke": True},
                },
            )
            activation.raise_for_status()
            asyncio.run(verify_repository(settings, suffix))
            run = httpx.post(
                "http://n8n:5678/webhook/kernel-v2/routine",
                json={
                    "project_id": project_id,
                    "workflow_id": workflow_id,
                    "idempotency_key": f"run-{suffix}",
                    "input": {"value": suffix},
                },
                timeout=20,
            )
            run.raise_for_status()
            workflow_run_id = run.json()["workflow_run_id"]
            assert run.json()["workflow_version_id"] == workflow_version_id

            newer_version = client.post(
                f"/v2/projects/{project_id}/workflows/{workflow_id}/versions",
                json={
                    "definition_relative_path": relative_workflow_path,
                    "input_schema": {
                        "type": "object",
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "output_schema": {
                        "type": "object",
                        "required": ["echo"],
                        "properties": {"echo": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "spec": {"executor": {"type": "python_script", "timeout_seconds": 60}},
                },
            )
            newer_version.raise_for_status()
            newer_version_id = newer_version.json()["workflow_version_id"]
            assert newer_version_id != workflow_version_id
            newer_activation = client.post(
                f"/v2/workflow-versions/{newer_version_id}/activate",
                json={
                    "tests_uri": "test://verify_v2/unit-newer",
                    "review_uri": "test://verify_v2/review-newer",
                    "accepted_by": "v2-smoke",
                    "acceptance_evidence": {"smoke": True, "newer": True},
                },
            )
            newer_activation.raise_for_status()

    finally:
        supervisor("start")

    deadline = time.monotonic() + 90
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=20) as client:
        while time.monotonic() < deadline:
            response = client.get(f"/v2/workflow-runs/{workflow_run_id}")
            response.raise_for_status()
            status_value = response.json()["status"]
            if status_value in {"succeeded", "failed", "cancelled"}:
                assert status_value == "succeeded", response.text
                assert response.json()["workflow_version_id"] == workflow_version_id
                assert response.json()["output_json"]["result"]["echo"] == suffix
                print(
                    json.dumps(
                        {
                            "status": "V2_SMOKE_OK",
                            "workflow_run_id": workflow_run_id,
                            "workflow_version_id": workflow_version_id,
                            "project_id": project_id,
                        },
                        sort_keys=True,
                    )
                )
                return
            time.sleep(1)
    raise TimeoutError("workflow run did not reach a terminal state")


if __name__ == "__main__":
    main()
