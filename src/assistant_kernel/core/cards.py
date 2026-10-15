"""The single open card per project and its items (plan section 4).

An item is created once and never edited in place: upgrading replaces it with a
new item id, so a click on an old item is always recognisable as stale. Draft
answers are kept per task and question id, so they survive upgrades.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from psycopg.types.json import Jsonb

from assistant_kernel.core.db import event, one

ITEM_KINDS = ("questions", "next_step", "decision")


class StaleItem(Exception):
    """The clicked item no longer exists on the project's open card."""


def new_item(*, target_kind: str, target_id: str | None, kind: str, origin: str,
             content: dict[str, Any], deliver_to: str | None = None) -> dict[str, Any]:
    if kind not in ITEM_KINDS:
        raise ValueError(f"unknown card item kind {kind}")
    return {"item_id": str(uuid.uuid4()), "target": {"kind": target_kind, "id": target_id},
            "kind": kind, "origin": origin, "deliver_to": deliver_to, "content": content}


async def open_card(c, project_id: str) -> dict[str, Any] | None:
    return await one(c, "SELECT * FROM core.interactions WHERE project_id=%s AND status='open' FOR UPDATE",
                     (project_id,))


async def _write(c, card: dict[str, Any], items: list[dict[str, Any]], **changes: Any) -> dict[str, Any]:
    closing = not items
    fields = {"items": items, **changes}
    sets = ", ".join(f"{name}=%s" for name in fields)
    values = [Jsonb(value) if name in ("items", "drafts", "supplements") else value
              for name, value in fields.items()]
    row = await one(
        c, f"""UPDATE core.interactions SET {sets}, revision=revision+1, updated_at=now(),
                status=CASE WHEN %s THEN 'closed' ELSE 'open' END,
                closed_at=CASE WHEN %s THEN now() END
               WHERE interaction_id=%s RETURNING *""",
        (*values, closing, closing, card["interaction_id"]))
    await event(c, card["project_id"], "card_revised", interaction_id=card["interaction_id"],
                revision=row["revision"], items=[i["item_id"] for i in items])
    return row


async def add_item(c, project_id: str, item: dict[str, Any]) -> dict[str, Any]:
    card = await open_card(c, project_id)
    if card is None:
        card = await one(c, "INSERT INTO core.interactions(project_id, items) VALUES (%s, '[]') RETURNING *",
                         (project_id,))
    return await _write(c, card, card["items"] + [item])


async def remove_items(c, project_id: str, keep: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    card = await open_card(c, project_id)
    if card is None:
        return None
    items = [item for item in card["items"] if keep(item)]
    if len(items) == len(card["items"]):
        return card
    return await _write(c, card, items)


def task_item(card: dict[str, Any] | None, task_id: str) -> dict[str, Any] | None:
    if card is None:
        return None
    return next((i for i in card["items"] if i["target"] == {"kind": "task", "id": str(task_id)}), None)


async def take_item(c, project_id: str, item_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove a clicked item from the card; raise StaleItem when it is no longer there."""
    card = await open_card(c, project_id)
    item = next((i for i in (card or {}).get("items", []) if i["item_id"] == item_id), None)
    if item is None:
        raise StaleItem("this card item was replaced or already handled")
    card = await _write(c, card, [i for i in card["items"] if i["item_id"] != item_id])
    return card, item


async def replace_task_item(c, project_id: str, task_id: str, item: dict[str, Any]) -> dict[str, Any]:
    card = await open_card(c, project_id)
    items = [i for i in (card or {}).get("items", []) if i["target"] != {"kind": "task", "id": str(task_id)}]
    if card is None:
        return await add_item(c, project_id, item)
    return await _write(c, card, items + [item])


async def save_draft(c, project_id: str, task_id: str, answers: dict[str, Any]) -> None:
    card = await open_card(c, project_id)
    if card is None:
        raise StaleItem("no open card")
    drafts = dict(card["drafts"])
    drafts[str(task_id)] = {**drafts.get(str(task_id), {}), **answers}
    await c.execute("UPDATE core.interactions SET drafts=%s, updated_at=now() WHERE interaction_id=%s",
                    (Jsonb(drafts), card["interaction_id"]))


async def add_supplement(c, project_id: str, task_id: str, message_id: str) -> None:
    """Show new user input on the card next to the task's item (display only)."""
    card = await open_card(c, project_id)
    if card is None:
        return
    supplements = dict(card["supplements"])
    supplements[str(task_id)] = supplements.get(str(task_id), []) + [str(message_id)]
    await c.execute("UPDATE core.interactions SET supplements=%s, updated_at=now() WHERE interaction_id=%s",
                    (Jsonb(supplements), card["interaction_id"]))


async def clear_task_extras(c, project_id: str, task_id: str) -> None:
    card = await open_card(c, project_id)
    if card is None:
        return
    drafts = {k: v for k, v in card["drafts"].items() if k != str(task_id)}
    supplements = {k: v for k, v in card["supplements"].items() if k != str(task_id)}
    await c.execute("UPDATE core.interactions SET drafts=%s, supplements=%s WHERE interaction_id=%s",
                    (Jsonb(drafts), Jsonb(supplements), card["interaction_id"]))
