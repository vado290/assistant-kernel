from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from assistant_kernel.mcp_tools.context import AgentMcpContext


def register(server, context: AgentMcpContext) -> None:
    if context.allows("workspace_list"):
        @server.tool()
        async def workspace_list(relative_path: str = "", limit: int = 200) -> dict:
            limit = min(max(limit, 1), int(context.defaults("workspace_list").get("limit", 1000)), 1000)
            async with context.audit("workspace_list", {"relative_path": relative_path, "limit": limit}):
                root = context.resolve_read(relative_path)
                if not root.exists():
                    return {"files": []}
                if not root.is_dir():
                    raise ValueError("relative_path must identify a directory")
                files = []
                for item in sorted(root.rglob("*")):
                    if not item.is_file():
                        continue
                    try:
                        context.resolve_read(item.relative_to(context.root).as_posix())
                    except PermissionError:
                        continue
                    files.append(item.relative_to(context.root).as_posix())
                    if len(files) >= limit:
                        break
                return {"files": files, "limit": limit}

    if context.allows("workspace_read_text"):
        @server.tool()
        async def workspace_read_text(relative_path: str, max_bytes: int | None = None) -> dict:
            configured = int(context.defaults("workspace_read_text").get("max_bytes", 262144))
            limit = min(max(int(max_bytes or configured), 1), 2 * 1024 * 1024)
            target_uri = f"vault://{context.document.runtime_project_id}/{relative_path}"
            async with context.audit("workspace_read_text", {"relative_path": relative_path, "max_bytes": limit}, target_uri):
                target = context.resolve_read(relative_path)
                if not target.is_file():
                    raise FileNotFoundError(relative_path)
                data = target.read_bytes()
                if len(data) > limit:
                    raise ValueError(f"file exceeds max_bytes={limit}")
                return {
                    "uri": target_uri,
                    "text": data.decode("utf-8"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }

    if context.allows("workspace_search"):
        @server.tool()
        async def workspace_search(query: str, relative_path: str = "", limit: int | None = None) -> dict:
            if not query.strip():
                raise ValueError("query cannot be blank")
            configured = int(context.defaults("workspace_search").get("limit", 50))
            count = min(max(int(limit or configured), 1), 200)
            async with context.audit("workspace_search", {"query": query, "relative_path": relative_path, "limit": count}):
                root = context.resolve_read(relative_path)
                if not root.is_dir():
                    raise ValueError("relative_path must identify a directory")
                matches = []
                scanned = 0
                for item in sorted(root.rglob("*")):
                    if not item.is_file() or item.stat().st_size > 2 * 1024 * 1024:
                        continue
                    try:
                        relative = item.relative_to(context.root).as_posix()
                        context.resolve_read(relative)
                        text = item.read_text(encoding="utf-8")
                    except (PermissionError, UnicodeDecodeError, OSError):
                        continue
                    scanned += 1
                    for line_no, line in enumerate(text.splitlines(), 1):
                        if query.casefold() in line.casefold():
                            matches.append({"path": relative, "line": line_no, "text": line[:1000]})
                            if len(matches) >= count:
                                return {"matches": matches, "limit": count, "scanned_files": scanned}
                return {"matches": matches, "limit": count, "scanned_files": scanned}

    if context.allows("workspace_write_text"):
        @server.tool()
        async def workspace_write_text(
            relative_path: str,
            content: str,
            expected_sha256: str | None = None,
            create_only: bool = False,
        ) -> dict:
            configured = int(context.defaults("workspace_write_text").get("max_bytes", 1048576))
            data = content.encode("utf-8")
            if len(data) > min(configured, 8 * 1024 * 1024):
                raise ValueError(f"content exceeds max_bytes={configured}")
            target_uri = f"vault://{context.document.runtime_project_id}/{relative_path}"
            async with context.audit(
                "workspace_write_text",
                {"relative_path": relative_path, "expected_sha256": expected_sha256, "create_only": create_only, "bytes": len(data)},
                target_uri,
            ) as audit:
                target = context.resolve_write(relative_path)
                if target.exists():
                    if create_only:
                        raise FileExistsError(relative_path)
                    previous = hashlib.sha256(target.read_bytes()).hexdigest()
                    audit.previous_sha256 = previous
                    if not expected_sha256 or expected_sha256 != previous:
                        raise ValueError("expected_sha256 must match the current file before overwrite")
                elif expected_sha256:
                    raise ValueError("expected_sha256 was provided for a missing file")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                digest = hashlib.sha256(data).hexdigest()
                audit.result_sha256 = digest
                return {"uri": target_uri, "sha256": digest, "bytes": len(data)}
