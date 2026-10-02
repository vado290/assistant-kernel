from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


def install_chatgpt_auth(source: Path, target: Path, label: str) -> None:
    payload = source.read_bytes()
    parsed = json.loads(payload.decode("utf-8"))
    if parsed.get("auth_mode") != "chatgpt" or not parsed.get("tokens"):
        raise RuntimeError("source auth.json is not a cached ChatGPT Codex login")
    target.mkdir(parents=True, exist_ok=True)
    auth_path = target / "auth.json"
    temporary = target / f"auth.json.tmp-{os.getpid()}"
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    temporary.replace(auth_path)
    os.chmod(auth_path, 0o600)
    print(f"CODEX_CHATGPT_AUTH_SYNCED {label}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Copy an existing ChatGPT-backed Codex auth cache into the Kernel template "
            "and selected isolated Project CODEX_HOME directories."
        )
    )
    parser.add_argument("--source-auth", type=Path, required=True)
    parser.add_argument("--project", action="append", default=[])
    args = parser.parse_args()

    settings = KernelSettings.from_environment()
    install_chatgpt_auth(
        args.source_auth,
        settings.data_root / "system/codex-home-template",
        "template",
    )
    runtime_manager = ProjectRuntimeManager(settings.data_root)
    for project_id in args.project:
        runtime = runtime_manager.ensure(project_id)
        install_chatgpt_auth(
            args.source_auth, runtime.codex_home, f"project:{project_id}"
        )


if __name__ == "__main__":
    main()
