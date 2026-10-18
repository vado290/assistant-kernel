"""Project credentials (docs/ROUND5_PLAN.md D8, §5.5): one JSON file per project, `runtime/credentials.json`.

Each item: {key, value, service, purpose, used_by, created_at, updated_at, source}. The key is named by the model
(e.g. `binance_api`); value is a string or an object (username / password ...). Look first, ask only when missing:
the PM asks with a question marked `credential`, and Kernel stores the answer here instead of in the conversation.
Isolation is per project only (the user's choice): values are not hidden from the models that need them.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "project-credentials.v1"
KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
META = ("key", "service", "purpose", "used_by", "created_at", "updated_at", "source")


def path(vault: Path) -> Path:
    return vault / "runtime" / "credentials.json"


@contextmanager
def _locked(vault: Path):
    target = path(vault)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_name("credentials.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield target
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(target: Path) -> list[dict[str, Any]]:
    if not target.is_file():
        return []
    data = json.loads(target.read_text("utf-8") or "{}")
    return [dict(item) for item in data.get("items") or [] if isinstance(item, dict) and item.get("key")]


def _write(target: Path, items: list[dict[str, Any]]) -> None:
    temporary = target.with_name("credentials.json.tmp")
    temporary.write_text(json.dumps({"schema_version": SCHEMA, "items": items}, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def listing(vault: Path) -> list[dict[str, Any]]:
    """Every credential without its value."""
    with _locked(vault) as target:
        return [{k: item.get(k) for k in META} for item in _read(target)]


def get(vault: Path, key: str) -> dict[str, Any] | None:
    with _locked(vault) as target:
        return next((item for item in _read(target) if item["key"] == key), None)


def put(vault: Path, key: str, value: Any, *, service: str = "", purpose: str = "", used_by: list[str] | None = None,
        source: str = "user") -> dict[str, Any]:
    """Store or replace one credential; the key must be lower_snake_case."""
    if not KEY.fullmatch(key):
        raise ValueError(f"凭据 key 只能是小写字母、数字和下划线，以字母开头：{key!r}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _locked(vault) as target:
        items = _read(target)
        old = next((item for item in items if item["key"] == key), None)
        item = {"key": key, "value": value, "service": service or (old or {}).get("service", ""),
                "purpose": purpose or (old or {}).get("purpose", ""), "used_by": used_by or (old or {}).get("used_by", []),
                "created_at": (old or {}).get("created_at", now), "updated_at": now, "source": source}
        _write(target, [i for i in items if i["key"] != key] + [item])
        return {k: item[k] for k in META}


def delete(vault: Path, key: str) -> bool:
    with _locked(vault) as target:
        items = _read(target)
        kept = [item for item in items if item["key"] != key]
        if len(kept) == len(items):
            return False
        _write(target, kept)
        return True


def render(items: list[dict[str, Any]]) -> str:
    if not items:
        return "本项目还没有保存任何凭据。"
    lines = ["本项目保存的凭据（不显示值）："]
    lines += [f"- `{i['key']}`：{i.get('service') or '—'} · {i.get('purpose') or '—'}（更新于 {str(i.get('updated_at'))[:10]}）"
              for i in items]
    lines.append("删除：/secrets delete <key>（会要求确认）。新增：由 PM 在需要时提问，你在卡片上填写。")
    return "\n".join(lines)
