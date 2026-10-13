from __future__ import annotations

import json
import uuid

import pytest

from assistant_kernel.services.public_background import PublicBackgroundService


def fact(field_id, value, **changes):
    return {
        "field_id": field_id, "value": value, "confirmed": True, "status": "active",
        "confirmed_at": "2026-10-04", "source_refs": [{"kind": "message", "id": "user-confirmation"}],
        **changes,
    }


def write_user(service, facts):
    service.root.mkdir(parents=True, exist_ok=True)
    service.document_path("user").write_text(json.dumps({
        "schema_version": "user-background.v1", "updated_at": "2026-10-04", "facts": facts,
    }), encoding="utf-8")


def test_read_empty_background_does_not_create_or_collect_data(tmp_path):
    service = PublicBackgroundService(tmp_path)
    assert service.read_project_context("demo")["documents"] == []
    with pytest.raises(PermissionError,match="retired"): service.ensure()
    assert not service.root.exists()


def test_only_confirmed_active_sourced_facts_are_returned(tmp_path):
    service=PublicBackgroundService(tmp_path)
    write_user(service,[fact("profession","developer")])
    assert service.read_project_context("demo")["documents"]==[]


def test_relevant_fields_and_revocation_are_applied_on_each_read(tmp_path):
    service=PublicBackgroundService(tmp_path)
    write_user(service,[fact("profession","developer")])
    assert service.read_project_context("demo",fields=["user.profession"])["documents"]==[]


def test_invalid_background_schema_and_project_path_are_rejected(tmp_path):
    service=PublicBackgroundService(tmp_path)
    with pytest.raises(ValueError,match="unknown"):service.document_path("../../private")


def test_revoked_sources_only_collect_actual_typed_message_ids(tmp_path):
    service = PublicBackgroundService(tmp_path)
    message_id, document_id, environment_message_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    write_user(service, [fact("age", 30, status="withdrawn", source_refs=[
        {"kind": "message", "id": message_id}, {"kind": "document", "id": document_id},
        {"kind": "message", "id": "free-text-not-a-message-id"}, {"id": document_id}])])
    service.document_path("environment").write_text(json.dumps({
        "schema_version": "development-environment.v1", "facts": [fact("host", "old-host", status="revoked",
            source_refs=[{"kind": "message", "message_id": environment_message_id}])]}), encoding="utf-8")
    assert service.revoked_source_message_ids() == {message_id, environment_message_id}


def test_withdrawn_source_cannot_reinject_old_active_copy_but_new_confirmation_can(tmp_path):
    service=PublicBackgroundService(tmp_path)
    old_id=str(uuid.uuid4())
    write_user(service,[fact("profession","old",status="revoked",source_refs=[{"kind":"message","id":old_id}]),fact("profession","new")])
    assert service.revoked_source_message_ids()=={old_id}
    assert service.read_project_context("demo")["documents"]==[]
