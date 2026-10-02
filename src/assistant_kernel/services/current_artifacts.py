"""Atomically maintained current Task artifacts; raw history remains in Kernel state."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


CURRENT_PATHS = {
    "requirements": Path("artifacts/requirements.json"),
    "implementation": Path("artifacts/implementation.json"),
    "operation": Path("artifacts/operation.json"),
    "state": Path("state/current.json"),
    "prompt": Path("prompt/current.json"),
    "handoff": Path("handoff/current.json"),
    "supervisor": Path("supervisor/current.json"),
}


class CurrentArtifactService:
    """Keep one effective document per Task concern with compare-and-swap writes."""

    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _path(self, project_id: str, task_id: str, kind: str) -> tuple[Path, Path]:
        relative = CURRENT_PATHS.get(kind)
        if relative is None:
            raise ValueError(f"unsupported current artifact kind: {kind}")
        task_relative = Path("runs") / task_id / relative
        target = self.runtime.ensure(project_id).vault_root / task_relative
        return target, task_relative

    @staticmethod
    @contextmanager
    def _lock(target: Path):
        """Serialize cross-process current-file compare-and-replace on Linux."""
        lock_path = target.with_suffix(target.suffix + ".lock")
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

    def read(self, project_id: str, task_id: str, kind: str) -> dict[str, Any] | None:
        target, relative = self._path(project_id, task_id, kind)
        if not target.is_file():
            return None
        encoded = target.read_bytes()
        payload = json.loads(encoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"current {kind} artifact must be an object")
        return {
            "kind": kind,
            "uri": f"vault://{project_id}/{relative.as_posix()}",
            "sha256": self._sha256(encoded),
            "size": len(encoded),
            "payload": payload,
        }

    def write(
        self, project_id: str, task_id: str, kind: str, payload: dict[str, Any], *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("current artifact payload must be an object")
        target, relative = self._path(project_id, task_id, kind)
        with self._lock(target):
            current = self.read(project_id, task_id, kind)
            current_sha = str((current or {}).get("sha256") or "") or None
            if expected_sha256 is not None and current_sha != expected_sha256:
                raise RuntimeError(f"current {kind} artifact changed during update")
            current_payload = dict((current or {}).get("payload") or {})
            current_payload.pop("updated_at", None)
            desired_payload = {**payload, "task_id": task_id}
            desired_payload.pop("updated_at", None)
            if current is not None and current_payload == desired_payload:
                return current
            document = {
                **desired_payload,
                "task_id": task_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            encoded = json.dumps(
                document, ensure_ascii=False, sort_keys=True, indent=2,
            ).encode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(
                target.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_bytes(encoded)
            temporary.replace(target)
        return {
            "kind": kind,
            "uri": f"vault://{project_id}/{relative.as_posix()}",
            "sha256": self._sha256(encoded),
            "size": len(encoded),
            "payload": document,
        }

    def view(self, project_id: str, task_id: str) -> dict[str, Any]:
        return {
            kind: self.read(project_id, task_id, kind)
            for kind in CURRENT_PATHS
        }
