"""Kernel tools for one PM / Supervisor model call (plan D3), served over stdio MCP.

Codex starts this module once per invocation (see ``harness/mcp_config.py``). The scope arrives in a
private, hash-checked JSON file: one project, one leased ``core.runs`` row and the task that owns the
candidate area. Every tool call re-checks the lease, so a cancelled or expired run cannot keep using
its tools. Each call is recorded in a receipt the harness checks after the turn.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import drafts
from assistant_kernel.core.db import Database, one
from assistant_kernel.storage import ProjectRuntimeManager

LOGGER = logging.getLogger("assistant_kernel.core.tools")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SCOPED_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DRAFT_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
MODEL_CALL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
HINTS = ((PermissionError, "权限或授权范围拒绝：只能访问本次执行授权的项目和目录。"),
         (FileNotFoundError, "目标文件、目录或记录不存在，或不在授权范围内。"),
         (KeyError, "引用了未知的键（root key、组件 id 等）。"),
         (ValueError, "参数或状态约束不满足：阅读 message 中的具体约束，修正后再重试。"),
         (TimeoutError, "执行超时：缩小范围后重试。"))


@dataclass
class Scope:
    settings: KernelSettings
    project_id: str
    run_id: str
    task_id: str
    claim_token: str
    stage: str | None
    allowed: set[str]
    roots: list[Any]
    receipt_path: Path | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    initialized: bool = False

    @classmethod
    def from_environment(cls, receipt_path: Path | None = None) -> "Scope":
        from assistant_kernel.services.workspace_scope import WorkspaceScopeService
        settings = KernelSettings.from_environment()
        env = os.environ
        project_id, run_id = env["KERNEL_SCOPE_PROJECT_ID"], env["KERNEL_SCOPE_RUN_ID"]
        task_id = env.get("KERNEL_SCOPE_TASK_ID") or run_id
        uri, sha = env.get("KERNEL_SCOPE_SNAPSHOT_URI", "").strip(), env.get("KERNEL_SCOPE_SNAPSHOT_SHA256", "").strip()
        if bool(uri) != bool(sha):
            raise ValueError("workspace scope snapshot URI and hash must be provided together")
        workspace = WorkspaceScopeService(settings)
        roots = (workspace.load_snapshot(project_id, task_id, None, uri, sha) if uri
                 else workspace.resolve(project_id, task_id, None))
        allowed = {t.strip() for t in env.get("KERNEL_SCOPE_ALLOWED_TOOLS", "").split(",") if t.strip()}
        return cls(settings=settings, project_id=project_id, run_id=run_id, task_id=task_id,
                   claim_token=env["KERNEL_SCOPE_CLAIM_TOKEN"], stage=env.get("KERNEL_SCOPE_STAGE") or None,
                   allowed=allowed, roots=roots, receipt_path=receipt_path)

    async def check_lease(self) -> None:
        async with Database(self.settings).tx() as c:
            held = await one(c, """SELECT 1 FROM core.runs WHERE run_id=%s AND claim_token=%s
                                   AND status='running' AND NOT cancel_requested""", (self.run_id, self.claim_token))
        if held is None:
            raise PermissionError("this invocation's run is no longer active")

    def save_receipt(self) -> None:
        if self.receipt_path is None:
            return
        temporary = self.receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.receipt, ensure_ascii=False, sort_keys=True), "utf-8")
        temporary.replace(self.receipt_path)


def error_payload(exc: BaseException, tool: str) -> str:
    hint = next((h for kind, h in HINTS if isinstance(exc, kind)), "未分类错误：查看本次调用的工具回执和服务日志。")
    return json.dumps({"ok": False, "tool": tool, "error_type": type(exc).__name__, "message": str(exc),
                       "hint": hint}, ensure_ascii=False, indent=2)


def build_server(scope: Scope) -> MCPServer:
    """Register only the tools this invocation was given; each call checks the lease first."""
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService

    settings, project_id, roots = scope.settings, scope.project_id, scope.roots
    runtime = ProjectRuntimeManager(settings.data_root)
    workspace = WorkspaceScopeService(settings)
    server = MCPServer(name="kernel-core-tools", instructions=(
        "These tools are fixed to the current Project and leased run. Targets outside that scope are "
        "rejected. Call get_authorized_workspace first. A successful tool result is not human approval."))

    def tool(annotations: ToolAnnotations, *, initializes: bool = False):
        def register(fn: Callable[..., Any]):
            name = fn.__name__
            if name not in scope.allowed:
                return fn

            @functools.wraps(fn)
            async def call(*args: Any, **kwargs: Any) -> Any:
                record = {"tool": name, "status": "started"}
                scope.receipt.setdefault("calls", []).append(record)
                try:
                    if not initializes and not scope.initialized:
                        raise PermissionError("call get_authorized_workspace before using scoped tools")
                    await scope.check_lease()
                    result = fn(*args, **kwargs)
                    if hasattr(result, "__await__"):
                        result = await result
                    record["status"] = "succeeded"
                    return result
                except Exception as exc:  # noqa: BLE001 - tool boundary: the model gets a structured error
                    LOGGER.exception("tool %s failed", name)
                    record.update(status="error", error_type=type(exc).__name__)
                    return error_payload(exc, name)
                finally:
                    scope.save_receipt()

            server.tool(annotations=annotations)(call)
            scope.receipt.setdefault("exposed", []).append(name)
            return fn
        return register

    def root_keys() -> set[str]:
        return {item.key for item in roots}

    @tool(READ_ONLY, initializes=True)
    def get_authorized_workspace(max_depth: int = 2, limit: int = 80, include_resource_guide: bool = False) -> dict:
        """Initialize this invocation and return the authorized roots with a compact file tree."""
        entries, truncated = workspace.tree(roots, max_depth=min(max_depth, 3), limit=min(limit, 100),
                                            selected_paths={}, soft_excludes={})
        scope.initialized = True
        writable = any(item.access == "write" for item in roots)
        result = {
            "project_id": project_id, "run_id": scope.run_id, "stage": scope.stage,
            "roots": [{
                "key": item.key, "path": str(item.path), "access": item.access,
                "native_access": "none" if item.key == "project" or item.native_mode == "mcp_only" else
                "write" if item.access == "write" and (item.key in {"candidate", "run_work"}
                                                       or item.write_mode == "direct") else "read",
                "mcp_access": item.access,
                "effective_access": "write" if item.access == "write" and item.key in {"candidate", "run_work"} else "read",
                "permission_package": item.permission_package,
                "description": item.description, "includes": list(item.includes), "excludes": list(item.excludes),
                "publish_allowed": item.publish_allowed, "grant_id": item.grant_id, "write_mode": item.write_mode,
                "native_mode": item.native_mode, "recursive": item.recursive} for item in roots],
            "file_tree": entries, "truncated": truncated, "allowed_tools": sorted(scope.allowed),
            "stage_permissions": {"workspace_file_access": "read_write_execute" if writable else "project_only",
                                  "formal_write": writable, "network": True},
        }
        if include_resource_guide:
            result["resource_guide"] = workspace.resource_guide(project_id)
        return result

    @tool(READ_ONLY)
    def list_authorized_tree(root_key: str = "project", relative_path: str = "", max_depth: int = 2,
                             limit: int = 200, include_soft_excluded: bool = False) -> dict:
        """Browse one authorized root/path on demand; this never expands authorization."""
        if root_key not in root_keys():
            raise PermissionError("workspace root is outside the authorized scope")
        entries, truncated = workspace.tree(roots, max_depth=max_depth, limit=limit,
                                            selected_paths={root_key: (relative_path,)}, soft_excludes={})
        return {"root_key": root_key, "relative_path": relative_path, "files": entries, "truncated": truncated}

    @tool(READ_ONLY)
    async def read_authorized_file(uri: str, max_bytes: int = 262144) -> dict:
        """Read one UTF-8 file addressed by an authorized workspace URI."""
        _, target, _ = workspace.resolve_uri(roots, uri)
        if not target.is_file():
            raise FileNotFoundError(uri)
        data = target.read_bytes()
        if len(data) > min(max(max_bytes, 1), 2 * 1024 * 1024):
            raise ValueError(f"file exceeds max_bytes={max_bytes}")
        digest = hashlib.sha256(data).hexdigest()
        return {"uri": uri, "text": data.decode("utf-8"), "sha256": digest, "size": len(data)}

    @tool(SCOPED_WRITE)
    def write_candidate_file(target_uri: str, content: str, expected_sha256: str | None = None,
                             expected_state: str | None = None) -> dict:
        """Write a candidate copy for an authorized formal target; never publish it."""
        result = workspace.write_candidate(roots, target_uri, content, expected_sha256, expected_state)
        relative = str(result["candidate_uri"])[len("workspace://candidate/"):]
        base = f"vault://{project_id}/runs/{scope.task_id}/candidate"
        return {**result, "candidate_vault_uri": f"{base}/{relative}", "manifest_vault_uri": f"{base}/manifest.json"}

    @tool(SCOPED_WRITE)
    def delete_candidate_file(target_uri: str, expected_sha256: str) -> dict:
        """Stage a hash-guarded deletion in this task's candidate manifest."""
        return workspace.delete_candidate(roots, target_uri, expected_sha256)

    @tool(READ_ONLY)
    def read_project_document(ref: dict, max_bytes: int = 262144) -> dict:
        """Read a Project document by its reference, with identity and byte checks."""
        from assistant_kernel.services.document_runtime import TaskDocumentService, validate_resource_ref
        normalized = validate_resource_ref(ref)
        prefix = f"vault://{project_id}/"
        if normalized["kind"] != "document" or not str(normalized["uri"]).startswith(prefix):
            raise PermissionError("document is not in the current Project")
        if not 1 <= max_bytes <= 2097152:
            raise ValueError("max_bytes must be 1..2097152")
        path = runtime.resolve_vault_file(project_id, normalized["uri"][len(prefix):])
        if path.stat().st_size > max_bytes:
            raise ValueError("document exceeds the requested read limit")
        document = TaskDocumentService(settings).read(project_id, normalized)
        return {"ref": normalized, "document_schema": document["schema_version"], "payload": document["payload"],
                "file_sha256": normalized["sha256"], "size": path.stat().st_size}

    async def task_documents() -> dict[str, Any]:
        async with Database(settings).tx() as c:
            row = await one(c, "SELECT documents FROM core.tasks WHERE task_id::text=%s AND project_id=%s",
                            (scope.task_id, project_id))
        if row is None:
            raise FileNotFoundError("this invocation has no task documents")
        return row["documents"] or {}

    def document_kind(kind: str) -> str:
        if kind not in drafts.KINDS:
            raise ValueError(f"unknown document kind {kind!r}; one of " + ", ".join(drafts.KINDS))
        return kind

    @tool(READ_ONLY)
    async def read_task_document(kind: str) -> dict:
        """Read one of this task's documents: the current stage's working draft, else the current revision."""
        document = (await task_documents()).get(document_kind(kind))
        if kind in drafts.editable(scope.stage):
            draft = drafts.ensure(settings, project_id, scope.task_id, kind, document)
            return {"kind": kind, "source": "draft", "markdown": draft["markdown"],
                    "sha256": drafts.text_sha(draft["markdown"]), "revision_sha256": draft["base_sha256"]}
        markdown = drafts.revision_markdown(settings, project_id, document)
        return {"kind": kind, "source": "revision" if document else "none", "markdown": markdown,
                "sha256": drafts.text_sha(markdown), "revision_sha256": (document or {}).get("sha256")}

    @tool(DRAFT_WRITE)
    async def edit_task_document(kind: str, edits: list[dict[str, str]] | None = None,
                                 content: str | None = None) -> dict:
        """Edit the current stage's document draft or the working notes: exact-text edits [{old, new}] (each old
        must occur exactly once), or the whole text as content. Kernel saves the draft as a new revision when the
        turn ends."""
        allowed = drafts.editable(scope.stage)
        if document_kind(kind) not in allowed:
            raise PermissionError(f"stage {scope.stage!r} can only edit {sorted(allowed)}, not {kind!r}")
        if (edits is None) == (content is None):
            raise ValueError("give either edits or content")
        draft = drafts.ensure(settings, project_id, scope.task_id, kind, (await task_documents()).get(kind))
        markdown = content if content is not None else drafts.apply_edits(draft["markdown"], edits or [])
        drafts.save(settings, project_id, scope.task_id, kind, markdown, draft["base_sha256"])
        return {"kind": kind, "sha256": drafts.text_sha(markdown), "size": len(markdown),
                "changed": markdown != draft["markdown"], "edits_applied": len(edits or [])}

    @tool(READ_ONLY)
    async def list_iterations(days: int = 28, artifact: str | None = None, major_only: bool = False,
                              query: str | None = None, limit: int = 20) -> dict:
        """This project's change stream (what changed, why, expected effect, effect review), newest first.
        artifact filters by e.g. "skill:assistant-requirements", "map", "charter", "jobs", "file:<path>" (a trailing *
        matches a prefix); query matches words in titles, reasons and change notes. Read only what the question needs."""
        from assistant_kernel.core import iterlog
        from assistant_kernel.core.kernel import Kernel
        rows = await Kernel(settings).iterations(project_id, days=days, artifact=artifact, major_only=major_only,
                                                 query=query, limit=min(max(limit, 1), 50))
        return {"count": len(rows), "text": iterlog.render(rows)}

    @tool(READ_ONLY)
    def map_lookup(query: str = "", limit: int = 20) -> dict:
        """Look up the project map (the index of the project's files: path, name, purpose, summary). query matches
        words in those fields (empty: the first entries); each match also says when the file last changed (git)."""
        from assistant_kernel.core import projectmap
        root, path, shown = projectmap.location(settings, project_id)
        entries = projectmap.load(path)
        matches = projectmap.lookup(root, entries, query, limit=min(max(limit, 1), 50),
                                    project_root=runtime.ensure(project_id).vault_root)
        return {"map": shown, "total_entries": len(entries), "count": len(matches), "entries": matches}

    @tool(READ_ONLY)
    def list_credentials() -> dict:
        """The project's saved credentials WITHOUT values: key, service, purpose, who uses it, when updated.
        Check here before asking the user for a login or key; ask (a question marked credential) only if missing."""
        from assistant_kernel.core import credentials
        return {"items": credentials.listing(runtime.ensure(project_id).vault_root)}

    @tool(READ_ONLY)
    def get_credential(key: str) -> dict:
        """One saved credential WITH its value, for work that really needs it (e.g. logging in during a test).
        Never copy the value into documents, reports, files or messages."""
        from assistant_kernel.core import credentials
        item = credentials.get(runtime.ensure(project_id).vault_root, key)
        if item is None:
            raise FileNotFoundError(f"没有凭据 {key}；用 list_credentials 查看已有的 key")
        return item

    @tool(READ_ONLY)
    async def list_recent_history(lookback_days: int = 365, limit: int = 50, cursor: str | None = None,
                                  scope: Literal["current_conversation", "project"] = "project") -> dict:
        """List recent messages in this Project (both channels, oldest first); cursor and scope are accepted
        for compatibility, every call returns the latest page of the whole Project."""
        async with Database(settings).tx() as c:
            rows = await (await c.execute(
                """SELECT message_id, created_at, direction, role, body->>'text' AS text FROM core.messages
                   WHERE project_id=%s AND body ? 'text' AND created_at > now() - make_interval(days => %s)
                   ORDER BY created_at DESC LIMIT %s""",
                (project_id, min(max(lookback_days, 1), 365), min(max(limit, 1), 200)))).fetchall()
        return {"items": [{**r, "message_id": str(r["message_id"]), "created_at": r["created_at"].isoformat()}
                          for r in reversed(rows)], "next_cursor": None}

    @tool(READ_ONLY)
    def vault_list(relative_path: str = "", limit: int = 200) -> dict:
        """List files inside the current Project Vault."""
        root = runtime.resolve_vault_file(project_id, relative_path)
        if not root.exists():
            return {"files": []}
        if not root.is_dir():
            raise ValueError("relative_path must identify a directory")
        base = runtime.ensure(project_id).vault_root
        files = []
        for item in sorted(root.rglob("*")):
            if item.is_file():
                files.append(item.relative_to(base).as_posix())
                if len(files) >= min(max(limit, 1), 1000):
                    break
        return {"project_id": project_id, "files": files}

    @tool(READ_ONLY)
    def vault_read_text(relative_path: str, max_bytes: int = 262144) -> dict:
        """Read one UTF-8 file inside the current Project Vault."""
        target = runtime.resolve_vault_file(project_id, relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        data = target.read_bytes()
        if len(data) > min(max(max_bytes, 1), 2 * 1024 * 1024):
            raise ValueError(f"file exceeds max_bytes={max_bytes}")
        return {"uri": f"vault://{project_id}/{relative_path}", "text": data.decode("utf-8")}

    if scope.allowed & {"volcengine_model_status", "volcengine_generate", "volcengine_ocr"}:
        from assistant_kernel.model_providers import VolcengineGateway
        volcengine = VolcengineGateway(settings)

        @tool(READ_ONLY)
        def volcengine_model_status() -> dict:
            """Return shared Volcengine capability status and fixed model names, never keys."""
            return volcengine.status()

        @tool(MODEL_CALL)
        async def volcengine_generate(prompt: str, system_prompt: str = "", max_tokens: int = 4096,
                                      thinking: Literal["enabled", "disabled", "auto"] = "disabled") -> dict:
            """Call the shared low-cost Volcengine text model."""
            return (await volcengine.generate(prompt, system_prompt=system_prompt, max_tokens=max_tokens,
                                              thinking=thinking)).model_dump()

        @tool(MODEL_CALL)
        async def volcengine_ocr(image_url: str, max_tokens: int = 8192,
                                 prompt: str = "Extract all visible text faithfully and return Markdown only.") -> dict:
            """Extract visible text from one HTTPS or data-URI image through the shared model."""
            return (await volcengine.ocr(image_url, prompt=prompt, max_tokens=max_tokens)).model_dump()

    missing = scope.allowed - set(scope.receipt.get("exposed", []))
    if missing:
        raise ValueError("requested Kernel tools are not implemented: " + ", ".join(sorted(missing)))
    return server


def load_private_config() -> dict[str, Any]:
    """The per-invocation scope file the harness wrote (credentials never travel in argv)."""
    path = Path(os.environ["KERNEL_MCP_CONFIG_PATH"])
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != os.environ["KERNEL_MCP_CONFIG_SHA256"]:
        raise PermissionError("invocation MCP configuration changed")
    return json.loads(encoded)


def run() -> None:
    config = load_private_config()
    os.environ.update(config.get("control_environment") or {})
    os.environ.update(config.get("scope_context") or {})
    # Native sub-agents may start their own connection; each process keeps its own receipt.
    receipt = Path(config["receipt_path"])
    scope = Scope.from_environment(receipt.with_name(f"{receipt.stem}.{os.getpid()}{receipt.suffix}"))
    scope.receipt = {"schema_version": "mcp-invocation.v1", "invocation_id": config["invocation_id"],
                     "exposed": [], "calls": []}
    server = build_server(scope)
    scope.save_receipt()
    server.run(transport="stdio")


if __name__ == "__main__":
    run()
