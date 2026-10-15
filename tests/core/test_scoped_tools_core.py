"""Phase 2e: the scoped MCP tool server checks leases in core.runs and gives candidates to the task."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from assistant_kernel.core.executors.common import workspace_snapshot

from .sim import next_step

PROBE = """
import asyncio, json
from assistant_kernel.mcp_server import scoped_tools as t
async def main():
    try:
        await t.check_lease(); lease = "ok"
    except PermissionError:
        lease = "refused"
    candidate = next(str(r.path) for r in t.authorized_roots if r.key == "candidate")
    print(json.dumps({"lease": lease, "candidate": candidate, "owner": t.scope_owner}))
asyncio.run(main())
"""


def probe(settings, run, task_id, snapshot):
    env = {**os.environ, "ASSISTANT_DATA_ROOT": str(settings.data_root), "POSTGRES_DB": settings.postgres_database,
           "KERNEL_SCOPE_CORE": "1", "KERNEL_SCOPE_PROJECT_ID": "p1", "KERNEL_SCOPE_RUN_ID": str(run["run_id"]),
           "KERNEL_SCOPE_TASK_ID": str(task_id), "KERNEL_SCOPE_CLAIM_TOKEN": str(run["claim_token"]),
           "KERNEL_SCOPE_WORKER_INSTANCE_ID": "w1", "KERNEL_SCOPE_ALLOWED_TOOLS": "get_authorized_workspace",
           "KERNEL_SCOPE_STAGE": "build", "KERNEL_SCOPE_SNAPSHOT_URI": snapshot[0],
           "KERNEL_SCOPE_SNAPSHOT_SHA256": snapshot[1]}
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


async def test_scoped_tools_use_core_leases_and_task_candidates(kernel, sim, kernel_settings, project_files):
    task_id = (await kernel.receive("p1", role="pm", text="写文档"))["task_id"]
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    run = await kernel.claim("w1")
    snapshot = workspace_snapshot(kernel_settings, "p1", task_id, "probe", candidates=True)

    held = probe(kernel_settings, run, task_id, snapshot)
    assert held["lease"] == "ok" and held["owner"] == task_id
    assert held["candidate"].endswith(f"/runs/{task_id}/candidate")

    await kernel.cancel_task(task_id)
    assert probe(kernel_settings, run, task_id, snapshot)["lease"] == "refused"
