from __future__ import annotations

import re
from pathlib import Path

from assistant_kernel.mcp_tools.context import AgentMcpContext


DESCRIPTION = re.compile(r"^description:\s*[\"']?(.*?)[\"']?\s*$", re.MULTILINE)


def register(server, context: AgentMcpContext) -> None:
    if not context.allows("search_project_capabilities"):
        return

    @server.tool()
    async def search_project_capabilities(query: str, limit: int | None = None) -> dict:
        if not query.strip():
            raise ValueError("query cannot be blank")
        configured = int(context.defaults("search_project_capabilities").get("limit", 20))
        count = min(max(int(limit or configured), 1), configured, 100)
        async with context.audit("search_project_capabilities", {"query": query, "limit": count}):
            candidates = []
            roots = [
                ("skill", context.root / ".agents" / "skills", "SKILL.md"),
                ("script", context.root / "capabilities" / "scripts", "*.py"),
                ("script", context.root / "scripts", "*.py"),
                ("workflow", context.root / "capabilities" / "workflows", "*"),
            ]
            needle = query.casefold()
            for kind, root, pattern in roots:
                if not root.is_dir():
                    continue
                paths = root.glob(f"*/{pattern}") if kind == "skill" else root.rglob(pattern)
                for path in paths:
                    if not path.is_file() or path.stat().st_size > 1024 * 1024:
                        continue
                    relative = path.relative_to(context.root).as_posix()
                    try:
                        context.resolve_read(relative)
                        content = path.read_text(encoding="utf-8")
                    except (PermissionError, UnicodeDecodeError, OSError):
                        continue
                    description = ""
                    if kind == "skill":
                        match = DESCRIPTION.search(content[:10000])
                        description = match.group(1).strip() if match else ""
                    haystack = f"{relative}\n{description}\n{content[:20000]}".casefold()
                    if needle not in haystack:
                        continue
                    candidates.append(
                        {
                            "asset_type": kind,
                            "name": path.parent.name if kind == "skill" else path.stem,
                            "path": relative,
                            "description": description,
                            "match_reason": "query text found in path, description or content",
                        }
                    )
                    if len(candidates) >= count:
                        return {"candidates": candidates, "limit": count}
            return {"candidates": candidates, "limit": count}

