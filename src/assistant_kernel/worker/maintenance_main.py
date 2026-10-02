from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import signal

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.channel_health import (
    ChannelHealthReporter,
    channel_health_pulse,
    now_iso,
)
from assistant_kernel.worker.main import run_maintenance_loop


async def _run() -> None:
    settings = KernelSettings.from_environment()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def request_shutdown(received: signal.Signals) -> None:
        if not shutdown_event.is_set():
            logging.info("received %s; stopping maintenance", received.name)
            shutdown_event.set()

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(received, request_shutdown, received)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(received)

    health = ChannelHealthReporter(settings, "maintenance")
    health.update(
        enabled=True,
        connected=True,
        process_running=True,
        last_ready_at=now_iso(),
        last_error_type=None,
    )
    pulse = asyncio.create_task(channel_health_pulse(health))
    try:
        await run_maintenance_loop(settings, shutdown_event)
    except Exception as exc:
        health.update(
            connected=False,
            process_running=False,
            last_error_type=type(exc).__name__,
        )
        raise
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
        health.update(connected=False, process_running=False)
        for received in installed:
            loop.remove_signal_handler(received)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    run()
