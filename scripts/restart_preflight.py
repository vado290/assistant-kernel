"""Read-only Assistant Kernel restart readiness check.

Run this with the currently active Kernel virtualenv inside the AI container.
It intentionally performs no updates and emits one machine-readable JSON object.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

from assistant_kernel import __version__
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.worker.process_control import process_start_ticks


def _seconds_until(value: Any, now: datetime) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((value - now).total_seconds()))


async def inspect() -> tuple[dict[str, Any], int]:
    settings = KernelSettings.from_environment()
    repository = CoreRepository(settings)
    if not await repository.database.ping():
        return {
            "schema_version": "kernel-restart-readiness.v1",
            "verdict": "unhealthy",
            "kernel_version": __version__,
            "database": False,
            "recommendation": "Database is unavailable; do not restart the Kernel.",
        }, 4

    async with repository.database.connection() as connection:
        counts_cursor = await connection.execute(
            "SELECT status,count(*) AS count FROM assistant.runs GROUP BY status"
        )
        counts = {
            str(row["status"]): int(row["count"])
            for row in await counts_cursor.fetchall()
        }
        active_cursor = await connection.execute(
            """
            SELECT run_id,project_id,run_type,stage,status,attempt,started_at,
                   updated_at,lease_until,process_pid,process_start_ticks,executor_json
            FROM assistant.runs
            WHERE status IN ('running','reconciling')
            ORDER BY created_at,run_id
            """
        )
        active = list(await active_cursor.fetchall())

    now = datetime.now(timezone.utc)
    running: list[dict[str, Any]] = []
    reconciling_alive: list[str] = []
    upper_bounds: list[int] = []
    for row in active:
        pid = int(row.get("process_pid") or 0)
        expected_ticks = row.get("process_start_ticks")
        alive = bool(
            pid and expected_ticks is not None
            and process_start_ticks(pid) == int(expected_ticks)
        )
        if row["status"] == "reconciling":
            if alive:
                reconciling_alive.append(str(row["run_id"]))
            continue
        executor = dict(row.get("executor_json") or {})
        timeout_seconds = int(executor.get("timeout_seconds") or 0)
        # ``started_at`` belongs to the first attempt and ``updated_at`` also
        # moves on heartbeats, so neither is a sound per-attempt ETA.  Report
        # the configured timeout as a conservative upper bound instead of
        # presenting false precision.
        remaining_timeout = timeout_seconds if timeout_seconds > 0 else None
        if remaining_timeout is not None:
            upper_bounds.append(remaining_timeout)
        running.append({
            "run_id": str(row["run_id"]),
            "project_id": str(row["project_id"]),
            "run_type": str(row["run_type"]),
            "stage": str(row.get("stage") or "") or None,
            "attempt": int(row.get("attempt") or 0),
            "process_alive": alive,
            "lease_remaining_seconds": _seconds_until(row.get("lease_until"), now),
            "timeout_upper_bound_seconds": remaining_timeout,
        })

    if running:
        verdict = "wait"
        exit_code = 2
        recommendation = "Active Runs exist; wait for them to finish before restarting."
    elif reconciling_alive:
        verdict = "blocked"
        exit_code = 3
        recommendation = "A reconciling Run still has a live process; do not restart yet."
    elif counts.get("reconciling", 0):
        verdict = "safe_with_warnings"
        exit_code = 0
        recommendation = (
            "No process is running, but unresolved reconciliation remains and should be reviewed."
        )
    else:
        verdict = "safe"
        exit_code = 0
        recommendation = "No active execution prevents a controlled Kernel restart."

    return {
        "schema_version": "kernel-restart-readiness.v1",
        "verdict": verdict,
        "kernel_version": __version__,
        "database": True,
        "counts": counts,
        "running_runs": running,
        "live_reconciling_run_ids": reconciling_alive,
        "estimated_upper_bound_seconds": max(upper_bounds) if upper_bounds else None,
        "recommendation": recommendation,
        "checked_at": now.isoformat(),
    }, exit_code


def main() -> None:
    try:
        payload, exit_code = asyncio.run(inspect())
    except Exception as exc:
        payload = {
            "schema_version": "kernel-restart-readiness.v1",
            "verdict": "unhealthy",
            "database": None,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "recommendation": "Preflight failed; do not restart the Kernel.",
        }
        exit_code = 4
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
