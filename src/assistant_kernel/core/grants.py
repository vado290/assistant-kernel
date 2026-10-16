"""Directory grants (docs/ROUND4_PLAN.md section 5): check a requested directory and build its grant policy.

A grant gives a project an external directory (read or write) for its workspace tools from the next run on.
It is requested in the creation charter (`directories`) or by the Supervisor (`propose_grant`), approved on a card,
and written by the state machine (`machine.grant_directory`). Checks here touch the filesystem; the machine does not.
Known limit: build turns run with full shell access, so write limits on the shell rely on the skills (A5 not done).
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from assistant_kernel.services.project_grants import policy_sha256

ACCESS = ("read", "write")


def visible_root() -> Path:
    """Only directories inside the container's mount of H:\\AI are visible to projects."""
    return Path(os.environ.get("CORE_GRANT_ROOT") or "/home/v/AI")


def container_path(raw: str) -> Path:
    """Accept both container paths and the user's Windows paths under H:\\AI."""
    text = str(raw or "").strip().strip('"')
    if re.match(r"^[A-Za-z]:[\\/]", text):
        parts = PureWindowsPath(text).parts
        if parts[0].upper().startswith("H:") and len(parts) > 1 and parts[1].lower() == "ai":
            return visible_root().joinpath(*parts[2:])
        raise ValueError(f"{text} 不在 H:\\AI 下，容器里看不到；需要先在 docker-compose 中挂载该目录")
    return Path(text)


def check_directory(raw: Any, project_id: str, data_root: Path) -> dict[str, str]:
    """A requested directory as {path, access, purpose, key}; raises ValueError with the reason it is refused."""
    if not isinstance(raw, dict):
        raise ValueError("目录授权必须写成对象：{path, access, purpose}")
    path = container_path(raw.get("path"))
    access, purpose = str(raw.get("access") or "read"), str(raw.get("purpose") or "").strip()
    if access not in ACCESS:
        raise ValueError(f"access 只能是 {ACCESS}")
    if not purpose:
        raise ValueError(f"请写明授权 {path} 的用途")
    root = visible_root().resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"{path} 不在 {root} 下，容器里看不到；需要先在 docker-compose 中挂载该目录")
    if not resolved.is_dir():
        raise ValueError(f"目录不存在：{path}")
    data = data_root.resolve()
    for platform_dir in ("runtime", "config", "system", "backups", "workspaces"):
        if resolved == data / platform_dir or (data / platform_dir) in resolved.parents:
            raise ValueError(f"{path} 是平台的运行目录，不能授权给项目")
    vaults = data / "vaults"
    if resolved == data or resolved == vaults or (vaults in resolved.parents and resolved.relative_to(vaults).parts[0] != project_id):
        raise ValueError(f"{path} 属于其他项目的 vault，不能授权")
    key = re.sub(r"[^a-z0-9-]+", "-", resolved.name.lower()).strip("-")[:40] or "dir"
    return {"path": str(resolved), "access": access, "purpose": purpose[:300], "key": key}


def policy(directory: dict[str, str]) -> tuple[dict[str, Any], str]:
    """The grant policy the workspace scope reads (same shape as the earlier hand-made grants) and its hash."""
    write = directory["access"] == "write"
    body = {"key": directory["key"], "path": directory["path"], "access": directory["access"], "publish": write,
            "write_mode": "direct", "description": directory["purpose"], "recursive": True, "includes": [],
            "excludes": [], "native_mode": "enforced", "sharing": "all_project_roles"}
    return body, policy_sha256(body)


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "本项目没有额外授权的目录（只能访问自己的 vault）。"
    lines = ["本项目授权的目录："]
    for r in rows:
        p = r["policy_json"]
        lines.append(f"- `{r['resource_key']}`：{p.get('path')}（{'可写' if p.get('access') == 'write' else '只读'}）"
                     f" — {p.get('description') or ''}")
    lines.append("撤销：/grants revoke <key>（会要求确认）。新增：在 Supervisor 频道说明目录、读或写、用途。")
    return "\n".join(lines)
