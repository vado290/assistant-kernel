"""Directory grants (docs/ROUND4_PLAN.md section 5): check a requested directory and build its grant policy.

A grant gives a project an external directory (read or write) for its workspace tools from the next run on.
It is requested in the creation charter (`directories`) or by the Supervisor (`propose_grant`), approved on a card,
and written by the state machine (`machine.grant_directory`). Checks here touch the filesystem; the machine does not.
Known limit: build turns run with full shell access, so write limits on the shell rely on the skills (A5 not done).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from assistant_kernel.services.project_grants import policy_sha256

ACCESS = ("read", "write")
RESERVED_KEYS = {"project", "candidate", "run-work"}


def distinct_key(directory: dict[str, str], active_path: str | None) -> dict[str, str]:
    """The directory with a key no other active grant uses: a different path under the same name gets a path hash."""
    if active_path is None or active_path == directory["path"]:
        return directory
    digest = hashlib.sha256(directory["path"].encode("utf-8")).hexdigest()[:6]
    return {**directory, "key": f"{directory['key'][:33]}-{digest}"}


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
        raise ValueError(f"{text} is not under H:\\AI, so the container cannot see it; mount the directory in docker-compose first")
    return Path(text)


def check_directory(raw: Any, project_id: str, data_root: Path) -> dict[str, str]:
    """A requested directory as {path, access, purpose, key}; raises ValueError with the reason it is refused."""
    if not isinstance(raw, dict):
        raise ValueError("a directory grant is an object: {path, access, purpose}")
    path = container_path(raw.get("path"))
    access, purpose = str(raw.get("access") or "read"), str(raw.get("purpose") or "").strip()
    if access not in ACCESS:
        raise ValueError(f"access must be one of {ACCESS}")
    if not purpose:
        raise ValueError(f"give the purpose of granting {path}")
    root = visible_root().resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"{path} is not under {root}, so the container cannot see it; mount the directory in docker-compose first")
    if not resolved.is_dir():
        raise ValueError(f"directory does not exist: {path}")
    data = data_root.resolve()
    for platform_dir in ("runtime", "config", "system", "backups", "workspaces"):
        if resolved == data / platform_dir or (data / platform_dir) in resolved.parents:
            raise ValueError(f"{path} is the platform's runtime directory and cannot be granted to a project")
    vaults = data / "vaults"
    if resolved == data or resolved == vaults or (vaults in resolved.parents and resolved.relative_to(vaults).parts[0] != project_id):
        raise ValueError(f"{path} belongs to another project's vault and cannot be granted")
    key = re.sub(r"[^a-z0-9-]+", "-", resolved.name.lower()).strip("-")[:40] or "dir"
    if key in RESERVED_KEYS:   # the workspace's own roots
        key = f"dir-{key}"
    return {"path": str(resolved), "access": access, "purpose": purpose[:300], "key": key}


def policy(directory: dict[str, str]) -> tuple[dict[str, Any], str]:
    """The grant policy the workspace scope reads (same shape as the earlier hand-made grants) and its hash."""
    write = directory["access"] == "write"
    body = {"key": directory["key"], "path": directory["path"], "access": directory["access"], "publish": write,
            "write_mode": "direct", "description": directory["purpose"], "recursive": True, "includes": [],
            "excludes": [], "native_mode": "enforced", "sharing": "all_project_roles"}
    return body, policy_sha256(body)


def render(rows: list[dict[str, Any]]) -> str:
    from assistant_kernel.core.i18n import t
    if not rows:
        return t("grants.none")
    lines = [t("grants.title")]
    for r in rows:
        p = r["policy_json"]
        access = t("card.access_write" if p.get("access") == "write" else "card.access_read")
        lines.append(t("grants.line", key=r["resource_key"], path=p.get("path"), access=access,
                       purpose=p.get("description") or ""))
    lines.append(t("grants.footer"))
    return "\n".join(lines)
