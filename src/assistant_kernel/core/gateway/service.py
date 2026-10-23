"""Gateway logic, independent of the Discord library (a Transport does the actual sending)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from assistant_kernel.core import i18n
from assistant_kernel.core.i18n import t
from assistant_kernel.core.cards import StaleItem
from assistant_kernel.core.gateway import render
from assistant_kernel.core.gateway.config import GatewayConfig
from assistant_kernel.core.kernel import Kernel

log = logging.getLogger(__name__)
REMINDERS = (timedelta(hours=1), timedelta(hours=24))
UNDELIVERABLE_AFTER = timedelta(minutes=30)


class Transport(Protocol):
    async def send(self, channel_id: str, text: str, buttons: list[dict] = (), attachment: str | None = None) -> str: ...
    async def edit(self, channel_id: str, message_id: str, text: str, buttons: list[dict] = ()) -> None: ...
    def link(self, channel_id: str, message_id: str | None = None) -> str: ...


@dataclass
class Reply:
    """What the gateway answers a click with: an ephemeral text, or a modal to open."""
    text: str | None = None
    modal: dict[str, Any] | None = None


class GatewayService:
    def __init__(self, kernel: Kernel, config: GatewayConfig, transport: Transport):
        self.kernel, self.config, self.transport = kernel, config, transport

    # ------------------------------------------------------------ inbound

    async def on_message(self, *, channel_id: str, user_id: str, message_id: str, text: str) -> dict | None:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id) or not text.strip():
            return None
        project_id, role = route
        command = text.strip().split()
        if command[0] in ("/status", "/pending", "/cancel", "/evolve", "/jobs", "/iterations", "/grants",
                          "/secrets"):   # commands
            if command[0] == "/secrets":
                reply = await self.secrets_command(project_id, command[1:])
            elif command[0] == "/grants":
                reply = await self.grants_command(project_id, command[1:])
            elif command[0] == "/iterations":
                reply = await self.iterations_text(project_id, command[1:])
            elif command[0] == "/jobs":
                reply = await self.jobs_command(project_id, command[1:])
            elif command[0] == "/evolve":
                await self.kernel.start_evolution(project_id, idempotency_key=f"evolution:{project_id}:msg:{message_id}")
                reply = t("gw.evolve")
            elif command[0] == "/status":
                reply = await self.status_text(channel_id)
            elif command[0] == "/pending":
                reply = await self.pending_text(channel_id)
            elif confirmed(command[1:2]):
                reply = await self.cancel_active(channel_id, user_id)
            else:
                reply = t("gw.cancel_confirm", word=t("cmd.confirm_word"))
            await self.transport.send(channel_id, reply)
            return {"action": "command", "command": command[0]}
        return await self.kernel.receive(project_id, role=role, text=text, channel="discord", chat_id=channel_id,
                                         user_id=user_id, external_event_id=f"msg:{message_id}")

    async def _item(self, project_id: str, item_id: str) -> tuple[dict | None, dict | None]:
        card = await self.kernel.card(project_id)
        item = next((i for i in (card or {}).get("items", []) if i["item_id"] == item_id), None)
        return card, item

    async def on_button(self, *, channel_id: str, user_id: str, interaction_id: str, custom_id: str) -> Reply:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id):
            return Reply(t("gw.no_permission"))
        _, item_id, action = custom_id.split(":", 2)
        card, item = await self._item(route[0], item_id)
        if item is None:
            return Reply(t("gw.stale"))
        if action.startswith("answer:"):
            page = int(action.split(":")[1])
            drafts = (card["drafts"] or {}).get(str(item["target"]["id"]), {})
            fields = [self._field(q, drafts, item) for q in render.question_pages(item)[page]]
            return Reply(modal={"custom_id": f"coremodal:{item_id}:answer:{page}", "title": t("gw.modal_answer"),
                                "fields": fields})
        if action == "revise":
            questions = item["kind"] == "questions"
            return Reply(modal={"custom_id": f"coremodal:{item_id}:revise",
                                "title": t("gw.modal_other") if questions else t("gw.modal_revise"),
                                "fields": [{"id": "text", "required": True, "default": "", "paragraph": True,
                                            "label": t("gw.other_label") if questions else t("gw.revise_label"),
                                            "placeholder": t("gw.other_placeholder") if questions else None}]})
        return await self._act(route[0], item_id, action, {}, user_id, interaction_id)

    async def on_modal(self, *, channel_id: str, user_id: str, interaction_id: str, custom_id: str,
                       values: dict[str, str]) -> Reply:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id):
            return Reply(t("gw.no_permission"))
        project_id = route[0]
        _, item_id, kind, *rest = custom_id.split(":")
        if kind == "revise":
            return await self._act(project_id, item_id, "revise", {"text": values.get("text", "")}, user_id, interaction_id)
        card, item = await self._item(project_id, item_id)
        if item is None:
            return Reply(t("gw.stale"))
        task_id = str(item["target"]["id"])
        answers = {**(card["drafts"] or {}).get(task_id, {}), **{k: v for k, v in values.items() if v.strip()}}
        pages = render.question_pages(item)
        page = int(rest[0]) if rest else 0
        if len(pages) > 1:
            await self.kernel.save_draft(project_id, task_id, answers)
        missing = [q["text"] for q in item["content"]["questions"] if q.get("required", True) and not answers.get(q["id"])]
        if missing and page < len(pages) - 1:
            return Reply(t("gw.page_saved", start=(page + 1) * render.MODAL_FIELDS + 1))
        if missing:
            return Reply(t("gw.missing", list=t("list.separator").join(missing[:5])))
        return await self._act(project_id, item_id, "submit", {"answers": answers}, user_id, interaction_id)

    async def _act(self, project_id, item_id, action, values, user_id, interaction_id) -> Reply:
        try:
            await self.kernel.act(project_id, item_id, action, values, user_id=user_id,
                                  external_event_id=f"click:{interaction_id}")
        except StaleItem as exc:
            return Reply(f"{t('gw.stale')} ({exc})")
        except ValueError as exc:
            return Reply(t("gw.cannot", error=exc))
        await self.sync_cards(project_id)
        return Reply(render.done_reply(action))

    # ------------------------------------------------------------ outbound

    async def sync_cards(self, project_id: str | None = None) -> None:
        """Make the channel show exactly the open card's items; retire messages of handled items."""
        for pid in ([project_id] if project_id else list(self.config.projects)):
            channels = self.config.projects.get(pid)
            if channels is None:
                continue
            for card in await self.kernel.displayed_cards(pid):
                try:   # one broken card must not keep every other card from showing
                    await self._sync_card(channels, card)
                except Exception:
                    log.exception("syncing card %s of %s failed", card["interaction_id"], pid)

    async def _sync_card(self, channels, card: dict) -> None:
        display = {"messages": {}, "reminded": {}, **(card["display"] or {})}
        current = {i["item_id"]: i for i in card["items"]} if card["status"] == "open" else {}
        changed = False
        for item_id, shown in list(display["messages"].items()):
            if item_id not in current:
                try:
                    await self.transport.edit(shown["channel_id"], shown["message_id"], render.closed_text(shown["item"]))
                except Exception:   # e.g. the user deleted the message: nothing left to retire
                    log.warning("could not retire card message %s; dropped", shown["message_id"], exc_info=True)
                del display["messages"][item_id]
                changed = True
        for item_id, item in current.items():
            if item_id in display["messages"]:
                continue
            body = render.render_item(item)
            message_id = await self.transport.send(channels.supervisor_channel_id, body["text"], body["buttons"],
                                                   body["attachment"])
            display["messages"][item_id] = {"channel_id": channels.supervisor_channel_id, "message_id": message_id,
                                            "item": {"kind": item["kind"]}}
            if item["target"]["kind"] == "task":
                await self.transport.send(channels.pm_channel_id,
                                          render.pointer_text(self.transport.link(channels.supervisor_channel_id,
                                                                                  message_id)))
            changed = True
        if changed:
            await self.kernel.set_card_display(card["interaction_id"], display)

    async def deliver_outbox(self) -> int:
        delivered = 0
        for message in await self.kernel.pending_outbound():
            # Undeliverable for long: give up on it, so it cannot keep every other project's messages waiting.
            expired = datetime.now(timezone.utc) - message["created_at"] > UNDELIVERABLE_AFTER
            channels = self.config.projects.get(message["project_id"])
            if channels is None:   # no routing (yet: a new project's routing is picked up within seconds)
                if expired:
                    log.warning("message %s has no channel for project %s; marked failed",
                                message["message_id"], message["project_id"])
                    await self.kernel.mark_delivered(message["message_id"], external_id=None, ok=False)
                continue
            channel_id = (message["chat_id"] if message["channel"] == "discord" and message["chat_id"]
                          else channels.supervisor_channel_id if message["role"] == "supervisor"
                          else channels.pm_channel_id)
            text = message["body"].get("text", "")
            if message["body"].get("link_message"):
                text += f"\n👉 {await self._link_to(message['body']['link_message'])}"
            try:
                external = await self.transport.send(channel_id, text)
            except Exception:
                log.exception("delivery of message %s failed; %s", message["message_id"],
                              "marked failed" if expired else "will retry")
                if expired:
                    await self.kernel.mark_delivered(message["message_id"], external_id=None, ok=False)
                continue
            await self.kernel.mark_delivered(message["message_id"], external_id=f"out:{external}")
            delivered += 1
        return delivered

    async def _link_to(self, message_id: str) -> str:
        """Link to an outbound message (e.g. a handoff note in another project's Supervisor channel);
        the channel itself when that message has not been delivered yet."""
        target = await self.kernel.get_message(message_id)
        channels = self.config.projects.get(target["project_id"]) if target else None
        if channels is None:
            return t("gw.no_channel")
        external = str(target.get("external_event_id") or "")
        sent = external.removeprefix("out:") if external.startswith("out:") else None
        return self.transport.link(channels.supervisor_channel_id, sent)

    async def remind(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for pid, channels in self.config.projects.items():
            card = await self.kernel.card(pid)
            if not card:
                continue
            display = {"messages": {}, "reminded": {}, **(card["display"] or {})}
            changed = False
            for item in card["items"]:
                shown = display["messages"].get(item["item_id"])
                level = display["reminded"].get(item["item_id"], 0)
                age = now - datetime.fromisoformat(item["created_at"])
                if shown and level < len(REMINDERS) and age >= REMINDERS[level]:
                    link = self.transport.link(shown["channel_id"], shown["message_id"])
                    hours = int(age.total_seconds() // 3600)
                    for channel in (channels.pm_channel_id, channels.supervisor_channel_id):
                        await self.transport.send(channel, t("gw.reminder", hours=hours, link=link))
                    display["reminded"][item["item_id"]] = level + 1
                    changed = True
            if changed:
                await self.kernel.set_card_display(card["interaction_id"], display)

    # ------------------------------------------------------------ slash commands

    @staticmethod
    def _field(q: dict, drafts: dict, item: dict) -> dict:
        """One modal field; a credential question gets no suggested answer and says where the answer goes."""
        credential = q.get("credential") if isinstance(q.get("credential"), dict) else None
        if credential:
            placeholder = i18n.t("gw.credential_placeholder", key=credential.get("key"))[:100]
            default = ""
        else:
            placeholder = i18n.t("gw.optional_options", options=" / ".join(map(str, q["options"])))[:100] \
                if q.get("options") else None
            default = str(drafts.get(q["id"]) or render.suggested_answer(item, q))[:4000]
        return {"id": q["id"], "label": q["text"][:45], "required": bool(q.get("required", True)), "default": default,
                "placeholder": placeholder, "long_text": q["text"]}

    async def secrets_command(self, project_id: str, args: list[str]) -> str:
        """/secrets · /secrets delete <key> [confirm] (ROUND5 D8): the project's credentials, never their values."""
        from assistant_kernel.core import credentials
        from assistant_kernel.storage import ProjectRuntimeManager
        vault = ProjectRuntimeManager(self.kernel.settings.data_root).ensure(project_id).vault_root
        if not args:
            return credentials.render(credentials.listing(vault))
        if args[0] != "delete" or len(args) not in (2, 3):
            return t("cmd.secrets_usage")
        if not confirmed(args[2:]):
            return t("cmd.secrets_confirm", key=args[1], word=t("cmd.confirm_word"))
        return t("cmd.secrets_deleted", key=args[1]) if credentials.delete(vault, args[1]) else \
            t("cmd.secrets_missing", key=args[1])

    async def grants_command(self, project_id: str, args: list[str]) -> str:
        """/grants · /grants revoke <key> [confirm] (ROUND4 R4.4)."""
        from assistant_kernel.core import grants
        if not args:
            return grants.render(await self.kernel.grants(project_id))
        if args[0] != "revoke" or len(args) not in (2, 3):
            return t("cmd.grants_usage")
        key = args[1]
        if not confirmed(args[2:]):
            return t("cmd.grants_confirm", key=key, word=t("cmd.confirm_word"))
        if await self.kernel.revoke_grant(project_id, key):
            return t("cmd.grants_revoked", key=key)
        return t("cmd.grants_missing", key=key)

    async def iterations_text(self, project_id: str, args: list[str]) -> str:
        """/iterations [days]: the project's recent changes and why (ROUND4 R4.1), one line each."""
        from assistant_kernel.core import iterlog
        days = int(args[0]) if args and args[0].isdigit() else 14
        rows = await self.kernel.iterations(project_id, days=days, limit=20)
        return t("cmd.iterations", days=days) + "\n" + \
            iterlog.render(rows, brief=True)

    async def jobs_command(self, project_id: str, args: list[str]) -> str:
        """/jobs · /jobs pause <id> · /jobs resume <id> · /jobs run <id> (ROUND3 S3)."""
        from assistant_kernel.core import jobs
        if not args:
            return jobs.render_jobs(await jobs.list_jobs(self.kernel, project_id))
        if len(args) != 2 or args[0] not in ("pause", "resume", "run"):
            return t("cmd.jobs_usage")
        action, job_id = args
        if action == "run":
            run = await jobs.run_now(self.kernel, project_id, job_id)
            return t("cmd.job_run", job=job_id) if run else t("cmd.job_missing", job=job_id)
        job = await jobs.set_status(self.kernel, project_id, job_id, "paused" if action == "pause" else "enabled")
        if job is None:
            return t("cmd.job_missing_service", job=job_id)
        return t("cmd.job_paused", job=job_id) if action == "pause" else t("cmd.job_resumed", job=job_id)

    async def status_text(self, channel_id: str) -> str:
        route = self.config.route(channel_id)
        if route is None:
            return t("cmd.no_project")
        task = await self.kernel.active_task(route[0])
        card = await self.kernel.card(route[0])
        pending = len((card or {}).get("items", []))
        if task is None:
            return t("cmd.status_idle", project=route[0], pending=pending)
        return t("cmd.status", project=route[0], goal=task["goal"][:200], state=task["state"],
                 stage=render.stage_name(task["stage"]), turns=task["turn_count"], pending=pending)

    async def pending_text(self, channel_id: str) -> str:
        route = self.config.route(channel_id)
        card = await self.kernel.card(route[0]) if route else None
        project_id = route[0] if route else None
        if not card or not card["items"]:
            return t("cmd.no_pending")
        lines = []
        for item in card["items"]:
            shown = (card["display"] or {}).get("messages", {}).get(item["item_id"])
            link = self.transport.link(shown["channel_id"], shown["message_id"]) if shown \
                else t("cmd.sending")
            lines.append(f"- {render.render_item(item)['text'].splitlines()[0]} {link}")
        return t("cmd.pending") + "\n" + "\n".join(lines)

    async def cancel_active(self, channel_id: str, user_id: str) -> str:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id):
            return t("cmd.no_permission")
        task = await self.kernel.active_task(route[0])
        if task is None:
            return t("cmd.no_task")
        if task["state"] in ("publishing", "bootstrapping"):   # its effects are being applied right now
            return t("cmd.cancel_publishing")
        try:
            result = await self.kernel.cancel_task(task["task_id"])
        except ValueError as exc:
            return str(exc)
        if result["state"] == "publishing":
            return t("cmd.cancel_published")
        await self.sync_cards(route[0])
        return t("cmd.cancelled", goal=task["goal"][:100])


def confirmed(words: list[str]) -> bool:
    """A command's confirm word ("confirm"; "确认" still works)."""
    return len(words) == 1 and words[0].casefold() in i18n.CONFIRM_WORDS
