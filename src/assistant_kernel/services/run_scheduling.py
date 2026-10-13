"""Durable queue and conflict classification; queues do not grant permissions."""
from __future__ import annotations

from typing import Any


def run_scheduling(
    project_id: str, agent_key: str, run_type: str, request: dict[str, Any], *, engineering_main: bool = True,
) -> tuple[str, str | None]:
    shared = request.get("shared_write", False)
    if not isinstance(shared, bool):
        raise ValueError("shared_write must be a boolean")
    queue = "runtime" if run_type == "python" else "interactive"
    if (run_type == "publish" or shared) and not request.get('resource_keys'):
        return queue, "shared-write:global"
    if engineering_main and agent_key.endswith(":manager"):
        return queue, f"engineering:{project_id}"
    return queue, None
