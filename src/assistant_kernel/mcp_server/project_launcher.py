from __future__ import annotations

import os
import runpy
from pathlib import Path


def run() -> None:
    root_value = os.environ.get("KERNEL_PROJECT_MCP_ROOT", "")
    entrypoint = os.environ.get("KERNEL_PROJECT_MCP_ENTRYPOINT", "")
    server_id = os.environ.get("KERNEL_PROJECT_MCP_SERVER_ID", "")
    if not root_value or not entrypoint or not server_id:
        raise RuntimeError("Project MCP launcher context is incomplete")
    root = Path(root_value).resolve()
    relative = Path(entrypoint)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
        raise ValueError("Project MCP entrypoint must be a relative Python file")
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise ValueError("Project MCP entrypoint escaped or is missing")
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    run()
