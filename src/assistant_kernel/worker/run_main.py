from __future__ import annotations

import asyncio
import argparse
import logging
import signal

from assistant_kernel.config import KernelSettings
from assistant_kernel.worker.main import run_worker_loop


async def _run(queue_kind: str = "interactive") -> None:
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def request_shutdown(received: signal.Signals) -> None:
        if not shutdown_event.is_set():
            logging.info(
                "received %s; draining the current Run before shutdown", received.name
            )
            shutdown_event.set()

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(received, request_shutdown, received)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(received)
    try:
        from assistant_kernel.services.config_consumers import startup_settings
        await run_worker_loop(startup_settings(KernelSettings.from_environment(),'worker'), shutdown_event, queue_kind)
    finally:
        for received in installed:
            loop.remove_signal_handler(received)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", choices=("interactive", "runtime"), default="interactive")
    asyncio.run(_run(parser.parse_args().queue))


if __name__ == "__main__":
    run()
