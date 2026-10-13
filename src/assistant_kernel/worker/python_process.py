"""Bounded cleanup for Python's private Linux session; no Codex harness changes."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any


def _stat(pid: int) -> dict[str, Any]:
    # comm can contain spaces and parentheses; fields after its final ')' are stable.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "state": fields[0], "pgid": int(fields[2]),
            "sid": int(fields[3]), "start_ticks": int(fields[19])}


def python_process_identity(pid: int, run_id: str, attempt: int) -> dict[str, Any]:
    try:
        ticks = _stat(pid)["start_ticks"]
    except FileNotFoundError:
        ticks = None  # A very short process may already have exited.
    return {"pid": pid, "pgid": pid, "sid": pid, "start_ticks": ticks,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "run_id": run_id, "attempt": attempt}


def inspect_python_group(identity: dict[str, Any] | None) -> dict[str, Any]:
    """Return verified (no executable members), active, or unknown. Never signal."""
    if not identity:
        return {"status": "unknown", "reason": "process identity was not recorded"}
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot != identity["boot_id"]:
            return {"status": "verified", "reason": "recorded kernel boot has ended"}
        if identity.get('start_ticks') is None:
            return {"status": "unknown", "reason": "launched process identity is unknown; reconciliation required"}
        pgid = int(identity["pgid"])
        if pgid <= 1 or pgid != int(identity["pid"]) or pgid != int(identity["sid"]):
            return {"status": "unknown", "reason": "not a private Python session"}
        leader = None
        members = []
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                member = _stat(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if member["pid"] == pgid:
                leader = member
            if member["pgid"] == pgid and member["state"] not in {"Z", "X"}:
                members.append(member)
        if leader and identity["start_ticks"] != leader["start_ticks"]:
            return {"status": "unknown", "reason": "leader identity changed"}
        if not members:
            return {"status": "verified", "reason": "no executable group members"}
        for member in members:
            if member["sid"] != pgid:
                return {"status": "unknown", "reason": "session identity changed"}
            if leader is None:
                # When the parent is gone, verify inherited launch markers before killing.
                try:
                    env = Path(f"/proc/{member['pid']}/environ").read_bytes().split(b"\0")
                    expected = [f"ASSISTANT_RUN_ID={identity['run_id']}".encode(),
                                f"ASSISTANT_ATTEMPT={identity['attempt']}".encode()]
                    if not all(marker in env for marker in expected):
                        return {"status": "unknown", "reason": "member launch markers differ"}
                    if _stat(member["pid"])["start_ticks"] != member["start_ticks"]:
                        return {"status": "unknown", "reason": "member identity changed"}
                except (FileNotFoundError, ProcessLookupError):
                    return {"status": "active", "reason": "member exited during probe"}
        return {"status": "active", "reason": "executable group members remain",
                "member_pids": [member["pid"] for member in members]}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {"status": "unknown", "reason": "process group could not be verified"}


async def wait_python_group(identity: dict[str, Any] | None, seconds: float) -> dict[str, Any]:
    """Allow normally joined pools' resource trackers to exit without killing them."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        probe = inspect_python_group(identity)
        if probe["status"] != "active":
            return probe
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return probe
        await asyncio.sleep(min(0.05, remaining))


async def cleanup_python_group(
    identity: dict[str, Any] | None, grace_seconds: float = 5, kill_seconds: float = 5,
) -> dict[str, Any]:
    """TERM then KILL only a verified private session, with bounded confirmation."""
    probe = inspect_python_group(identity)
    for sig, seconds in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, kill_seconds)):
        if probe["status"] != "active":
            return probe
        # Recheck immediately before each signal; never signal on an unknown identity.
        probe = inspect_python_group(identity)
        if probe["status"] != "active":
            return probe
        try:
            os.killpg(int(identity["pgid"]), sig)
        except ProcessLookupError:
            return inspect_python_group(identity)
        except OSError:
            return {"status": "unknown", "reason": "group signal could not be delivered"}
        probe = await wait_python_group(identity, seconds)
        if probe["status"] != "active":
            return probe
    return {**probe, "status": "failed", "reason": "group still active after bounded cleanup"}
