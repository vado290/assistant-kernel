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
    channel = f"conversation-live-{suffix}"
    with httpx.Client(base_url="http://127.0.0.1:8001", headers=headers, timeout=30) as client:
        binding = client.put(
            "/v2/channel-bindings",
            json={
                "frontend": "v2-smoke",
                "user_id": "smoke-user",
                "channel_conversation_id": channel,
                "project_id": "kernel-v2-smoke",
                "confirmed_by": "v2-smoke",
            },
        )
        binding.raise_for_status()
        intake = client.post(
            "/v2/intake/messages",
            json={
                "frontend": "v2-smoke",
                "user_id": "smoke-user",
                "channel_conversation_id": channel,
                "external_message_id": f"message-{suffix}",
                "text": "This is a read-only live verification. Reply exactly CONVERSATION_LIVE_OK",
                "model": "gpt-5.1",
                "reasoning_effort": "medium",
            },
        )
        intake.raise_for_status()
        accepted = intake.json()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = client.get(f"/v2/jobs/{accepted['job_id']}")
            job.raise_for_status()
            payload = job.json()
            if payload["status"] == "succeeded":
                conversation = client.get(f"/v2/conversations/{accepted['conversation_id']}")
                conversation.raise_for_status()
                outbound = [
                    item for item in conversation.json()["messages"]
                    if item["direction"] == "outbound"
                ]
                assert len(outbound) == 1
                assert outbound[0]["content"]["text"].strip() == "CONVERSATION_LIVE_OK"
                reply = payload["result_json"]["reply"]
                assert reply["outbox_event_id"]
                print(
                    json.dumps(
                        {
                            "status": "CONVERSATION_LIVE_OK",
                            "conversation_id": accepted["conversation_id"],
                            "job_id": accepted["job_id"],
                            "outbox_event_id": reply["outbox_event_id"],
                        },
                        sort_keys=True,
                    )
                )
                return
            if payload["status"] in {"failed", "cancelled", "dead_letter"}:
                raise RuntimeError(f"conversation job failed: {payload}")
            time.sleep(1)
    raise TimeoutError("conversation job did not complete")


if __name__ == "__main__":
    main()
