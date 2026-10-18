"""Public entry points of the rebuilt core. Each method is one transaction."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import cards, machine, rules
from assistant_kernel.core.db import Database, all_rows, event, lock_project, one

EVOLUTION_GOAL = "周度复盘：根据最近的运行记录提出改进建议"
EVOLUTION_TZ = ZoneInfo("Asia/Shanghai")
IDLE_DAYS = 7   # a task waiting this long for the user ends on its own

log = logging.getLogger(__name__)
CONTROL_RUNS = ("pm_turn", "supervisor", "validation", "publish", "bootstrap", "triage", "job")


class LeaseLost(Exception):
    """The run is no longer held by this claim (cancelled, expired or claimed elsewhere)."""


class Kernel:
    def __init__(self, settings: KernelSettings, *, retry_backoff_seconds: float = 15.0, triage: bool = True):
        self.db = Database(settings)
        self.settings = settings
        self.retry_backoff_seconds = retry_backoff_seconds
        self.triage = triage   # idle PM-channel messages go to triage first (R2.1); tests may start tasks directly

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
            routed = (await machine.user_input(c, project_id, message, triage=self.triage) if role == "pm"
                      else await machine.consult(c, project_id, message))
            return {"message_id": str(message["message_id"]), **routed}

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

    async def get_task(self, task_id) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.tasks WHERE task_id=%s", (task_id,))

    async def get_message(self, message_id) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.messages WHERE message_id=%s", (message_id,))

    async def recent_messages(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            rows = await (await c.execute(
                """SELECT * FROM core.messages WHERE project_id=%s AND body ? 'text'
                   ORDER BY created_at DESC LIMIT %s""", (project_id, limit))).fetchall()
            return list(reversed(rows))

    # ------------------------------------------------------------ cards

    async def card(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.interactions WHERE project_id=%s AND status='open'", (project_id,))

    async def act(self, project_id: str, item_id: str, action: str, values: dict[str, Any] | None = None,
                  *, user_id: str | None = None, external_event_id: str | None = None) -> dict[str, Any]:
        """Apply a card action. Raises cards.StaleItem for replaced/handled items (UI: 'card updated')."""
        if action == "submit" and (values or {}).get("answers"):
            values = {**values, "answers": await self._store_credentials(project_id, item_id, values["answers"])}
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

    async def _store_credentials(self, project_id: str, item_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        """Answers to questions marked `credential` go to the project's credential file; the conversation keeps
        only a note that they were stored (ROUND5 D8)."""
        card = await self.card(project_id)
        item = next((i for i in (card or {}).get("items", []) if i["item_id"] == item_id), None)
        marked = {q["id"]: q["credential"] for q in ((item or {}).get("content") or {}).get("questions") or []
                  if isinstance(q.get("credential"), dict) and q["credential"].get("key")}
        if not marked:
            return answers
        from assistant_kernel.core import credentials
        from assistant_kernel.storage import ProjectRuntimeManager
        vault = ProjectRuntimeManager(self.settings.data_root).ensure(project_id).vault_root
        stored = dict(answers)
        for question_id, spec in marked.items():
            value = str(answers.get(question_id) or "").strip()
            if value:
                credentials.put(vault, str(spec["key"]), value, service=str(spec.get("service") or ""),
                                purpose=str(spec.get("purpose") or ""), source="user_card")
                stored[question_id] = f"（已保存为项目凭据 {spec['key']}，用 get_credential 读取）"
        return stored

    async def displayed_cards(self, project_id: str) -> list[dict[str, Any]]:
        """The open card plus closed cards whose channel messages still need tidying up."""
        async with self.db.tx() as c:
            return list(await (await c.execute(
                """SELECT * FROM core.interactions WHERE project_id=%s
                   AND (status='open' OR display->'messages' <> '{}'::jsonb) ORDER BY created_at""",
                (project_id,))).fetchall())

    async def set_card_display(self, interaction_id, display: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await cards.set_display(c, interaction_id, display)

    async def pending_outbound(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            return list(await (await c.execute(
                """SELECT * FROM core.messages WHERE direction='outbound' AND delivery_status='pending'
                   ORDER BY created_at LIMIT %s""", (limit,))).fetchall())

    async def mark_delivered(self, message_id, *, external_id: str | None, ok: bool = True) -> None:
        async with self.db.tx() as c:
            await c.execute("""UPDATE core.messages SET delivery_status=%s, external_event_id=COALESCE(%s, external_event_id)
                               WHERE message_id=%s""", ("sent" if ok else "failed", external_id, message_id))

    async def start_evolution(self, project_id: str, *, origin: str = "user",
                              idempotency_key: str | None = None) -> dict[str, Any]:
        """Queue a self-evolution review: the PM reviews the last week, the user decides on its proposals."""
        return await self.create_task(project_id, goal=EVOLUTION_GOAL, origin=origin, kind="evolution",
                                      idempotency_key=idempotency_key)

    async def schedule_evolution(self, now: datetime | None = None) -> list[str]:
        """Weekly review from Sunday 13:00 Asia/Shanghai: once per project and ISO week, only if the project was used."""
        local = (now or datetime.now(timezone.utc)).astimezone(EVOLUTION_TZ)
        if local.isoweekday() != 7 or local.hour < 13:
            return []
        year, week, _ = local.isocalendar()
        week_key = f"{year}-W{week:02d}"
        async with self.db.tx() as c:
            due = await all_rows(c, """
                SELECT p.project_id FROM core.projects p
                WHERE p.status='active' AND coalesce((p.settings->>'weekly_evolution')::boolean, true)
                  AND NOT EXISTS (SELECT 1 FROM core.tasks t WHERE t.idempotency_key = 'evolution:' || p.project_id || ':' || %s)
                  AND (EXISTS (SELECT 1 FROM core.messages m WHERE m.project_id=p.project_id AND m.direction='inbound'
                                 AND m.created_at > now() - interval '7 days')
                       OR EXISTS (SELECT 1 FROM core.tasks t WHERE t.project_id=p.project_id AND t.kind <> 'evolution'
                                    AND t.updated_at > now() - interval '7 days'))""", (week_key,))
        for row in due:
            await self.start_evolution(row["project_id"], origin="schedule",
                                       idempotency_key=f"evolution:{row['project_id']}:{week_key}")
        return [row["project_id"] for row in due]

    async def expire_idle_tasks(self, days: int = IDLE_DAYS) -> list[str]:
        """Tasks waiting for the user with no word or click from the user for `days` end on their own, so the
        project's next task can start (ROUND5 D23). Returns their ids."""
        async with self.db.tx() as c:
            idle = await all_rows(c, """
                SELECT t.task_id, t.project_id FROM core.tasks t
                WHERE t.state = 'awaiting_user' AND t.updated_at < now() - make_interval(days => %s)
                  AND NOT EXISTS (SELECT 1 FROM core.messages m WHERE m.project_id = t.project_id
                                  AND m.direction = 'inbound' AND m.created_at > now() - make_interval(days => %s))""",
                                  (days, days))
        expired = []
        for row in idle:
            async with self.db.tx() as c:
                await lock_project(c, row["project_id"])
                task = await machine.load_task(c, row["task_id"])
                if task["state"] != "awaiting_user":
                    continue
                await machine.close(c, task, "cancelled", "expired_no_response")
                await machine.notify(c, task["project_id"], "supervisor",
                                     f"⌛ 任务等待你超过 {days} 天没有回应，已自动结束：{task['goal'][:100]}。"
                                     "已有的文档都保留着，需要时重新提出即可。", task_id=task["task_id"])
                expired.append(str(task["task_id"]))
        return expired

    async def grants(self, project_id: str) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            return await all_rows(c, """SELECT resource_key, policy_json FROM core.project_grants WHERE project_id=%s
                                        AND grant_kind='file' AND status='active' ORDER BY resource_key""", (project_id,))

    async def revoke_grant(self, project_id: str, key: str) -> bool:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            return await machine.revoke_grant(c, project_id, key, reason="用户用 /grants revoke 撤销")

    async def iterations(self, project_id: str, *, days: int = 28, artifact: str | None = None,
                         major_only: bool = False, query: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """The project's change stream, newest first (ROUND4 R4.1). `query` matches words for now; the same parameter
        can later be served by vector search over search_text without changing callers."""
        sql = ["SELECT * FROM core.iterations WHERE project_id=%s AND created_at > now() - %s::interval"]
        params: list[Any] = [project_id, f"{max(1, int(days))} days"]
        if artifact:
            sql.append("AND (%s = ANY(artifacts) OR EXISTS (SELECT 1 FROM unnest(artifacts) a WHERE a LIKE %s))")
            params += [artifact, artifact.rstrip("*") + "%"]
        if major_only:
            sql.append("AND significance='major'")
        for word in (query or "").split()[:5]:
            sql.append("AND search_text ILIKE %s")
            params.append(f"%{word}%")
        sql.append("ORDER BY created_at DESC LIMIT %s")
        params.append(min(max(int(limit), 1), 100))
        async with self.db.tx() as c:
            return await all_rows(c, " ".join(sql), tuple(params))

    async def task_ledger(self, task_id) -> dict[str, Any]:
        """The recorded facts of one task: its finished PM turns (input and output), the user messages each turn
        consumed, and the card actions. Read-only; rendered by executors/state.py."""
        async with self.db.tx() as c:
            turns = await all_rows(c, """SELECT run_id, input, result, created_at FROM core.runs
                                         WHERE task_id=%s AND run_type='pm_turn' AND status='succeeded'
                                         ORDER BY created_at""", (task_id,))
            messages = await all_rows(c, """SELECT consumed_by_run_id, body->>'text' AS text FROM core.messages
                                            WHERE task_id=%s AND direction='inbound' AND consumed_by_run_id IS NOT NULL
                                            ORDER BY created_at""", (task_id,))
            actions = await all_rows(c, """SELECT payload, created_at FROM core.task_events
                                           WHERE task_id=%s AND kind='card_action' ORDER BY event_id""", (task_id,))
        supplements: dict[str, list[str]] = {}
        for m in messages:
            if m["text"]:
                supplements.setdefault(str(m["consumed_by_run_id"]), []).append(m["text"])
        return {"turns": [{**t, "supplements": supplements.get(str(t["run_id"]), [])} for t in turns],
                "actions": [{**a["payload"], "at": a["created_at"]} for a in actions]}

    async def project_digest(self, project_id: str, days: int = 7) -> dict[str, Any]:
        """What happened in a project recently: the evidence a weekly evolution review works from."""
        since = f"{int(days)} days"
        async with self.db.tx() as c:
            tasks = list(await (await c.execute(
                """SELECT kind, state, stage, turn_count, close_reason, left(goal, 160) AS goal FROM core.tasks
                   WHERE project_id=%s AND updated_at > now() - %s::interval AND kind <> 'evolution'
                   ORDER BY created_at""", (project_id, since))).fetchall())
            actions = list(await (await c.execute(
                """SELECT payload FROM core.task_events WHERE project_id=%s AND kind='card_action'
                   AND created_at > now() - %s::interval ORDER BY event_id""", (project_id, since))).fetchall())
            failures = list(await (await c.execute(
                """SELECT run_type, purpose, count(*) AS n, left(max(error->>'message'), 200) AS example
                   FROM core.runs WHERE project_id=%s AND created_at > now() - %s::interval
                   AND (status='failed' OR attempt > 1) GROUP BY run_type, purpose""", (project_id, since))).fetchall())
            counts = dict(await (await c.execute(
                """SELECT kind, count(*) FROM core.task_events WHERE project_id=%s AND created_at > now() - %s::interval
                   AND kind IN ('card_upgraded','stale_result','supervisor_reevaluate') GROUP BY kind""",
                (project_id, since))).fetchall())
            inbound = (await one(c, """SELECT count(*) AS n FROM core.messages WHERE project_id=%s AND direction='inbound'
                                       AND body ? 'text' AND created_at > now() - %s::interval""", (project_id, since)))["n"]
            delegated = await all_rows(c, """
                SELECT d->>'name' AS name, count(*) AS n FROM core.runs r,
                       jsonb_array_elements(CASE WHEN jsonb_typeof(r.result->'delegations') = 'array'
                                                 THEN r.result->'delegations' ELSE '[]'::jsonb END) d
                WHERE r.project_id=%s AND r.run_type='pm_turn' AND r.created_at > now() - %s::interval
                GROUP BY 1 ORDER BY 2 DESC""", (project_id, since))
            job_runs = await all_rows(c, """
                SELECT input->>'job_id' AS job_id, count(*) FILTER (WHERE status='succeeded') AS ok,
                       count(*) FILTER (WHERE status='failed') AS failed, max(error->>'message') AS example
                FROM core.runs WHERE project_id=%s AND run_type='job' AND created_at > now() - %s::interval
                GROUP BY 1 ORDER BY 1""", (project_id, since))
            stage_turns = await all_rows(c, """
                SELECT left(t.goal, 60) AS goal, r.input->>'stage' AS stage, count(*) AS n
                FROM core.runs r JOIN core.tasks t ON t.task_id = r.task_id
                WHERE r.project_id=%s AND r.run_type='pm_turn' AND r.status='succeeded'
                  AND r.created_at > now() - %s::interval
                GROUP BY t.task_id, t.goal, r.input->>'stage' ORDER BY min(r.created_at)""", (project_id, since))
        return {"days": days, "tasks": tasks, "card_actions": [a["payload"] for a in actions], "stage_turns": stage_turns,
                "failures": failures, "event_counts": counts, "user_messages": inbound,
                "delegations": [(d["name"] or "（未命名）", d["n"]) for d in delegated], "jobs": job_runs}

    async def active_projects(self) -> list[str]:
        async with self.db.tx() as c:
            return [r["project_id"] for r in await all_rows(
                c, "SELECT project_id FROM core.projects WHERE status='active' ORDER BY project_id")]

    async def active_task(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.tasks WHERE project_id=%s AND state = ANY(%s)",
                             (project_id, list(rules.ACTIVE_STATES)))

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
                        ORDER BY r.created_at
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
            except Exception as exc:   # a Kernel bug: running the model again cannot fix it, so tell the user now
                log.exception("handling the result of run %s failed", run["run_id"])
                await self._fail(c, held, {"type": "kernel_error", "message": f"{type(exc).__name__}: {exc}"[:2000]},
                                 retryable=False)

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
            try:   # one run that cannot be failed must not keep the others stuck
                async with self.db.tx() as c:
                    await lock_project(c, row["project_id"])
                    run = await one(c, "SELECT * FROM core.runs WHERE run_id=%s AND status='running' FOR UPDATE",
                                    (row["run_id"],))
                    if run and run["lease_until"] is not None:
                        # A publish may have partly applied: never retry it blindly.
                        await self._fail(c, run, {"type": "lease_expired", "message": "worker stopped"},
                                         retryable=run["run_type"] != "publish")
            except Exception:
                log.exception("recovering expired run %s failed", row["run_id"])
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
        status = "cancelled" if run["cancel_requested"] else "failed"
        failed = await one(c, """UPDATE core.runs SET status=%s, error=%s, ended_at=now(), lease_until=NULL
                                 WHERE run_id=%s RETURNING *""", (status, Jsonb(error), run["run_id"]))
        if run["run_type"] == "pm_turn":   # its input never reached a finished turn: the next PM turn reads it
            await c.execute("UPDATE core.messages SET consumed_by_run_id=NULL WHERE consumed_by_run_id=%s",
                            (run["run_id"],))
        if status == "failed":
            await machine.on_exhausted(c, failed)
