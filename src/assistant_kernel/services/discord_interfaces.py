from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from assistant_kernel.config import KernelSettings


class DiscordInterfaceRegistry:
    """Small non-secret map for role-specific Discord Project interfaces."""

    def __init__(self, settings: KernelSettings):
        self.path = settings.data_root / "config" / "discord-interfaces.json"

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": "discord-interfaces.v1", "interfaces": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "discord-interfaces.v1":
            raise ValueError("unsupported Discord interface registry schema")
        interfaces = payload.get("interfaces") or {}
        if not isinstance(interfaces, dict):
            raise ValueError("Discord interface registry interfaces must be an object")
        return {**payload, "interfaces": interfaces}

    @contextmanager
    def _lock(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
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

    def upsert(
        self, *, channel_id: str, guild_id: str, project_id: str, role: str,
    ) -> dict[str, Any]:
        if role not in {"supervisor", "operator"}:
            raise ValueError("unsupported Discord Project interface role")
        with self._lock():
            payload = self.read()
            interfaces = dict(payload["interfaces"])
            interfaces[str(channel_id)] = {
                "guild_id": str(guild_id),
                "project_id": str(project_id),
                "role": role,
            }
            updated = {
                "schema_version": "discord-interfaces.v1",
                "interfaces": interfaces,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(
                self.path.suffix + f".{uuid.uuid4().hex}.tmp"
            )
            temporary.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(self.path)
            return dict(interfaces[str(channel_id)])

    def resolve(self, channel_id: str) -> dict[str, Any] | None:
        value = self.read()["interfaces"].get(str(channel_id))
        if not isinstance(value, dict):
            return None
        project_id = str(value.get("project_id") or "").strip()
        role = str(value.get("role") or "").strip()
        if not project_id or role not in {"supervisor", "operator"}:
            raise ValueError("Discord interface entry is incomplete")
        return {
            **value,
            "channel_id": str(channel_id),
            "project_id": project_id,
            "role": role,
        }

    def find(self, *, project_id: str, role: str) -> dict[str, Any] | None:
        """Resolve a role interface without leaking the registry storage shape.

        The v1 registry intentionally stores ``channel_id`` as the mapping key.
        Callers must use this method instead of assuming it is duplicated inside
        the entry payload.
        """
        if role not in {"supervisor", "operator"}:
            raise ValueError("unsupported Discord Project interface role")
        for channel_id, value in self.read()["interfaces"].items():
            if not isinstance(value, dict):
                continue
            if (
                str(value.get("project_id") or "") == str(project_id)
                and str(value.get("role") or "") == role
            ):
                return {
                    **value,
                    "channel_id": str(channel_id),
                    "project_id": str(project_id),
                    "role": role,
                }
        return None
