"""Scalable before/after evidence for Runs allowed to change shared files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from assistant_kernel.services.workspace_scope import AuthorizedRoot, WorkspaceScopeService


MAX_FILES = 10_000
MAX_DIRECTORIES = 20_000
MAX_BYTES = 500 * 1024 * 1024


class FileEffectRecorder:
    def __init__(
        self, vault_root: Path, run_id: str, invocation_id: str,
        roots: list[AuthorizedRoot], protected_backup_root: Path | None = None,
    ):
        self.vault_root = vault_root
        self.run_id = run_id
        self.root = vault_root / "runs" / run_id / "file-effects" / invocation_id
        self.protected_backup_root = protected_backup_root or self.root / "before"
        self.roots = [
            item for item in roots
            if item.access == "write" and item.write_mode == "direct"
            and item.native_mode == "enforced"
            and not item.recursive
            and item.key not in {"project", "candidate", "run_work"}
        ]
        self.before: dict[str, dict] = {}
        self.before_stats: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _scan(self, *, backup: bool) -> dict[str, dict]:
        result: dict[str, dict] = {}
        count = 0
        directories = 0
        total = 0
        backed_up_files = 0
        backed_up_bytes = 0
        metadata_only_files = 0
        for root in self.roots:
            stack = [root.path]
            while stack:
                parent = stack.pop()
                with os.scandir(parent) as entries:
                    for entry in entries:
                        relative = Path(entry.path).relative_to(root.path).as_posix()
                        symlink = entry.is_symlink()
                        directory = entry.is_dir(follow_symlinks=False) if not symlink else False
                        if WorkspaceScopeService._root_excluded(
                            root, relative, is_directory=directory
                        ):
                            continue
                        key = f"{root.key}/{relative}"
                        if symlink:
                            count += 1
                            link_target = os.readlink(entry.path)
                            result[key] = {
                                "kind": "symlink", "target": link_target,
                                "sha256": hashlib.sha256(link_target.encode()).hexdigest(),
                                "grant_id": root.grant_id,
                            }
                            continue
                        if directory:
                            directories += 1
                            result[key] = {
                                "kind": "directory", "grant_id": root.grant_id,
                            }
                            if backup:
                                (self.protected_backup_root / root.key / relative).mkdir(
                                    parents=True, exist_ok=True
                                )
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        count += 1
                        stat = entry.stat(follow_symlinks=False)
                        total += stat.st_size
                        path = Path(entry.path)
                        should_backup = (
                            backup and backed_up_files < MAX_FILES
                            and backed_up_bytes + stat.st_size <= MAX_BYTES
                        )
                        digest = self._sha256(path) if should_backup else None
                        if should_backup:
                            target = self.protected_backup_root / root.key / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                target.parent.chmod(0o700)
                            except OSError:
                                pass
                            with path.open("rb") as source, target.open("wb") as output:
                                while chunk := source.read(1024 * 1024):
                                    output.write(chunk)
                            if self._sha256(target) != digest:
                                raise RuntimeError("file-effect backup differs from the source")
                            try:
                                target.chmod(0o600)
                            except OSError:
                                pass
                            backed_up_files += 1
                            backed_up_bytes += stat.st_size
                        elif backup:
                            metadata_only_files += 1
                        record = {
                            "kind": "file", "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns, "grant_id": root.grant_id,
                            "baseline": "content_backup" if should_backup else "metadata_only",
                        }
                        if digest is not None:
                            record["sha256"] = digest
                        result[key] = record
        if backup:
            self.before_stats = {
                "file_count": count,
                "directory_count": directories,
                "total_bytes": total,
                "backed_up_file_count": backed_up_files,
                "backed_up_bytes": backed_up_bytes,
                "metadata_only_file_count": metadata_only_files,
            }
        return result

    def capture_before(self) -> None:
        if not self.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.before = self._scan(backup=True)
        target = self.root / "before.json"
        target.write_text(json.dumps(self.before, sort_keys=True, indent=2), encoding="utf-8")

    def capture_after(self) -> dict | None:
        if not self.enabled:
            return None
        after = self._scan(backup=False)
        changes = []
        for path in sorted(set(self.before) | set(after)):
            before = self.before.get(path)
            current = after.get(path)
            if before is not None and current is not None and before.get("kind") == current.get("kind"):
                kind = before.get("kind")
                if kind == "directory":
                    continue
                if kind == "symlink" and before.get("target") == current.get("target"):
                    continue
                if kind == "file" and (
                    before.get("size"), before.get("mtime_ns")
                ) == (
                    current.get("size"), current.get("mtime_ns")
                ):
                    continue
            if current is not None and current.get("kind") == "file":
                root_key, relative = path.split("/", 1)
                root = next(item for item in self.roots if item.key == root_key)
                current = {**current, "sha256": self._sha256(root.path / relative)}
            changes.append({"path": path, "before": before, "after": current})
        payload = {
            "schema_version": "file-effects.v1",
            "run_id": self.run_id,
            "changes": changes,
            "before_file_count": len(self.before),
            "after_file_count": len(after),
            "baseline": dict(self.before_stats),
        }
        target = self.root / "changes.json"
        target.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        return {
            "uri": f"vault://{self.vault_root.name}/{target.relative_to(self.vault_root).as_posix()}",
            "changed_file_count": sum(
                1 for item in changes
                if (item["after"] or item["before"]).get("kind") != "directory"
            ),
            "changed_directory_count": sum(
                1 for item in changes
                if (item["after"] or item["before"]).get("kind") == "directory"
            ),
            "changed_entry_count": len(changes),
        }
