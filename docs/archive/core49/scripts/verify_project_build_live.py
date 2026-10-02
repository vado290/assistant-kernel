from __future__ import annotations

import json
import time
import uuid

import httpx

from assistant_kernel.config import KernelSettings


def main() -> None:
    settings = KernelSettings.from_environment()
    token = settings.load_provider_secrets().internal.kernel_api_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid.uuid4().hex[:12]
    base_url = "http://127.0.0.1:8001"
    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        response = client.post(
            "/v2/projects/kernel-v2-smoke/builds",
            headers={"X-Idempotency-Key": f"live-build-{suffix}"},
            json={
                "request": {
                    "goal": "Produce a minimal requirements artifact for a smoke-test echo workflow.",
                    "constraints": [
                        "Do not modify files",
                        "Do not call external systems",
                        "Keep the result concise",
                    ],
                    "acceptance": ["Requirements artifact is persisted and requests user confirmation"],
                },
                "execution_profile": {
                    "provider": "openai",
                    "model": "gpt-5.1",
                    "reasoning_effort": "medium",
                    "timeout_seconds": 120,
                },
            },
        )
        response.raise_for_status()
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 300
        approval_id = None
        while time.monotonic() < deadline:
            task = client.get(f"/v2/tasks/{task_id}")
            task.raise_for_status()
            task_status = task.json()["status"]
            if task_status == "waiting_user":
                approvals = client.get(f"/v2/tasks/{task_id}/approvals")
                approvals.raise_for_status()
                pending = [
                    item for item in approvals.json()["approvals"]
                    if item["status"] == "pending"
                ]
                assert len(pending) == 1
                approval_id = pending[0]["approval_id"]
                break
            if task_status in {"failed", "cancelled", "succeeded"}:
                raise RuntimeError(f"project build terminated before approval: {task_status}")
            time.sleep(2)
        if approval_id is None:
            raise TimeoutError("requirements approval was not created")
        decision = client.post(
            f"/v2/approvals/{approval_id}/decisions",
            headers={"X-Idempotency-Key": f"approve-requirements-{suffix}"},
            json={"decision": "approve", "decided_by": "v2-smoke"},
        )
        decision.raise_for_status()
        duplicate = client.post(
            f"/v2/approvals/{approval_id}/decisions",
            headers={"X-Idempotency-Key": f"approve-requirements-duplicate-{suffix}"},
            json={"decision": "approve", "decided_by": "v2-smoke"},
        )
        duplicate.raise_for_status()
        assert duplicate.json()["duplicate"] is True

        architecture_approval_id = None
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            task = client.get(f"/v2/tasks/{task_id}")
            task.raise_for_status()
            if task.json()["status"] == "waiting_user":
                approvals = client.get(f"/v2/tasks/{task_id}/approvals")
                approvals.raise_for_status()
                pending = [
                    item
                    for item in approvals.json()["approvals"]
                    if item["status"] == "pending" and item["approval_type"] == "architecture"
                ]
                if pending:
                    assert len(pending) == 1
                    architecture_approval_id = pending[0]["approval_id"]
                    break
            if task.json()["status"] in {"failed", "cancelled", "succeeded"}:
                raise RuntimeError(f"unexpected terminal status: {task.json()['status']}")
            time.sleep(1)
        if architecture_approval_id is None:
            raise TimeoutError("architecture approval was not created after resume")

        reject = client.post(
            f"/v2/approvals/{architecture_approval_id}/decisions",
            headers={"X-Idempotency-Key": f"reject-architecture-{suffix}"},
            json={"decision": "reject", "decided_by": "v2-smoke"},
        )
        reject.raise_for_status()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            task = client.get(f"/v2/tasks/{task_id}")
            task.raise_for_status()
            if task.json()["status"] == "cancelled":
                print(
                    json.dumps(
                        {
                            "status": "PROJECT_BUILD_LIVE_OK",
                            "task_id": task_id,
                            "requirements_approval_id": approval_id,
                            "architecture_approval_id": architecture_approval_id,
                        },
                        sort_keys=True,
                    )
                )
                return
            if task.json()["status"] in {"failed", "succeeded"}:
                raise RuntimeError(f"unexpected terminal status: {task.json()['status']}")
            time.sleep(1)
    raise TimeoutError("architecture-rejected project build did not cancel")


if __name__ == "__main__":
    main()
