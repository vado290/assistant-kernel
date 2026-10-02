from __future__ import annotations

import asyncio
import logging

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.channel_health import ChannelHealthReporter, channel_health_pulse
from assistant_kernel.services.feishu import channel_worker_loop, feishu_gateway_loop


async def feishu_worker_loop() -> None:
    settings = KernelSettings.from_environment()
    health = ChannelHealthReporter(settings, "feishu")
    await asyncio.gather(
        channel_worker_loop(settings, health),
        feishu_gateway_loop(settings, health),
        channel_health_pulse(health),
    )


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(feishu_worker_loop())


if __name__ == "__main__":
    run()
