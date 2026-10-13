"""Bounded file-input watcher in maintenance, independent from business timers."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.system_config import SystemConfigService


async def watch_system_config(settings, shutdown_event: asyncio.Event) -> None:
    service=SystemConfigService(settings)
    poll_seconds=1.0;settle_seconds=2.0;reported=None;adopted=None
    while not shutdown_event.is_set():
        try:
            state=await service.state()
            if state:
                config=service._config(state['desired_ref'])
                selected=config['config_updates']
                fingerprint=hashlib.sha256(canonical_json(selected)).hexdigest()
                if adopted!=fingerprint:
                    # These two parameters really are consumed here. No other
                    # group is marked applied until its own adapter adopts it.
                    poll_seconds=float(selected['poll_seconds']);settle_seconds=float(selected['settle_seconds'])
                    receipt={'schema_version':'config-consumer-receipt.v1','consumer':'config_watcher',
                             'source_ref':state['desired_ref'],'selected_values':selected,'pid':os.getpid(),
                             'observed_at':datetime.now(timezone.utc).isoformat()}
                    encoded=canonical_json(receipt);digest=hashlib.sha256(encoded).hexdigest()
                    path=settings.data_root/'runtime/config-consumers/config-watcher'/f'{digest}.json'
                    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(encoded)
                    ref=resource_ref(kind='receipt',identity='config-watcher-'+digest[:24],
                                     uri=str(path),sha256=digest,purpose='actual watcher parameter adoption')
                    await service.settle_group(expected_revision=state['revision'],group='config_updates:dynamic_admission',
                                               evidence_ref=ref,applied=True,reason='Watcher is using these poll and settling intervals')
                    adopted=fingerprint
            outcome=await service.poll(settle_seconds=settle_seconds)
            observation={k:v for k,v in outcome.items() if k!='receipt'}
            fingerprint=hashlib.sha256(canonical_json(observation)).hexdigest()
            if reported!=fingerprint:
                async with service.database.connection() as c:
                    await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES ('system-config-input-status',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()",
                                    (Jsonb({**observation,'observed_at':datetime.now(timezone.utc).isoformat()}),))
                    await c.commit()
                reported=fingerprint
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not print candidate contents or credentials, nor start business
            # work when DB/input/application evidence is unavailable.
            logging.warning('Configuration watcher deferred: %s',type(exc).__name__)
        try:
            await asyncio.wait_for(shutdown_event.wait(),timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
