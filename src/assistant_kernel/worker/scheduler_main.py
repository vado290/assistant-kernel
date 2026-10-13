"""Supervised business clock, separate from runtime execution and maintenance."""
import asyncio
import logging
import signal
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.scheduler import Scheduler
from assistant_kernel.services.channel_health import ChannelHealthReporter,now_iso
from assistant_kernel.services.config_consumers import ConfigConsumers,startup_settings


async def _run(settings=None,shutdown_event=None):
    settings=startup_settings(settings or KernelSettings.from_environment(),'scheduler');repository=CoreRepository(settings)
    scheduler=Scheduler(settings,repository);health=None;stop=shutdown_event or asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
    consumer=ConfigConsumers(settings)
    try:
        while not stop.is_set():
            try:
                async with repository.database.connection() as c:
                    if not await scheduler.acquire(c):
                        await asyncio.wait_for(stop.wait(),timeout=2)
                        continue
                    if health is None:health=ChannelHealthReporter(settings,'scheduler')
                    active=False
                    while not stop.is_set():
                        config,ref=await scheduler.configuration()
                        if config['enabled'] and not active:
                            cutoff=(await (await c.execute('SELECT clock_timestamp() AS value')).fetchone())['value'];await c.commit()
                            await scheduler.recover(c,cutoff)
                        active=config['enabled']
                        result=await scheduler.scan(c,configuration=config)
                        if ref:await consumer.record('scheduler',ref,{'/kernel/scheduler/'+k:v for k,v in config.items()})
                        health.update(enabled=active,connected=True,process_running=True,standby=False,last_ready_at=now_iso(),
                            last_error_type=None,**{'last_scan_at':result['last_scan_at']})
                        try:await asyncio.wait_for(stop.wait(),timeout=config['poll_seconds'])
                        except asyncio.TimeoutError:pass
            except asyncio.TimeoutError:pass
            except Exception as exc:
                logging.exception('Scheduler stopped submitting; reconnect requires a fresh missed-time boundary')
                if health:health.update(connected=False,process_running=True,last_error_type=type(exc).__name__)
                try:await asyncio.wait_for(stop.wait(),timeout=2)
                except asyncio.TimeoutError:pass
    finally:
        if health:health.update(connected=False,process_running=False)
        for sig in (signal.SIGTERM,signal.SIGINT):loop.remove_signal_handler(sig)


def run():
    logging.basicConfig(level=logging.INFO);asyncio.run(_run())

if __name__=='__main__':run()
