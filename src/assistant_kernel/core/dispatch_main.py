"""Process entry point for the core dispatcher (assistant-kernel-core-dispatcher)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from assistant_kernel.config import KernelSettings
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors
from assistant_kernel.core.kernel import Kernel
from assistant_kernel.harness import CodexCliHarness
from assistant_kernel.storage import ProjectRuntimeManager


async def _serve() -> None:
    settings = KernelSettings.from_environment()
    cap = os.environ.get("CORE_MODEL_CAP")
    dispatcher = Dispatcher(Kernel(settings), build_executors(),
                            services={"settings": settings,
                                      "harness": CodexCliHarness(ProjectRuntimeManager(settings.data_root))},
                            model_cap=int(cap) if cap else None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    logging.info("core dispatcher serving (model cap: %s)", cap or "off")
    await dispatcher.serve(stop)
    logging.info("core dispatcher drained and stopped")


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
