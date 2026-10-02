from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChannelHealthReporter:
    def __init__(self, settings: KernelSettings, channel: str) -> None:
        self.path = settings.data_root / "runtime" / "channel-health" / f"{channel}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "channel": channel,
            "process_started_at": now_iso(),
            "process_running": True,
            "connected": False,
            "reconnect_count": 0,
            "last_error_type": None,
        }
        self.update()

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
            self._state["updated_at"] = now_iso()
            temporary = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def reconnecting(self) -> None:
        self.update(
            connected=False,
            reconnect_count=int(self._state.get("reconnect_count") or 0) + 1,
        )


async def channel_health_pulse(reporter: ChannelHealthReporter) -> None:
    while True:
        reporter.update(process_running=True, last_process_heartbeat_at=now_iso())
        await asyncio.sleep(30)


def read_channel_health(settings: KernelSettings, channel: str) -> dict[str, Any]:
    path = settings.data_root / "runtime" / "channel-health" / f"{channel}.json"
    if not path.is_file():
        return {"channel": channel, "process_running": False, "connected": False, "stale": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(data.get("updated_at") or ""))
        age = max((datetime.now(timezone.utc) - updated).total_seconds(), 0.0)
        return {**data, "stale": age > 90, "age_seconds": round(age, 3)}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"channel": channel, "process_running": False, "connected": False, "stale": True}


class WorkerHealthReporter:
    def __init__(self, settings: KernelSettings, worker_id: str) -> None:
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(worker_id)).strip("-")
        if not safe_id:
            raise ValueError("worker health id is empty")
        self.path = settings.data_root / "runtime" / "worker-health" / f"{safe_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "worker_id": safe_id,
            "process_started_at": now_iso(),
            "process_running": True,
            "claimed_runs": 0,
            "last_error_type": None,
        }
        self.update()

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
            self._state["updated_at"] = now_iso()
            temporary = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)


async def worker_health_pulse(reporter: WorkerHealthReporter) -> None:
    while True:
        reporter.update(process_running=True, last_process_heartbeat_at=now_iso())
        await asyncio.sleep(30)


def read_worker_health(
    settings: KernelSettings, *, stale_after_seconds: float = 90.0,
) -> dict[str, Any]:
    root = settings.data_root / "runtime" / "worker-health"
    workers: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                updated = datetime.fromisoformat(str(data.get("updated_at") or ""))
                age = max((datetime.now(timezone.utc) - updated).total_seconds(), 0.0)
                workers.append({
                    **data,
                    "stale": age > stale_after_seconds,
                    "age_seconds": round(age, 3),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                workers.append({
                    "worker_id": path.stem, "process_running": False,
                    "stale": True, "age_seconds": None,
                })
    fresh = [
        item for item in workers
        if item.get("process_running") and not item.get("stale")
    ]
    return {
        "fresh_count": len(fresh),
        "total_count": len(workers),
        "workers": workers,
    }
