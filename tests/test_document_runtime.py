from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.document_runtime import (
    TaskDocumentService, resource_ref, validate_resource_ref,
)


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})


def test_resource_ref_requires_integrity_for_documents() -> None:
    with pytest.raises(ValueError):
        resource_ref(kind="document", identity="task:x", uri="vault://p/x")
    message = resource_ref(kind="message", identity="m-1", purpose="user_input")
    assert validate_resource_ref(message) == message


def test_immutable_revision(tmp_path: Path) -> None:
    service = TaskDocumentService(settings_for(tmp_path))
    ref = service.write_revision(
        project_id="demo", task_id="task-1", document_kind="turn-record",
        document_schema="turn-record.v1",
        payload={"turn_number": 1, "outcome": "continue", "next_action": "next"},
        status="settled", actor_type="pm", actor_id="project:demo:manager",
        run_id="task-1", reason="PM turn settled",
        source_refs=[resource_ref(kind="message", identity="message-1")],
    )
    same = service.write_revision(
        project_id="demo", task_id="task-1", document_kind="turn-record",
        document_schema="turn-record.v1",
        payload={"turn_number": 1, "outcome": "continue", "next_action": "next"},
        status="settled", actor_type="pm", actor_id="project:demo:manager",
        run_id="task-1", reason="PM turn settled",
        source_refs=[resource_ref(kind="message", identity="message-1")],
    )
    assert same == ref
    assert service.read("demo", ref)["payload"]["turn_number"] == 1


def test_document_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    service = TaskDocumentService(settings_for(tmp_path))
    ref = service.write_revision(
        project_id="demo", task_id="task-1", document_kind="task-contract",
        document_schema="task-contract.v1",
        payload={"goal": "g", "requirements": [], "acceptance_criteria": []},
        status="draft", actor_type="kernel", actor_id="kernel", run_id="task-1",
        reason="bootstrap",
    )
    path = tmp_path / "vaults" / "demo" / ref["uri"].split("/", 3)[-1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["payload"]["goal"] = "changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        service.read("demo", ref)
