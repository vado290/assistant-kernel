"""Real-model smoke run of the rebuilt core (plan step 2f, decision D4).

Uses a temporary database and the kernel-v12-validation project's vault and Codex login.
An autopilot answers cards (recommended option, execute, approve) until the task ends.
The production database and other projects are not touched.

  python scripts/core_smoke.py ["goal text"] [--minutes 45]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

import psycopg
from psycopg import sql

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import migrate
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors
from assistant_kernel.core.kernel import Kernel
from assistant_kernel.harness import CodexCliHarness
from assistant_kernel.storage import ProjectRuntimeManager

PROJECT = "kernel-v12-validation"
GOAL = ("这是一个测试任务：请在本项目 vault 的 docs 目录下生成一份 hello.md，内容是一段三句话以内的项目简介。"
        "需求和设计都尽量简短，不需要提问。")


def say(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def answer_for(question: dict) -> str:
    return str(question.get("recommended") or (question.get("options") or ["请按你的推荐处理"])[0])


async def autopilot(kernel: Kernel, task_id: str, deadline: float) -> str:
    seen: set[str] = set()
    last_state = None
    while time.monotonic() < deadline:
        task = await kernel.get_task(task_id)
        if (task["state"], task["stage"]) != last_state:
            last_state = (task["state"], task["stage"])
            say(f"task state={task['state']} stage={task['stage']} turns={task['turn_count']}")
        if task["state"] in ("completed", "cancelled", "failed"):
            return task["state"]
        card = await kernel.card(PROJECT)
        for item in (card or {}).get("items", []):
            if item["item_id"] in seen:
                continue
            seen.add(item["item_id"])
            content = item["content"]
            say(f"CARD {item['kind']} ({item['origin']}): {content.get('message', '')}")
            if item["kind"] == "questions":
                answers = {q["id"]: answer_for(q) for q in content["questions"]}
                say(f"  answer {json.dumps(answers, ensure_ascii=False)}")
                await kernel.act(PROJECT, item["item_id"], "submit", {"answers": answers})
            elif item["kind"] == "next_step":
                say(f"  → {content['target_stage']}: {content['instruction'][:300]}")
                await kernel.act(PROJECT, item["item_id"], "execute")
            else:
                decision = content["decision"]
                action = {"release": "approve", "complete": "approve", "new_task": "reject",
                          "cancel": "reject", "turn_budget": "stop", "run_failed": "stop",
                          "reconcile": "failed"}[decision]
                say(f"  decision {decision} → {action} {json.dumps(content.get('data', {}), ensure_ascii=False)[:300]}")
                await kernel.act(PROJECT, item["item_id"], action)
        await asyncio.sleep(3)
    return "timeout"


async def main(goal: str, minutes: float) -> None:
    base = KernelSettings.from_environment()
    admin = migrate.connection_args(base)
    name = "kernel_core6_smoke_" + uuid.uuid4().hex[:12]
    with psycopg.connect(**admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = base.model_copy(update={"postgres_database": name})
    try:
        migrate.apply(migrate.connection_args(settings))
        kernel = Kernel(settings, retry_backoff_seconds=5)
        await kernel.ensure_project(PROJECT)
        harness = CodexCliHarness(ProjectRuntimeManager(settings.data_root))
        dispatcher = Dispatcher(kernel, build_executors(), services={"settings": settings, "harness": harness},
                                poll_seconds=1.0)
        stop = asyncio.Event()
        serving = asyncio.create_task(dispatcher.serve(stop))
        received = await kernel.receive(PROJECT, role="pm", text=goal)
        say(f"task {received['task_id']} created in temporary database {name}")
        outcome = await autopilot(kernel, received["task_id"], time.monotonic() + minutes * 60)
        stop.set()
        await asyncio.wait_for(serving, timeout=900)
        say(f"RESULT {outcome}")
        async with kernel.db.tx() as c:
            for row in await (await c.execute(
                    "SELECT run_type, purpose, status, attempt, error FROM core.runs ORDER BY created_at")).fetchall():
                say(f"  run {row['run_type']}:{row['purpose']} {row['status']} attempt={row['attempt']}"
                    + (f" error={json.dumps(row['error'], ensure_ascii=False)[:300]}" if row["error"] else ""))
            for row in await (await c.execute(
                    "SELECT direction, role, body->>'text' AS text FROM core.messages ORDER BY created_at")).fetchall():
                say(f"  msg {row['direction']}/{row['role']}: {(row['text'] or '')[:300]}")
            for row in await (await c.execute(
                    """SELECT kind, title, complete, significance, artifacts, reason, changes, measure, facts
                       FROM core.iterations ORDER BY created_at""")).fetchall():
                say(f"  iteration {row['kind']} complete={row['complete']} {row['significance']} {row['title']}"
                    f"\n    artifacts={row['artifacts']}\n    reason={row['reason'][:200]}\n    measure={row['measure'][:200]}"
                    f"\n    changes={json.dumps(row['changes'], ensure_ascii=False)[:500]}\n    facts={json.dumps(row['facts'], ensure_ascii=False)[:300]}")
    finally:
        with psycopg.connect(**admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        say(f"dropped {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("goal", nargs="?", default=GOAL)
    parser.add_argument("--minutes", type=float, default=45)
    options = parser.parse_args()
    asyncio.run(main(options.goal, options.minutes))
