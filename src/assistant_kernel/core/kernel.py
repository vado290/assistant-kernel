"""Public entry points of the rebuilt core. Each method is one transaction."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import cards, machine, rules
from assistant_kernel.core.db import Database, event, lock_project, one

CONTROL_RUNS = ("pm_turn", "supervisor", "validation", "publish")


class LeaseLost(Exception):
    """The run is no longer held by this claim (cancelled, expired or claimed elsewhere)."""


class Kernel:
    def __init__(self, settings: KernelSettings, *, retry_backoff_seconds: float = 15.0):
        self.db = Database(settings)
        self.retry_backoff_seconds = retry_backoff_seconds

    # ------------------------------------------------------------ projects and messages

    async def ensure_project(self, project_id: str, *, kind: str = "business") -> None:
        async with self.db.tx() as c:
            await c.execute("INSERT INTO core.projects(project_id, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (project_id, kind))

    async def receive(self, project_id: str, *, role: str, text: str, channel: str = "api",
                      chat_id: str | None = None, user_id: str | None = None,
                      external_event_id: str | None = None) -> dict[str, Any]:
        """Store one inbound user message and route it: PM channel → task, Supervisor channel → consult.

        A repeated external_event_id is recognised and not processed again.
        """
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            message = await one(
                c, """INSERT INTO core.messages(project_id, channel, chat_id, user_id, direction, role, body,
                                                external_event_id)
                      VALUES (%s, %s, %s, %s, 'inbound', %s, %s, %s)
                      ON CONFLICT (channel, external_event_id) WHERE external_event_id IS NOT NULL DO NOTHING
                      RETURNING *""",
                (project_id, channel, chat_id, user_id, role, Jsonb({"text": text}), external_event_id))
            if message is None:
                return {"action": "duplicate"}
            route = machine.user_input if role == "pm" else machine.consult
            return {"message_id": str(message["message_id"]), **await route(c, project_id, message)}

    async def create_task(self, project_id: str, **fields: Any) -> dict[str, Any]:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            return await machine.create_task(c, project_id, **fields)

    async def cancel_task(self, task_id) -> dict[str, Any]:
        async with self.db.tx() as c:
            task = await one(c, "SELECT project_id FROM core.tasks WHERE task_id=%s", (task_id,))
            await lock_project(c, task["project_id"])
            task = await machine.load_task(c, task_id)
            if task["state"] in rules.TERMINAL_STATES:
                return task
            return await machine.close(c, task, "cancelled", "cancelled_by_user")

    # ------------------------------------------------------------ cards

    async def card(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.interactions WHERE project_id=%s AND status='open'", (project_id,))

    async def act(self, project_id: str, item_id: str, action: str, values: dict[str, Any] | None = None,
                  *, user_id: str | None = None, external_event_id: str | None = None) -> dict[str, Any]:
        """Apply a card action. Raises cards.StaleItem for replaced/handled items (UI: 'card updated')."""
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            record = await one(
                c, """INSERT INTO core.messages(project_id, channel, user_id, direction, role, body,
                                                external_event_id)
                      VALUES (%s, 'card', %s, 'inbound', 'supervisor', %s, %s)
                      ON CONFLICT (channel, external_event_id) WHERE external_event_id IS NOT NULL DO NOTHING
                      RETURNING message_id""",
                (project_id, user_id, Jsonb({"card_action": {"item_id": item_id, "action": action,
                                                              "values": values or {}}}), external_event_id))
            if record is None:
                return {"action": "duplicate"}
            result = await machine.on_action(c, project_id, item_id, action, values or {},
                                             source_message_id=record["message_id"])
            return {"action": action, "task_id": str(result["task_id"]) if result.get("task_id") else None}

    async def save_draft(self, project_id: str, task_id, answers: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            await cards.save_draft(c, project_id, task_id, answers)

    # ------------------------------------------------------------ runs

    async def claim(self, worker_id: str, *, lease_seconds: int = 900,
                    run_types: tuple[str, ...] = CONTROL_RUNS) -> dict[str, Any] | None:
        """Claim one runnable run. Every execution condition is in this one statement (principle P6)."""
        try:
            async with self.db.tx() as c:
                run = await one(c, """
                    UPDATE core.runs SET status='running', attempt=attempt+1, worker_id=%s, claim_token=%s,
                           lease_until=now() + make_interval(secs => %s), started_at=now()
                    WHERE run_id = (
                        SELECT r.run_id FROM core.runs r LEFT JOIN core.tasks t ON t.task_id = r.task_id
                        WHERE r.status='queued' AND r.available_at <= now() AND r.run_type = ANY(%s)
                          AND (r.task_id IS NULL OR t.state = ANY(%s))
                          AND NOT EXISTS (SELECT 1 FROM core.runs x WHERE x.mutex_key = r.mutex_key
                                          AND x.status IN ('running','reconciling'))
                          AND (r.task_id IS NULL OR NOT EXISTS (
                               SELECT 1 FROM core.runs x WHERE x.task_id = r.task_id
                               AND x.status IN ('running','reconciling')))
                        ORDER BY (t.priority = 'urgent') DESC NULLS LAST, r.created_at
                        FOR UPDATE OF r SKIP LOCKED LIMIT 1)
                    RETURNING *""",
                    (worker_id, uuid.uuid4(), lease_seconds, list(run_types), list(rules.ACTIVE_STATES)))
                if run and run["run_type"] == "pm_turn":
                    # Pin the user input this turn will read; later input waits for the next turn.
                    await c.execute("""UPDATE core.messages SET consumed_by_run_id=%s WHERE task_id=%s
                                       AND direction='inbound' AND consumed_by_run_id IS NULL""",
                                    (run["run_id"], run["task_id"]))
                return run
        except UniqueViolation:
            return None  # a concurrent claim took the same mutex/task; try again on the next poll

    async def turn_inputs(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            return list(await (await c.execute(
                "SELECT * FROM core.messages WHERE consumed_by_run_id=%s ORDER BY created_at",
                (run["run_id"],))).fetchall())

    async def complete(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await lock_project(c, run["project_id"])
            held = await self._held(c, run)
            held = await one(c, """UPDATE core.runs SET status='succeeded', result=%s, ended_at=now(),
                                   lease_until=NULL WHERE run_id=%s RETURNING *""", (Jsonb(result), run["run_id"]))
            if held["cancel_requested"]:
                await event(c, run["project_id"], "cancelled_run_result", task_id=run["task_id"], run_id=run["run_id"])
                return
            try:
                async with c.transaction():
                    await machine.on_result(c, held, result)
            except machine.InvalidResult as exc:
                await self._fail(c, held, {"type": "invalid_result", "message": str(exc)}, retryable=True)

    async def fail(self, run: dict[str, Any], error: dict[str, Any], *, retryable: bool = True) -> None:
        async with self.db.tx() as c:
            await lock_project(c, run["project_id"])
            await self._fail(c, await self._held(c, run), error, retryable=retryable)

    async def heartbeat(self, run: dict[str, Any], lease_seconds: int = 900) -> bool:
        async with self.db.tx() as c:
            row = await one(c, """UPDATE core.runs SET lease_until=now() + make_interval(secs => %s)
                                  WHERE run_id=%s AND claim_token=%s AND status='running' AND NOT cancel_requested
                                  RETURNING run_id""", (lease_seconds, run["run_id"], run["claim_token"]))
            return row is not None

    async def recover_expired(self) -> int:
        """Runs whose worker vanished: retry (or hand to the user once retries are used up)."""
        async with self.db.tx() as c:
            expired = list(await (await c.execute(
                "SELECT run_id, project_id FROM core.runs WHERE status='running' AND lease_until < now()")).fetchall())
        for row in expired:
            async with self.db.tx() as c:
                await lock_project(c, row["project_id"])
                run = await one(c, "SELECT * FROM core.runs WHERE run_id=%s AND status='running' FOR UPDATE",
                                (row["run_id"],))
                if run and run["lease_until"] is not None:
                    # A publish may have partly applied: never retry it blindly.
                    await self._fail(c, run, {"type": "lease_expired", "message": "worker stopped"},
                                     retryable=run["run_type"] != "publish")
        return len(expired)

    async def _held(self, c, run) -> dict[str, Any]:
        held = await one(c, "SELECT * FROM core.runs WHERE run_id=%s AND claim_token=%s AND status='running' FOR UPDATE",
                         (run["run_id"], run["claim_token"]))
        if held is None:
            raise LeaseLost(str(run["run_id"]))
        return held

    async def _fail(self, c, run, error: dict[str, Any], *, retryable: bool) -> None:
        if retryable and not run["cancel_requested"] and run["attempt"] < run["max_attempts"]:
            await c.execute("""UPDATE core.runs SET status='queued', error=%s, worker_id=NULL, claim_token=NULL,
                               lease_until=NULL, available_at=now() + make_interval(secs => %s) WHERE run_id=%s""",
                            (Jsonb(error), self.retry_backoff_seconds * run["attempt"], run["run_id"]))
            return
        failed = await one(c, """UPDATE core.runs SET status='failed', error=%s, ended_at=now(), lease_until=NULL
                                 WHERE run_id=%s RETURNING *""", (Jsonb(error), run["run_id"]))
        if not run["cancel_requested"]:
            await machine.on_exhausted(c, failed)
