"""The newest Codex login always wins across projects (one-time refresh tokens)."""
from __future__ import annotations

import os

from assistant_kernel.core.executors.auth_sync import master_path, sync
from assistant_kernel.storage import ProjectRuntimeManager


def write(path, text, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def test_master_and_project_logins_follow_the_newest(kernel_settings):
    master = master_path(kernel_settings)
    local = ProjectRuntimeManager(kernel_settings.data_root).ensure("p1").codex_home / "auth.json"
    write(master, '{"t": "A"}', 1000)
    write(local, '{"t": "old"}', 500)

    assert sync(kernel_settings, "p1", direction="in") and local.read_text() == '{"t": "A"}'
    assert not sync(kernel_settings, "p1", direction="out")          # same content: no copy back

    write(local, '{"t": "B"}', 2000)                            # Codex refreshed this project's copy
    assert sync(kernel_settings, "p1", direction="out") and master.read_text() == '{"t": "B"}'
    assert not sync(kernel_settings, "p1", direction="in")

    write(local, '{"t": "C', 3000)                                     # Codex is still writing it
    assert not sync(kernel_settings, "p1", direction="out") and master.read_text() == '{"t": "B"}'


async def test_failed_consultation_tells_the_user(kernel, sim):
    from .sim import Fail
    await kernel.receive("p1", role="supervisor", text="现在进展如何？")
    sim.script("supervisor:consult", Fail(), Fail(), Fail())
    await sim.drain()
    assert (await sim.outbound("p1"))[-1]["body"]["text"].startswith("⚠ The Supervisor cannot answer")
