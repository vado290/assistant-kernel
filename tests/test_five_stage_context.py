"""Verify effective phase inputs and withdrawal across actual native Session boundaries."""
import json
import uuid
from types import SimpleNamespace

import pytest
import yaml

from test_core4 import core_settings
from test_five_stage_flow import setup_flow, pm_turn, response, settle_governor, through_design
from assistant_kernel.services.agent_results import publication_requires_final_approval
from assistant_kernel.services.public_background import PublicBackgroundService
from assistant_kernel.services.workspace_scope import AuthorizedRoot


async def test_background_withdrawal_rotates_native_session_and_filters_source(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings)
    project = yaml.safe_load((root / "project.yaml").read_text())
    project["background_fields"] = ["user.profession"]
    (root / "project.yaml").write_text(yaml.safe_dump(project))
    source = await repository.ingest_message(project_id="demo", agent_key="project:demo:supervisor",
        channel="discord", chat_id="profile-chat", user_id="flow-user",
        external_event_id=str(uuid.uuid4()), external_message_id="profile",
        content={"text": "Confirmed profession: orbit-specialist"})
    background = PublicBackgroundService(core_settings.data_root)
    background.root.mkdir(parents=True,exist_ok=True)
    fact = {"field_id": "profession", "value": "orbit-specialist", "confirmed": True,
        "status": "active", "confirmed_at": "2026-10-04",
        "source_refs": [{"kind": "message", "id": str(source["message"]["message_id"])}]}
    document = {"schema_version": "user-background.v1", "updated_at": "2026-10-04", "facts": [fact]}
    background.document_path("user").write_text(json.dumps(document))
    await pm_turn(core_settings, repository, harness, task_id, response("continue", "requirements"))
    assert "orbit-specialist" not in harness.requests[-1].prompt
    await settle_governor(core_settings, repository, task_id, "requirements")
    reply = await repository.create_outbound(project_id="demo", session_id=None, run_id=task_id,
        channel="discord", chat_id="profile-chat", user_id="flow-user", message_type="reply",
        content={"text": "Confirmed orbit-specialist", "context_visibility": "semantic"},
        reply_to_message_id=str(source["message"]["message_id"]), idempotency_key="profile-confirmation")
    fact.update({"status": "revoked", "revoked_at": "2026-10-04"})
    background.document_path("user").write_text(json.dumps(document))
    await pm_turn(core_settings, repository, harness, task_id, response("continue", "requirements"))
    assert "orbit-specialist" not in harness.requests[-1].prompt
    history = await repository.list_recent_history(project_id="demo", channel="discord", chat_id="profile-chat")
    assert str(source["message"]["message_id"]) not in {item["message_id"] for item in history["items"]}
    assert str(reply["message_id"]) not in {item["message_id"] for item in history["items"]}


async def test_build_prompt_contains_approved_design(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings)
    _, design = await through_design(core_settings, repository, harness, task_id)
    await pm_turn(core_settings, repository, harness, task_id, response("continue", "implementation"))
    prompt = harness.requests[-1].prompt
    assert design["sha256"] in prompt
    assert "Test offline. Review before publication." in prompt


def test_kernel_code_through_an_alternate_grant_still_requires_final_approval(tmp_path):
    settings = SimpleNamespace(workspace_root=tmp_path, data_root=tmp_path / "data")
    target = tmp_path / "assistant-kernel"
    target.mkdir()
    # The old-URI alias cannot conceal the actual Kernel directory.
    root = AuthorizedRoot(key="shared", path=tmp_path, access="write", description="Existing shared Grant")
    assert publication_requires_final_approval(settings, "ordinary",
        [{"target_uri": "workspace://shared/assistant-kernel/core.py"}], [root])
