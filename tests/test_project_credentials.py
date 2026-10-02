from __future__ import annotations

import json

import pytest

from assistant_kernel.services.project_credentials import ProjectCredentialsStore


def test_project_credentials_round_trip_and_metadata_hides_values(tmp_path):
    store = ProjectCredentialsStore(tmp_path)
    empty = store.list_metadata("demo")
    assert empty["schema_version"] == "project-credentials.v3"
    assert empty["credentials"] == []
    request = store.request(
        project_id="demo", run_id="run-1", credential_key="alpaca_market_data",
        description="Alpaca market data",
        fields=[
            {"name": "APCA_API_KEY_ID", "label": "Key ID"},
            {"name": "APCA_API_SECRET_KEY", "label": "Secret"},
        ],
    )
    result = store.complete_request(
        project_id="demo", request_id=request["request_id"],
        fields={"APCA_API_KEY_ID": "id-value", "APCA_API_SECRET_KEY": "secret-value"},
        external_event_id="event-1",
    )
    assert result["status"] == "saved"
    metadata = store.list_metadata("demo")
    assert metadata["credentials"][0]["credential_key"] == "alpaca_market_data"
    assert metadata["credentials"][0]["purpose"] == "Alpaca market data"
    assert metadata["credentials"][0]["revision"] == 1
    assert metadata["credentials"][0]["value_descriptions"] == [
        {"path": "APCA_API_KEY_ID", "purpose": "Key ID", "env": "APCA_API_KEY_ID"},
        {
            "path": "APCA_API_SECRET_KEY", "purpose": "Secret",
            "env": "APCA_API_SECRET_KEY",
        },
    ]
    assert "id-value" not in json.dumps(metadata)
    assert "secret-value" not in json.dumps(metadata)
    assert store.get("demo", "alpaca_market_data")["fields"] == {
        "APCA_API_KEY_ID": "id-value",
        "APCA_API_SECRET_KEY": "secret-value",
    }
    document = json.loads(store.path("demo").read_text(encoding="utf-8"))
    record = document["credentials"]["alpaca_market_data"]
    assert document["schema_version"] == "project-credentials.v3"
    assert document["project_id"] == "demo"
    assert record["credential_key"] == "alpaca_market_data"
    assert record["purpose"] == "Alpaca market data"
    assert record["status"] == "active"
    assert record["revision"] == 1
    assert record["last_request_id"] == request["request_id"]
    assert record["last_external_event_id"] == "event-1"
    assert record["created_at"]
    assert record["updated_at"]
    assert record["values"] == {
        "APCA_API_KEY_ID": {
            "value": "id-value", "purpose": "Key ID", "env": "APCA_API_KEY_ID",
        },
        "APCA_API_SECRET_KEY": {
            "value": "secret-value", "purpose": "Secret",
            "env": "APCA_API_SECRET_KEY",
        },
    }
    assert store.environment("demo")["APCA_API_SECRET_KEY"] == "secret-value"
    assert store.complete_request(
        project_id="demo", request_id=request["request_id"],
        fields={"APCA_API_KEY_ID": "different", "APCA_API_SECRET_KEY": "different"},
        external_event_id="event-1",
    )["status"] == "already_saved"
    assert store.delete("demo", "alpaca_market_data")["deleted"] is True


def test_project_credentials_reject_conflicting_environment_fields(tmp_path):
    store = ProjectCredentialsStore(tmp_path)
    for index, value in enumerate(("first", "second"), start=1):
        request = store.request(
            project_id="demo", run_id=f"run-{index}", credential_key=f"credential_{index}",
            description="test", fields=[{"name": "TOKEN", "label": "Token"}],
        )
        store.complete_request(
            project_id="demo", request_id=request["request_id"],
            fields={"TOKEN": value}, external_event_id=f"event-{index}",
        )
    with pytest.raises(ValueError, match="conflicts"):
        store.environment("demo")


def test_project_credentials_migrates_v1_and_tracks_updates(tmp_path):
    store = ProjectCredentialsStore(tmp_path)
    path = store.path("demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "project-credentials.v1",
        "credentials": {
            "service": {
                "description": "first purpose",
                "fields": {"TOKEN": "old"},
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }), encoding="utf-8")
    migrated = store.load("demo")
    assert migrated["schema_version"] == "project-credentials.v3"
    assert migrated["credentials"]["service"]["purpose"] == "first purpose"
    assert migrated["credentials"]["service"]["values"]["TOKEN"] == {
        "value": "old", "purpose": "first purpose: TOKEN", "env": "TOKEN",
    }
    request = store.request(
        project_id="demo", run_id="run-update", credential_key="service",
        description="updated purpose", fields=[{"name": "TOKEN", "label": "Token"}],
    )
    store.complete_request(
        project_id="demo", request_id=request["request_id"],
        fields={"TOKEN": "new"}, external_event_id="event-update",
    )
    updated = store.get("demo", "service")
    assert updated["purpose"] == "updated purpose"
    assert updated["revision"] == 2
    assert updated["fields"] == {"TOKEN": "new"}
    assert updated["created_at"] == "2026-01-01T00:00:00+00:00"


def test_project_credentials_accept_nested_values_with_purpose_per_leaf(tmp_path):
    store = ProjectCredentialsStore(tmp_path)
    path = store.path("demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "project-credentials.v3",
        "project_id": "demo",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "credentials": {
            "alpaca": {
                "credential_key": "alpaca",
                "purpose": "Alpaca paper account",
                "status": "active",
                "revision": 1,
                "values": {
                    "auth": {
                        "key_id": {
                            "value": "id", "purpose": "Paper account key ID",
                            "env": "APCA_API_KEY_ID",
                        },
                        "secret": {
                            "value": "secret-value-nonce", "purpose": "Paper account secret",
                            "env": "APCA_API_SECRET_KEY",
                        },
                    },
                    "endpoint": {
                        "value": "https://paper-api.alpaca.markets",
                        "purpose": "Paper trading base URL",
                        "env": "APCA_API_BASE_URL",
                    },
                },
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }), encoding="utf-8")
    loaded = store.get("demo", "alpaca")
    assert loaded["values"]["auth"]["secret"]["purpose"] == "Paper account secret"
    assert loaded["fields"]["APCA_API_BASE_URL"] == "https://paper-api.alpaca.markets"
    metadata = store.list_metadata("demo")["credentials"][0]
    assert {item["path"] for item in metadata["value_descriptions"]} == {
        "auth.key_id", "auth.secret", "endpoint",
    }
    assert "secret-value-nonce" not in json.dumps(metadata)


def test_project_credentials_reject_value_without_own_purpose(tmp_path):
    store = ProjectCredentialsStore(tmp_path)
    path = store.path("demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "project-credentials.v3",
        "project_id": "demo",
        "credentials": {
            "service": {
                "credential_key": "service", "purpose": "Service", "revision": 1,
                "status": "active", "values": {"token": {"value": "secret"}},
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="purpose must be non-empty"):
        store.load("demo")
