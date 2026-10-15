"""Dispatcher: claims runs on demand and executes each one as its own asyncio task.

Executors only build input, call a model or tool, and return a result dict; they
never change task state. The dispatcher reports the outcome to the Kernel:
complete() on success, fail() on any exception (the Kernel decides retry or not).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from assistant_kernel.core.kernel import CONTROL_RUNS, Kernel, LeaseLost

log = logging.getLogger(__name__)
MODEL_RUNS = ("pm_turn", "supervisor")


@dataclass
class ExecutionContext:
    kernel: Kernel
    services: dict[str, Any] = field(default_factory=dict)   # harness, settings, ... shared by executors
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


Executor = Callable[[dict[str, Any], ExecutionContext], Awaitable[dict[str, Any]]]


class Dispatcher:
    def __init__(self, kernel: Kernel, executors: dict[str, Executor], *,
                 services: dict[str, Any] | None = None, worker_id: str | None = None,
                 lease_seconds: int = 900, poll_seconds: float = 1.0,
                 recover_every_seconds: float = 30.0, model_cap: int | None = None):
        self.kernel = kernel
        self.executors = executors
        self.services = services or {}
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:dispatcher"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.recover_every_seconds = recover_every_seconds
        self.model_cap = model_cap                  # optional resource valve, off by default
        self.inflight: dict[str, tuple[dict[str, Any], asyncio.Task]] = {}

    def _claimable_types(self) -> tuple[str, ...]:
        types = tuple(t for t in CONTROL_RUNS if t in self.executors)
        if self.model_cap is not None:
            busy = sum(1 for run, _ in self.inflight.values() if run["run_type"] in MODEL_RUNS)
            if busy >= self.model_cap:
                types = tuple(t for t in types if t not in MODEL_RUNS)
        return types

    async def claim_and_start(self) -> dict[str, Any] | None:
        """Claim one run and start executing it in the background; None when nothing is runnable."""
        types = self._claimable_types()
        if not types:
            return None
        run = await self.kernel.claim(self.worker_id, lease_seconds=self.lease_seconds, run_types=types)
        if run is None:
            return None
        task = asyncio.create_task(self._execute(run), name=f"run-{run['run_id']}")
        self.inflight[str(run["run_id"])] = (run, task)
        task.add_done_callback(lambda _: self.inflight.pop(str(run["run_id"]), None))
        return run

    async def _execute(self, run: dict[str, Any]) -> None:
        context = ExecutionContext(self.kernel, self.services)
        work = asyncio.create_task(self.executors[run["run_type"]](run, context))
        beat = asyncio.create_task(self._heartbeat(run, context, work))
        try:
            result = await work
        except asyncio.CancelledError:
            await self._report_failure(run, {"type": "cancelled", "message": "run was cancelled or lost its lease"},
                                       retryable=False)
            return
        except Exception as exc:  # executor failure: let the Kernel retry or hand it to the user
            log.exception("run %s failed", run["run_id"])
            # A publish may have partly applied files: never retry it blindly, go to reconciliation.
            await self._report_failure(run, {"type": type(exc).__name__, "message": str(exc)[:2000]},
                                       retryable=run["run_type"] != "publish")
            return
        finally:
            beat.cancel()
        try:
            await self.kernel.complete(run, result)
        except LeaseLost:
            log.warning("run %s finished after losing its lease; result dropped", run["run_id"])

    async def _report_failure(self, run, error, *, retryable: bool) -> None:
        try:
            await self.kernel.fail(run, error, retryable=retryable)
        except LeaseLost:
            log.warning("run %s failed after losing its lease", run["run_id"])

    async def _heartbeat(self, run, context: ExecutionContext, work: asyncio.Task) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await self.kernel.heartbeat(run, self.lease_seconds)
            except Exception:
                log.exception("heartbeat for run %s failed; will retry", run["run_id"])
                continue
            if not alive:  # task cancelled or lease taken: stop the work
                context.cancel.set()
                work.cancel()
                return

    async def serve(self, stop: asyncio.Event) -> None:
        """Main loop: start every runnable run; recover expired leases periodically; drain on stop."""
        loop = asyncio.get_running_loop()
        next_recovery = 0.0
        while not stop.is_set():
            try:
                if loop.time() >= next_recovery:
                    await self.kernel.recover_expired()
                    next_recovery = loop.time() + self.recover_every_seconds
                started = await self.claim_and_start()
            except Exception:
                log.exception("dispatcher loop error")
                started = None
            if started is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        await self.drain()

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for in-flight runs; whatever is still running after `timeout` is left to lease recovery."""
        tasks = [task for _, task in self.inflight.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
