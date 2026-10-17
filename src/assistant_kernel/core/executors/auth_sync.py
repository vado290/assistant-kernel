"""Keep every project's Codex login in step with one master copy.

All projects use the same ChatGPT login, whose refresh tokens work only once: when one project's copy
refreshes, the other copies become invalid. So the newest auth.json always wins:
  before a model call  master → project  (if the master is newer)
  during and after it  project → master  (if Codex refreshed the project's copy; every 20 s while it runs)
Copies keep the file's mtime (copy2), so equal files are never copied back and forth.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from assistant_kernel.storage import ProjectRuntimeManager


def master_path(settings) -> Path:
    return settings.data_root / "system" / "codex-home-template" / "auth.json"


@contextmanager
def _locked(settings):
    lock = master_path(settings).with_name("auth.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _newer(source: Path, target: Path) -> bool:
    if not source.is_file() or not _complete(source):
        return False
    if not target.is_file():
        return True
    return source.stat().st_mtime > target.stat().st_mtime and source.read_bytes() != target.read_bytes()


def _complete(path: Path) -> bool:
    """A login file Codex is still writing is never copied (calls also sync while Codex runs)."""
    try:
        return isinstance(json.loads(path.read_text("utf-8")), dict)
    except (OSError, ValueError):
        return False


def _copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f"auth.json.sync-{os.getpid()}")
    shutil.copy2(source, temporary)
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def sync(settings, project_id: str, *, direction: str) -> bool:
    """direction 'in' (master → project) or 'out' (project → master); True when a copy was made."""
    local = ProjectRuntimeManager(settings.data_root).ensure(project_id).codex_home / "auth.json"
    master = master_path(settings)
    source, target = (master, local) if direction == "in" else (local, master)
    with _locked(settings):
        if _newer(source, target):
            _copy(source, target)
            return True
    return False
