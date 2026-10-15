"""Consistency checks for the three core invariants (plan principle P7)."""
from __future__ import annotations

from assistant_kernel.core import rules
from assistant_kernel.core.db import all_rows


async def violations(c) -> list[str]:
    """Return human-readable violations; an empty list means the database is consistent."""
    problems: list[str] = []
    for row in await all_rows(c, """SELECT project_id, count(*) n FROM core.tasks
                                    WHERE state = ANY(%s) GROUP BY project_id HAVING count(*) > 1""",
                              (list(rules.ACTIVE_STATES),)):
        problems.append(f"project {row['project_id']} has {row['n']} active tasks")
    for row in await all_rows(c, """SELECT project_id, count(*) n FROM core.interactions WHERE status='open'
                                    GROUP BY project_id HAVING count(*) > 1"""):
        problems.append(f"project {row['project_id']} has {row['n']} open cards")
    tasks = await all_rows(c, "SELECT * FROM core.tasks WHERE state = ANY(%s)", (list(rules.ACTIVE_STATES),))
    cards = {r["project_id"]: r for r in await all_rows(c, "SELECT * FROM core.interactions WHERE status='open'")}
    for task in tasks:
        runs = await all_rows(c, """SELECT run_type, purpose FROM core.runs WHERE task_id=%s
                                    AND status IN ('queued','running','reconciling')
                                    AND purpose NOT IN ('review_input')""", (task["task_id"],))
        card = cards.get(task["project_id"])
        items = [i for i in (card or {}).get("items", []) if i["target"] == {"kind": "task", "id": str(task["task_id"])}]
        cursors = [(r["run_type"], r["purpose"]) for r in runs] + [("card_item", i["kind"]) for i in items]
        expected = rules.CURSOR_RUN.get(task["state"])
        ok = (len(cursors) == 1 and (cursors[0] == expected if expected else cursors[0][0] == "card_item"))
        if not ok:
            problems.append(f"task {task['task_id']} in {task['state']} has cursors {cursors}")
    return problems
