"""Gateway logic, independent of the Discord library (a Transport does the actual sending)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from assistant_kernel.core.cards import StaleItem
from assistant_kernel.core.gateway import render
from assistant_kernel.core.gateway.config import GatewayConfig
from assistant_kernel.core.kernel import Kernel

log = logging.getLogger(__name__)
STALE_REPLY = "这张卡片已更新或已处理，请看 Supervisor 频道里最新的卡片。"
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
        if command[0] in ("/status", "/pending", "/cancel", "/evolve", "/jobs", "/iterations", "/grants"):   # commands
            if command[0] == "/grants":
                reply = await self.grants_command(project_id, command[1:])
            elif command[0] == "/iterations":
                reply = await self.iterations_text(project_id, command[1:])
            elif command[0] == "/jobs":
                reply = await self.jobs_command(project_id, command[1:])
            elif command[0] == "/evolve":
                await self.kernel.start_evolution(project_id, idempotency_key=f"evolution:{project_id}:msg:{message_id}")
                reply = "已安排一次复盘：PM 会回顾最近的运行记录并提出改进建议，结果在 Supervisor 频道的卡片里由你决定。"
            elif command[0] == "/status":
                reply = await self.status_text(channel_id)
            elif command[0] == "/pending":
                reply = await self.pending_text(channel_id)
            elif command[1:2] == ["确认"]:
                reply = await self.cancel_active(channel_id, user_id)
            else:
                reply = "确定要取消当前任务吗？请发送：/cancel 确认"
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
            return Reply("你没有权限操作这张卡片。")
        _, item_id, action = custom_id.split(":", 2)
        card, item = await self._item(route[0], item_id)
        if item is None:
            return Reply(STALE_REPLY)
        if action.startswith("answer:"):
            page = int(action.split(":")[1])
            drafts = (card["drafts"] or {}).get(str(item["target"]["id"]), {})
            fields = [{"id": q["id"], "label": q["text"][:45], "required": bool(q.get("required", True)),
                       "default": str(drafts.get(q["id"]) or render.suggested_answer(item, q))[:4000],
                       "placeholder": ("可选：" + " / ".join(map(str, q["options"])))[:100] if q.get("options") else None,
                       "long_text": q["text"]}
                      for q in render.question_pages(item)[page]]
            return Reply(modal={"custom_id": f"coremodal:{item_id}:answer:{page}", "title": "回答问题", "fields": fields})
        if action == "revise":
            questions = item["kind"] == "questions"
            return Reply(modal={"custom_id": f"coremodal:{item_id}:revise",
                                "title": "其他想法" if questions else "修改下一步",
                                "fields": [{"id": "text", "required": True, "default": "", "paragraph": True,
                                            "label": "不回答也可以，说说你的想法" if questions else "你希望怎么改？",
                                            "placeholder": "例如：这些我不清楚，你帮我查；或者：先不做这个，改做…"
                                            if questions else None}]})
        return await self._act(route[0], item_id, action, {}, user_id, interaction_id)

    async def on_modal(self, *, channel_id: str, user_id: str, interaction_id: str, custom_id: str,
                       values: dict[str, str]) -> Reply:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id):
            return Reply("你没有权限操作这张卡片。")
        project_id = route[0]
        _, item_id, kind, *rest = custom_id.split(":")
        if kind == "revise":
            return await self._act(project_id, item_id, "revise", {"text": values.get("text", "")}, user_id, interaction_id)
        card, item = await self._item(project_id, item_id)
        if item is None:
            return Reply(STALE_REPLY)
        task_id = str(item["target"]["id"])
        answers = {**(card["drafts"] or {}).get(task_id, {}), **{k: v for k, v in values.items() if v.strip()}}
        pages = render.question_pages(item)
        page = int(rest[0]) if rest else 0
        if len(pages) > 1:
            await self.kernel.save_draft(project_id, task_id, answers)
        missing = [q["text"] for q in item["content"]["questions"] if q.get("required", True) and not answers.get(q["id"])]
        if missing and page < len(pages) - 1:
            return Reply(f"已保存这一页，请继续点「回答 {(page + 1) * render.MODAL_FIELDS + 1}-…」。")
        if missing:
            return Reply("还有必答题没有填写：" + "；".join(missing[:5]))
        return await self._act(project_id, item_id, "submit", {"answers": answers}, user_id, interaction_id)

    async def _act(self, project_id, item_id, action, values, user_id, interaction_id) -> Reply:
        try:
            await self.kernel.act(project_id, item_id, action, values, user_id=user_id,
                                  external_event_id=f"click:{interaction_id}")
        except StaleItem as exc:
            return Reply(f"{STALE_REPLY}（{exc}）")
        except ValueError as exc:
            return Reply(f"无法执行：{exc}")
        await self.sync_cards(project_id)
        return Reply(render.DONE_REPLIES.get(action, "已记录。"))

    # ------------------------------------------------------------ outbound

    async def sync_cards(self, project_id: str | None = None) -> None:
        """Make the channel show exactly the open card's items; retire messages of handled items."""
        for pid in ([project_id] if project_id else list(self.config.projects)):
            channels = self.config.projects.get(pid)
            if channels is None:
                continue
            for card in await self.kernel.displayed_cards(pid):
                await self._sync_card(channels, card)

    async def _sync_card(self, channels, card: dict) -> None:
        display = {"messages": {}, "reminded": {}, **(card["display"] or {})}
        current = {i["item_id"]: i for i in card["items"]} if card["status"] == "open" else {}
        changed = False
        for item_id, shown in list(display["messages"].items()):
            if item_id not in current:
                await self.transport.edit(shown["channel_id"], shown["message_id"], render.closed_text(shown["item"]))
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
                                          render.pointer_text(self.transport.link(channels.supervisor_channel_id, message_id)))
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
            return "（目标项目的频道未配置）"
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
                        await self.transport.send(channel, f"⏰ 有一项卡片已等待约 {hours} 小时：{link}")
                    display["reminded"][item["item_id"]] = level + 1
                    changed = True
            if changed:
                await self.kernel.set_card_display(card["interaction_id"], display)

    # ------------------------------------------------------------ slash commands

    async def grants_command(self, project_id: str, args: list[str]) -> str:
        """/grants · /grants revoke <key> [确认] (ROUND4 R4.4)."""
        from assistant_kernel.core import grants
        if not args:
            return grants.render(await self.kernel.grants(project_id))
        if args[0] != "revoke" or len(args) not in (2, 3):
            return "用法：/grants · /grants revoke <key>"
        key = args[1]
        if args[2:] != ["确认"]:
            return f"确定要撤销目录授权 {key} 吗？撤销后本项目从下一轮起不能再访问它。请发送：/grants revoke {key} 确认"
        if await self.kernel.revoke_grant(project_id, key):
            return f"已撤销目录授权 {key}（从下一轮起生效）。"
        return f"没有找到有效的目录授权 {key}。发送 /grants 查看。"

    async def iterations_text(self, project_id: str, args: list[str]) -> str:
        """/iterations [days]: the project's recent changes and why (ROUND4 R4.1), one line each."""
        from assistant_kernel.core import iterlog
        days = int(args[0]) if args and args[0].isdigit() else 14
        rows = await self.kernel.iterations(project_id, days=days, limit=20)
        return f"最近 {days} 天的变更记录（最多 20 条）：\n" + iterlog.render(rows, brief=True)

    async def jobs_command(self, project_id: str, args: list[str]) -> str:
        """/jobs · /jobs pause <id> · /jobs resume <id> · /jobs run <id> (ROUND3 S3)."""
        from assistant_kernel.core import jobs
        if not args:
            return jobs.render_jobs(await jobs.list_jobs(self.kernel, project_id))
        if len(args) != 2 or args[0] not in ("pause", "resume", "run"):
            return "用法：/jobs · /jobs pause <id> · /jobs resume <id> · /jobs run <id>"
        action, job_id = args
        if action == "run":
            run = await jobs.run_now(self.kernel, project_id, job_id)
            return (f"已安排立即运行作业 {job_id}（不影响下次定时）；失败会在 Supervisor 频道提醒。" if run
                    else f"没有找到作业 {job_id}。")
        job = await jobs.set_status(self.kernel, project_id, job_id, "paused" if action == "pause" else "enabled")
        if job is None:
            return f"没有找到作业 {job_id}（服务不能暂停，只能通过修改 jobs.yaml 停用）。"
        return f"作业 {job_id} 已暂停。" if action == "pause" else f"作业 {job_id} 已恢复。"

    async def status_text(self, channel_id: str) -> str:
        route = self.config.route(channel_id)
        if route is None:
            return "这个频道不属于任何项目。"
        task = await self.kernel.active_task(route[0])
        card = await self.kernel.card(route[0])
        pending = len((card or {}).get("items", []))
        if task is None:
            return f"项目 {route[0]} 当前没有进行中的任务。待处理卡片：{pending} 项。"
        return (f"项目 {route[0]}\n任务：{task['goal'][:200]}\n阶段：{render.STAGE_NAMES.get(task['stage'], task['stage'])}"
                f"　状态：{task['state']}　已执行 PM 回合：{task['turn_count']}\n待处理卡片：{pending} 项")

    async def pending_text(self, channel_id: str) -> str:
        route = self.config.route(channel_id)
        card = await self.kernel.card(route[0]) if route else None
        if not card or not card["items"]:
            return "当前没有待处理的卡片。"
        lines = []
        for item in card["items"]:
            shown = (card["display"] or {}).get("messages", {}).get(item["item_id"])
            link = self.transport.link(shown["channel_id"], shown["message_id"]) if shown else "（正在发送）"
            lines.append(f"- {render.render_item(item)['text'].splitlines()[0]} {link}")
        return "待处理卡片：\n" + "\n".join(lines)

    async def cancel_active(self, channel_id: str, user_id: str) -> str:
        route = self.config.route(channel_id)
        if route is None or not self.config.allowed(user_id):
            return "你没有权限执行这个操作。"
        task = await self.kernel.active_task(route[0])
        if task is None:
            return "当前没有进行中的任务。"
        if task["state"] in ("publishing", "bootstrapping"):   # its effects are being applied right now
            return "任务正在发布（或创建项目），这一步无法中途取消；完成后会通知你，届时再决定是否需要撤回。"
        await self.kernel.cancel_task(task["task_id"])
        await self.sync_cards(route[0])
        return f"已取消任务：{task['goal'][:100]}"
