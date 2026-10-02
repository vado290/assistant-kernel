"""Project-local plaintext credentials for the first-release Kernel workflow."""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from assistant_kernel.storage import ProjectRuntimeManager


CREDENTIAL_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
VALUE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,127}$")
ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SCHEMA_VERSION = "project-credentials.v3"
LEGACY_SCHEMA_VERSIONS = {"project-credentials.v1", "project-credentials.v2"}
REQUEST_SCHEMA_VERSION = "project-credential-request.v1"


class ProjectCredentialsStore:
    """Own one atomic JSON credential document per Project Vault."""

    def __init__(self, data_root: Path):
        self.runtime = ProjectRuntimeManager(data_root)

    def path(self, project_id: str) -> Path:
        return self.runtime.ensure(project_id).vault_root / "runtime" / "credentials.json"

    def request_root(self, project_id: str) -> Path:
        return self.runtime.ensure(project_id).vault_root / "runtime" / "credential-requests"

    @staticmethod
    def _empty(project_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "created_at": now,
            "updated_at": now,
            "credentials": {},
        }

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)

    @contextmanager
    def _lock(self, path: Path) -> Iterator[None]:
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def ensure(self, project_id: str) -> Path:
        target = self.path(project_id)
        with self._lock(target):
            if not target.exists():
                self._atomic_json(target, self._empty(project_id))
            original = json.loads(target.read_text(encoding="utf-8"))
            payload = self._load_path(target, project_id)
            if payload != original:
                self._atomic_json(target, payload)
        return target

    def _load_path(self, target: Path, project_id: str) -> dict[str, Any]:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            SCHEMA_VERSION, *LEGACY_SCHEMA_VERSIONS,
        }:
            raise ValueError("unsupported Project credentials document")
        credentials = payload.get("credentials")
        if not isinstance(credentials, dict):
            raise ValueError("Project credentials must contain an object named credentials")
        now = datetime.now(timezone.utc).isoformat()
        if payload.get("schema_version") in LEGACY_SCHEMA_VERSIONS:
            migrated: dict[str, Any] = {}
            for key, record in credentials.items():
                raw = dict(record or {})
                purpose = str(raw.get("purpose") or raw.get("description") or "")
                values = raw.get("values")
                if not isinstance(values, dict):
                    values = {
                        str(field): {
                            "value": value,
                            "purpose": f"{purpose}: {field}" if purpose else str(field),
                            "env": str(field),
                        }
                        for field, value in dict(raw.get("fields") or {}).items()
                    }
                migrated[str(key)] = {
                    "credential_key": str(key),
                    "purpose": purpose,
                    "description": purpose,
                    "status": str(raw.get("status") or "active"),
                    "revision": int(raw.get("revision") or 1),
                    "values": values,
                    "created_at": raw.get("created_at") or now,
                    "updated_at": raw.get("updated_at") or now,
                    "last_request_id": raw.get("last_request_id"),
                    "last_external_event_id": raw.get("last_external_event_id"),
                }
            payload = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "created_at": payload.get("created_at") or now,
                "updated_at": payload.get("updated_at") or now,
                "credentials": migrated,
            }
            credentials = migrated
        if str(payload.get("project_id") or "") != project_id:
            raise ValueError("Project credentials document belongs to another Project")
        payload["created_at"] = payload.get("created_at") or now
        payload["updated_at"] = payload.get("updated_at") or now
        for key, record in credentials.items():
            self._validate_record(str(key), record)
        return payload

    @staticmethod
    def _walk_values(
        values: dict[str, Any], prefix: tuple[str, ...] = (),
    ) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
        for raw_name, node in values.items():
            name = str(raw_name)
            if not VALUE_KEY.fullmatch(name):
                raise ValueError(f"invalid credential value key: {name}")
            path = (*prefix, name)
            if not isinstance(node, dict):
                raise ValueError(
                    f"credential value {'.'.join(path)} must contain value and purpose"
                )
            if "value" in node:
                extra = set(node) - {"value", "purpose", "env"}
                if extra:
                    raise ValueError(
                        f"credential value {'.'.join(path)} has unsupported keys: {sorted(extra)}"
                    )
                yield path, node
            else:
                if not node:
                    raise ValueError(f"credential value group {'.'.join(path)} is empty")
                yield from ProjectCredentialsStore._walk_values(node, path)

    @classmethod
    def _validate_record(cls, key: str, record: Any) -> None:
        if not CREDENTIAL_KEY.fullmatch(key):
            raise ValueError(f"invalid credential key: {key}")
        if not isinstance(record, dict) or not isinstance(record.get("values"), dict):
            raise ValueError(f"credential {key} must contain a values object")
        if str(record.get("credential_key") or key) != key:
            raise ValueError(f"credential record key differs: {key}")
        purpose = record.get("purpose", record.get("description", ""))
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError(f"credential {key} purpose must be non-empty text")
        if int(record.get("revision") or 0) < 1:
            raise ValueError(f"credential {key} revision must be positive")
        if str(record.get("status") or "active") != "active":
            raise ValueError(f"credential {key} status is unsupported")
        for timestamp_name in ("created_at", "updated_at"):
            if not isinstance(record.get(timestamp_name), str) or not record[timestamp_name]:
                raise ValueError(
                    f"credential {key} {timestamp_name} must be non-empty text"
                )
        leaves = list(cls._walk_values(record["values"]))
        if not leaves:
            raise ValueError(f"credential {key} must contain at least one value")
        environment_paths: dict[str, str] = {}
        for path, leaf in leaves:
            value = leaf.get("value")
            if value is None or isinstance(value, (dict, list)) or value == "":
                raise ValueError(
                    f"credential value {'.'.join(path)} must be a non-empty scalar"
                )
            purpose = leaf.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                raise ValueError(
                    f"credential value {'.'.join(path)} purpose must be non-empty text"
                )
            environment_name = leaf.get("env")
            if environment_name is not None and not ENVIRONMENT_KEY.fullmatch(
                str(environment_name)
            ):
                raise ValueError(
                    f"credential value {'.'.join(path)} env name is invalid"
                )
            if environment_name:
                environment_name = str(environment_name)
                if environment_name in environment_paths:
                    raise ValueError(
                        f"credential env {environment_name} is assigned by both "
                        f"{environment_paths[environment_name]} and {'.'.join(path)}"
                    )
                environment_paths[environment_name] = ".".join(path)

    def load(self, project_id: str) -> dict[str, Any]:
        target = self.ensure(project_id)
        with self._lock(target):
            return self._load_path(target, project_id)

    def list_metadata(self, project_id: str) -> dict[str, Any]:
        payload = self.load(project_id)
        credentials: list[dict[str, Any]] = []
        for key, record in sorted(payload["credentials"].items()):
            descriptions = [
                {
                    "path": ".".join(path),
                    "purpose": str(leaf["purpose"]),
                    "env": leaf.get("env"),
                }
                for path, leaf in self._walk_values(record["values"])
            ]
            credentials.append({
                "credential_key": key,
                "purpose": str(record.get("purpose") or record.get("description") or ""),
                "description": str(record.get("purpose") or record.get("description") or ""),
                "status": str(record.get("status") or "active"),
                "revision": int(record.get("revision") or 1),
                "field_names": sorted(
                    str(item["env"]) for item in descriptions if item.get("env")
                ),
                "value_descriptions": descriptions,
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "credentials": credentials,
        }

    def get(self, project_id: str, credential_key: str) -> dict[str, Any]:
        if not CREDENTIAL_KEY.fullmatch(credential_key):
            raise ValueError("credential_key must be a safe name")
        record = self.load(project_id)["credentials"].get(credential_key)
        if record is None:
            raise KeyError(f"Project credential was not found: {credential_key}")
        fields = {
            str(leaf["env"]): str(leaf["value"])
            for _, leaf in self._walk_values(record["values"])
            if leaf.get("env")
        }
        return {
            "project_id": project_id,
            "credential_key": credential_key,
            "purpose": str(record.get("purpose") or record.get("description") or ""),
            "description": str(record.get("purpose") or record.get("description") or ""),
            "status": str(record.get("status") or "active"),
            "revision": int(record.get("revision") or 1),
            "values": dict(record["values"]),
            "fields": fields,
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }

    def environment(self, project_id: str) -> dict[str, str]:
        environment: dict[str, str] = {}
        owners: dict[str, str] = {}
        for key, record in self.load(project_id)["credentials"].items():
            for _, leaf in self._walk_values(record["values"]):
                field = leaf.get("env")
                if not field:
                    continue
                value = str(leaf["value"])
                if field in environment and environment[field] != value:
                    raise ValueError(
                        f"credential field {field} conflicts between {owners[field]} and {key}"
                    )
                environment[str(field)] = str(value)
                owners[str(field)] = str(key)
        return environment

    @staticmethod
    def _normalize_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not 1 <= len(fields) <= 12:
            raise ValueError("credential request requires between 1 and 12 fields")
        for raw in fields:
            name = str(raw.get("name") or "").strip()
            label = str(raw.get("label") or name).strip()
            if (
                not ENVIRONMENT_KEY.fullmatch(name)
                or name in seen
                or not 1 <= len(label) <= 80
            ):
                raise ValueError("credential field name or label is invalid")
            seen.add(name)
            normalized.append({
                "name": name,
                "label": label,
                "required": bool(raw.get("required", True)),
            })
        return normalized

    def request(
        self, *, project_id: str, run_id: str, credential_key: str,
        description: str, fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not CREDENTIAL_KEY.fullmatch(credential_key):
            raise ValueError("credential_key must be a safe name")
        normalized = self._normalize_fields(fields)
        material = json.dumps(
            [project_id, run_id, credential_key, normalized],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        import hashlib
        request_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        target = self.request_root(project_id) / f"{request_id}.json"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock(target):
            if target.exists():
                request = json.loads(target.read_text(encoding="utf-8"))
                if (
                    request.get("project_id") != project_id
                    or request.get("run_id") != run_id
                    or request.get("credential_key") != credential_key
                    or request.get("fields") != normalized
                ):
                    raise RuntimeError("credential request path collision")
            else:
                request = {
                    "schema_version": REQUEST_SCHEMA_VERSION,
                    "request_id": request_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "credential_key": credential_key,
                    "purpose": description.strip(),
                    "description": description.strip(),
                    "fields": normalized,
                    "status": "pending",
                    "created_at": now,
                    "completed_at": None,
                    "external_event_id": None,
                }
                self._atomic_json(target, request)
        question_id = f"credential:{request_id}"
        return {
            "project_id": project_id,
            "credential_key": credential_key,
            "request_id": request_id,
            "question": {
                "question_id": question_id,
                "question_text": description.strip() or f"请填写凭据 {credential_key}",
                "required": True,
                "input_schema": {
                    "kind": "project_credential",
                    "request_id": request_id,
                    "credential_key": credential_key,
                    "fields": normalized,
                },
            },
            "status": str(request.get("status") or "pending"),
        }

    def complete_request(
        self, *, project_id: str, request_id: str,
        fields: dict[str, Any], external_event_id: str,
    ) -> dict[str, Any]:
        target = self.request_root(project_id) / f"{request_id}.json"
        if not target.is_file():
            raise FileNotFoundError("Project credential request was not found")
        with self._lock(target):
            request = json.loads(target.read_text(encoding="utf-8"))
            if (
                request.get("schema_version") != REQUEST_SCHEMA_VERSION
                or request.get("project_id") != project_id
                or request.get("request_id") != request_id
            ):
                raise ValueError("Project credential request does not match")
            if request.get("status") == "completed":
                return {
                    "project_id": project_id,
                    "credential_key": str(request["credential_key"]),
                    "field_names": [str(item["name"]) for item in request["fields"]],
                    "status": "already_saved",
                }
            expected = [str(item["name"]) for item in request.get("fields") or []]
            required = [
                str(item["name"]) for item in request.get("fields") or []
                if bool(item.get("required", True))
            ]
            submitted = {str(key): str(value) for key, value in fields.items()}
            missing = [name for name in required if not submitted.get(name)]
            unknown = sorted(set(submitted) - set(expected))
            if missing:
                raise ValueError(f"credential form is missing required fields: {missing}")
            if unknown:
                raise ValueError(f"credential form contains unexpected fields: {unknown}")
            credential_path = self.path(project_id)
            with self._lock(credential_path):
                document = (
                    self._load_path(credential_path, project_id)
                    if credential_path.exists() else self._empty(project_id)
                )
                key = str(request["credential_key"])
                prior = document["credentials"].get(key) or {}
                now = datetime.now(timezone.utc).isoformat()
                purpose = str(request.get("purpose") or request.get("description") or "")
                requested_fields = {
                    str(item["name"]): item for item in request.get("fields") or []
                }
                document["credentials"][key] = {
                    "credential_key": key,
                    "purpose": purpose,
                    "description": purpose,
                    "status": "active",
                    "revision": int(prior.get("revision") or 0) + 1,
                    "values": {
                        name: {
                            "value": submitted[name],
                            "purpose": str(
                                requested_fields[name].get("label") or name
                            ).strip(),
                            "env": name,
                        }
                        for name in expected if submitted.get(name)
                    },
                    "created_at": prior.get("created_at") or now,
                    "updated_at": now,
                    "last_request_id": request_id,
                    "last_external_event_id": external_event_id,
                }
                document["updated_at"] = now
                self._atomic_json(credential_path, document)
            request.update({
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "external_event_id": external_event_id,
            })
            self._atomic_json(target, request)
        return {
            "project_id": project_id,
            "credential_key": str(request["credential_key"]),
            "field_names": expected,
            "status": "saved",
        }

    def delete(self, project_id: str, credential_key: str) -> dict[str, Any]:
        if not CREDENTIAL_KEY.fullmatch(credential_key):
            raise ValueError("credential_key must be a safe name")
        target = self.ensure(project_id)
        with self._lock(target):
            payload = self._load_path(target, project_id)
            removed = payload["credentials"].pop(credential_key, None)
            if removed is not None:
                payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._atomic_json(target, payload)
        return {
            "project_id": project_id,
            "credential_key": credential_key,
            "deleted": removed is not None,
        }
