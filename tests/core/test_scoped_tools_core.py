"""The core tool server (core/tools.py) over real stdio MCP: lease checks against core.runs, the
task-owned candidate area, only the selected tools exposed, and a receipt for the harness."""
from __future__ import annotations

import hashlib
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from assistant_kernel.core.executors.common import workspace_snapshot

from .sim import next_step

TOOLS = ("get_authorized_workspace", "write_candidate_file", "vault_read_text", "list_recent_history")


def private_config(settings, run, task_id, snapshot, directory, tools=TOOLS, stage="build"):
    """What harness/mcp_config.py writes for one invocation."""
    scope = {"KERNEL_SCOPE_PROJECT_ID": "p1", "KERNEL_SCOPE_RUN_ID": str(run["run_id"]),
             "KERNEL_SCOPE_TASK_ID": str(task_id), "KERNEL_SCOPE_CLAIM_TOKEN": str(run["claim_token"]),
             "KERNEL_SCOPE_ALLOWED_TOOLS": ",".join(tools), "KERNEL_SCOPE_STAGE": stage,
             "KERNEL_SCOPE_SNAPSHOT_URI": snapshot[0], "KERNEL_SCOPE_SNAPSHOT_SHA256": snapshot[1]}
    payload = {"invocation_id": "inv-test", "scope_context": scope, "scoped_tools": list(tools),
               "control_environment": {"ASSISTANT_DATA_ROOT": str(settings.data_root),
                                       "POSTGRES_DB": settings.postgres_database},
               "receipt_path": str(directory / "mcp-runtime.json")}
    raw = json.dumps(payload).encode()
    path = directory / "mcp-private.json"
    path.write_bytes(raw)
    return {**os.environ, "KERNEL_MCP_CONFIG_PATH": str(path), "KERNEL_MCP_CONFIG_SHA256": hashlib.sha256(raw).hexdigest()}


def payload(result) -> dict:
    return json.loads(result.content[0].text)


async def test_core_tool_server_checks_leases_and_owns_candidates(kernel, sim, kernel_settings, project_files, tmp_path):
    task_id = (await kernel.receive("p1", role="pm", text="写文档"))["task_id"]
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    run = await kernel.claim("w1")
    snapshot = workspace_snapshot(kernel_settings, "p1", task_id, "probe", candidates=True)
    trace = tmp_path / "trace"
    trace.mkdir()
    env = private_config(kernel_settings, run, task_id, snapshot, trace)
    server = StdioServerParameters(command=sys.executable, args=["-m", "assistant_kernel.core.tools"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert sorted(t.name for t in (await session.list_tools()).tools) == sorted(TOOLS)

        refused = payload(await session.call_tool("vault_read_text", {"relative_path": "project.yaml"}))
        assert refused["ok"] is False and "get_authorized_workspace" in refused["message"]

        workspace = payload(await session.call_tool("get_authorized_workspace", {}))
        roots = {root["key"]: root for root in workspace["roots"]}
        assert roots["candidate"]["path"].endswith(f"/runs/{task_id}/candidate")

        written = payload(await session.call_tool("write_candidate_file", {
            "target_uri": "workspace://project/docs/hello.md", "content": "hi"}))
        assert written["candidate_vault_uri"].startswith(f"vault://p1/runs/{task_id}/candidate/")
        assert "p1" in payload(await session.call_tool("vault_read_text", {"relative_path": "project.yaml"}))["text"]

        await kernel.cancel_task(task_id)
        stopped = payload(await session.call_tool("vault_read_text", {"relative_path": "project.yaml"}))
        assert stopped["ok"] is False and stopped["error_type"] == "PermissionError"

    receipts = list(trace.glob("mcp-runtime.*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text("utf-8"))
    assert sorted(receipt["exposed"]) == sorted(TOOLS)
    assert [c["status"] for c in receipt["calls"]] == ["error", "succeeded", "succeeded", "succeeded", "error"]


async def test_document_tools_edit_only_the_stage_draft(kernel, sim, kernel_settings, project_files, tmp_path):
    task_id = (await kernel.receive("p1", role="pm", text="write the requirements"))["task_id"]
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    run = await kernel.claim("w1")
    snapshot = workspace_snapshot(kernel_settings, "p1", task_id, "probe", candidates=False)
    tools = ("get_authorized_workspace", "read_task_document", "edit_task_document")
    env = private_config(kernel_settings, run, task_id, snapshot, tmp_path, tools=tools, stage="requirements")
    server = StdioServerParameters(command=sys.executable, args=["-m", "assistant_kernel.core.tools"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("get_authorized_workspace", {})
        draft = payload(await session.call_tool("read_task_document", {"kind": "task_contract"}))
        assert (draft["source"], draft["markdown"], draft["revision_sha256"]) == ("draft", "", None)

        edit = lambda **args: session.call_tool("edit_task_document", {"kind": "task_contract", **args})  # noqa: E731
        assert payload(await edit(content="# R\nA\nB\nA"))["changed"] is True
        missing = payload(await edit(edits=[{"old": "C", "new": "D"}]))
        assert missing["ok"] is False and "not found" in missing["message"]
        repeated = payload(await edit(edits=[{"old": "A", "new": "D"}]))
        assert repeated["ok"] is False and "occurs 2 times" in repeated["message"]
        partial = payload(await edit(edits=[{"old": "B", "new": "B2"}, {"old": "nope", "new": "x"}]))
        assert partial["ok"] is False          # all or nothing: the first edit is not kept either
        other = payload(await session.call_tool("edit_task_document", {"kind": "solution_design", "content": "x"}))
        assert other["ok"] is False and other["error_type"] == "PermissionError"
        assert payload(await edit(edits=[{"old": "B", "new": "B2"}]))["edits_applied"] == 1
        draft = payload(await session.call_tool("read_task_document", {"kind": "task_contract"}))
        assert draft["markdown"] == "# R\nA\nB2\nA"
        design = payload(await session.call_tool("read_task_document", {"kind": "solution_design"}))
        assert design["source"] == "none"

        await kernel.cancel_task(task_id)
        stopped = payload(await edit(content="late"))
        assert stopped["ok"] is False and stopped["error_type"] == "PermissionError"
