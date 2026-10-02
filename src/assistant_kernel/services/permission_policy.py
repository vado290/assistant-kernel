"""Compile one immutable Codex local-command policy from a Run's Scope snapshot."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

from assistant_kernel.services.workspace_scope import AuthorizedRoot


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _excluded(relative: str, patterns: Iterable[str]) -> bool:
    normalized = relative.replace("\\", "/").casefold()
    for pattern in patterns:
        candidate = pattern.casefold()
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
        if "**/" in candidate and fnmatch.fnmatchcase(
            normalized, candidate.replace("**/", "", 1)
        ):
            return True
    return False


class PermissionPolicyCompiler:
    """Codex permission profiles are a native-command boundary, not MCP grants."""

    def __init__(self, codex_runtime_root: Path | None = None):
        self.codex_runtime_root = codex_runtime_root

    @staticmethod
    def current_runtime_root(workspace_root: Path) -> Path | None:
        pointer = workspace_root / ".local" / "codex.current"
        value = pointer.read_text(encoding="utf-8").splitlines()[0].strip() if pointer.is_file() else ""
        selected = Path(value) if value.startswith("/") else workspace_root / ".local" / value
        if not value or not selected.is_dir():
            selected = workspace_root / ".local" / "codex"
        if not selected.is_dir():
            return None
        resolved = selected.resolve()
        if not resolved.is_dir() or workspace_root.resolve() not in resolved.parents:
            raise PermissionError("Codex runtime root is outside the AI workspace")
        return resolved

    @staticmethod
    def profile_name(run_id: str) -> str:
        return "kernel_run_" + run_id.replace("-", "_")

    @staticmethod
    def _existing_denials(root: Path, patterns: tuple[str, ...]) -> list[str]:
        """Use exact existing targets so bwrap never creates synthetic masks.

        Codex's Linux glob expansion has a bounded match budget. A broad legacy
        grant that exceeds it is rejected instead of silently losing excludes.
        """
        denied: list[str] = []
        scanned = 0
        stack = [root]
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > 100_000:
                        raise ValueError("native Scope scan exceeded 100000 entries")
                    relative = Path(entry.path).relative_to(root).as_posix()
                    # A symlink target is governed by the target root's rule or
                    # by :root=deny.  Adding a deny on the link itself can make
                    # bwrap reject a writable root before Codex starts.
                    if entry.is_symlink():
                        continue
                    if _excluded(relative, patterns):
                        denied.append(relative)
                        if len(denied) > 8_000:
                            raise ValueError("native Scope has more than 8000 denied paths; narrow it")
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        return sorted(denied)

    def compile(
        self, run_id: str, roots: list[AuthorizedRoot], *, network_enabled: bool = True,
    ) -> tuple[str, bool]:
        profile = self.profile_name(run_id)
        lines = [
            f'default_permissions = {_toml_string(profile)}',
            'approval_policy = "never"',
            f'[permissions.{profile}.filesystem]',
            '":root" = "deny"',
            '":minimal" = "read"',
            'glob_scan_max_depth = 12',
        ]
        if self.codex_runtime_root is not None:
            runtime_root = self.codex_runtime_root.resolve()
            if not runtime_root.is_dir():
                raise PermissionError("Codex runtime root is unavailable")
            lines.append(f'{_toml_string(str(runtime_root))} = "read"')
        root_rules: list[tuple[str, list[tuple[str, str]]]] = []
        may_write = False
        for root in roots:
            if root.native_mode == "mcp_only":
                continue
            resolved = root.path.resolve()
            if resolved != root.path or not resolved.is_dir():
                raise PermissionError(f"Scope root is unavailable or redirected: {root.key}")
            access = "write" if root.access == "write" and root.write_mode == "direct" else "read"
            if root.key == "candidate" and root.access == "write":
                access = "write"
            # A denied directory also denies its descendants.  Passing both
            # ``.git`` and ``.git/**`` to bwrap can make it try to create the
            # same mask once as a file and once as a directory.
            normalized_excludes = tuple(dict.fromkeys(
                pattern[:-3] if pattern.endswith("/**") else pattern
                for pattern in root.excludes
            ))
            excludes = tuple(normalized_excludes)
            if root.includes and set(root.includes) != {"**/*"}:
                if access == "write":
                    raise ValueError(
                        "native writable Scope requires an enforceable directory Root; "
                        "exact-file includes are MCP-only on this Codex runtime"
                    )
                rules: list[tuple[str, str]] = [(".", "deny")]
                for include in root.includes:
                    if any(char in include for char in "*?[]") or include.startswith(("/", "\\")):
                        raise ValueError(f"native Scope cannot enforce include pattern: {include}")
                    relative = Path(include)
                    if ".." in relative.parts or not relative.parts:
                        raise ValueError("native Scope include escapes its root")
                    target = (root.path / relative).resolve()
                    if root.path not in target.parents and target != root.path:
                        raise PermissionError("native Scope include follows a symlink outside its root")
                    if _excluded(relative.as_posix(), excludes) or _excluded(
                        target.relative_to(root.path).as_posix(), excludes
                    ):
                        raise PermissionError("native Scope include is excluded")
                    rules.append((relative.as_posix(), access))
                root_rules.append((str(root.path), rules))
            else:
                denials = self._existing_denials(root.path, excludes) if excludes else []
                root_rules.append((str(root.path), [
                    (".", access), *((path, "deny") for path in denials)
                ]))
            may_write = may_write or access == "write"
        for path, rules in root_rules:
            lines.append(f'[permissions.{profile}.filesystem.{_toml_string(path)}]')
            for relative, access in rules:
                lines.append(f'{_toml_string(relative)} = {_toml_string(access)}')
        lines.extend([
            f'[permissions.{profile}.network]',
            f'enabled = {str(network_enabled).lower()}',
            '[shell_environment_policy]',
            'inherit = "core"',
        ])
        run_work = next((item.path for item in roots if item.key == "run_work"), None)
        if run_work is not None:
            lines.extend([
                '[shell_environment_policy.set]',
                f'HOME = {_toml_string(str(run_work))}',
                f'TMPDIR = {_toml_string(str(run_work))}',
            ])
        lines.extend([
            '[shell_environment_policy.filters]',
            '"POSTGRES_*" = "exclude"',
            '"ASSISTANT_*" = "exclude"',
            '"KERNEL_SCOPE_*" = "exclude"',
            '"RAG_ENV_FILE" = "exclude"',
            '',
        ])
        return "\n".join(lines), may_write

    def materialize(
        self, codex_home: Path, run_id: str, roots: list[AuthorizedRoot],
        *, network_enabled: bool = True,
    ) -> tuple[str, str, bool]:
        text, may_write = self.compile(
            run_id, roots, network_enabled=network_enabled,
        )
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        profile = self.profile_name(run_id)
        target = codex_home / f"{profile}.config.toml"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("pinned Codex permission profile changed")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(target)
        return profile, digest, may_write
