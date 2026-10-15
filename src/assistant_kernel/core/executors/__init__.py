"""Executors: build input, call a model or tool, return a result dict. They never change task state."""
from __future__ import annotations

from typing import Any

from assistant_kernel.core.dispatch import Executor


def build_executors() -> dict[str, Executor]:
    from assistant_kernel.core.executors.delivery import execute_publish, execute_validation
    from assistant_kernel.core.executors.pm import execute_pm
    from assistant_kernel.core.executors.supervisor import execute_supervisor
    return {"pm_turn": execute_pm, "supervisor": execute_supervisor,
            "validation": execute_validation, "publish": execute_publish}
