"""Phase 3: the gateway against a fake Discord transport."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from assistant_kernel.core.gateway.config import GatewayConfig, ProjectChannels
from assistant_kernel.core.gateway.service import STALE_REPLY, GatewayService

from .sim import next_step, pm, relay

PM_CH, SUP_CH, USER = "100", "200", "u1"


class FakeTransport:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.order: list[str] = []

    async def send(self, channel_id, text, buttons=(), attachment=None):
        message_id = str(len(self.order) + 1)
        self.messages[message_id] = {"channel": channel_id, "text": text, "buttons": list(buttons),
                                     "attachment": attachment}
        self.order.append(message_id)
        return message_id

    async def edit(self, channel_id, message_id, text, buttons=()):
        self.messages[message_id].update(text=text, buttons=list(buttons))

    def link(self, channel_id, message_id=None):
        return f"https://discord/{channel_id}/{message_id}" if message_id else f"https://discord/{channel_id}"

    def in_channel(self, channel):
        return [self.messages[m] for m in self.order if self.messages[m]["channel"] == channel]

    def last_card(self):
        return next(m for m in reversed([self.messages[i] for i in self.order])
                    if m["channel"] == SUP_CH and m["buttons"])


@pytest.fixture
def gateway(kernel):
    config = GatewayConfig(guild_id="g", allowed_user_ids=frozenset({USER}),
                           projects={"p1": ProjectChannels("p1", PM_CH, SUP_CH)})
    return GatewayService(kernel, config, FakeTransport())


async def say(gateway, text, channel=PM_CH, message_id="m1", user=USER):
    return await gateway.on_message(channel_id=channel, user_id=user, message_id=message_id, text=text)


async def click(gateway, button_label, interaction="i1"):
    card = gateway.transport.last_card()
    custom_id = next(b["custom_id"] for b in card["buttons"] if b["label"].startswith(button_label))
    return await gateway.on_button(channel_id=SUP_CH, user_id=USER, interaction_id=interaction, custom_id=custom_id)


async def test_next_step_card_round_trip(gateway, sim):
    received = await say(gateway, "做个日报工具")
    sim.script("supervisor:evaluate", next_step("requirements", "梳理日报需求"))
    await sim.drain()
    await gateway.sync_cards()
    card = gateway.transport.last_card()
    assert "梳理日报需求" in card["text"] and [b["label"] for b in card["buttons"]] == ["执行", "修改…", "停止任务"]
    assert gateway.transport.in_channel(PM_CH)[-1]["text"].startswith("📌")
    reply = await click(gateway, "执行")
    assert reply.text == "已确认，PM 将开始执行。"
    assert (await sim.task(received["task_id"]))["state"] == "pm"
    assert "已处理" in gateway.transport.messages["1"]["text"] and not gateway.transport.messages["1"]["buttons"]


async def test_questions_are_answered_in_a_prefilled_modal(gateway, sim, kernel):
    received = await say(gateway, "做个日报工具")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await gateway.sync_cards()
    await click(gateway, "执行")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "给谁看？"}]))
    sim.script("supervisor:evaluate", {**relay("q1"), "suggested": {"q1": {"answer": "团队", "basis": "原话"}}})
    await sim.drain()
    await gateway.sync_cards()
    modal = (await click(gateway, "回答")).modal
    assert modal["fields"][0]["default"] == "团队"
    reply = await gateway.on_modal(channel_id=SUP_CH, user_id=USER, interaction_id="i2",
                                   custom_id=modal["custom_id"], values={"c_q1": "团队和老板"})
    assert reply.text == "答案已提交。"
    run = await kernel.claim("w1")
    assert run["input"]["answers"] == {"c_q1": "团队和老板"}


async def test_questions_can_be_answered_with_other_thoughts(gateway, sim):
    received = await say(gateway, "写今天测试的三条要点")
    sim.script("supervisor:evaluate", {"decision": "ask_user", "message": "缺内容",
                                       "questions": [{"id": "c1", "text": "三条要点是什么？"}]})
    await sim.drain()
    await gateway.sync_cards()
    assert [b["label"] for b in gateway.transport.last_card()["buttons"]] == ["回答", "其他想法…", "停止任务"]
    modal = (await click(gateway, "其他想法")).modal
    assert modal["title"] == "其他想法"
    reply = await gateway.on_modal(channel_id=SUP_CH, user_id=USER, interaction_id="i2", custom_id=modal["custom_id"],
                                   values={"text": "我也不清楚，你帮我查记录"})
    assert reply.text.startswith("你的想法已交给 Supervisor")
    run = await gateway.kernel.claim("w1")
    assert run["input"]["reason"] == "revise" and run["input"]["unanswered"] == ["三条要点是什么？"]
    assert (await sim.task(received["task_id"]))["state"] == "supervising"


async def test_many_questions_use_pages_and_drafts(gateway, sim, kernel):
    await say(gateway, "大任务")
    questions = [{"id": f"s{i}", "text": f"问题{i}"} for i in range(7)]
    sim.script("supervisor:evaluate", {"decision": "ask_user", "message": "要问几件事", "questions": questions})
    await sim.drain()
    await gateway.sync_cards()
    card = gateway.transport.last_card()
    assert [b["label"] for b in card["buttons"]] == ["回答 1-5", "回答 6-7", "其他想法…", "停止任务"]
    first = (await click(gateway, "回答 1-5")).modal
    reply = await gateway.on_modal(channel_id=SUP_CH, user_id=USER, interaction_id="i2", custom_id=first["custom_id"],
                                   values={f"s{i}": f"答{i}" for i in range(5)})
    assert reply.text.startswith("已保存这一页")
    second = (await click(gateway, "回答 6-7", interaction="i3")).modal
    reply = await gateway.on_modal(channel_id=SUP_CH, user_id=USER, interaction_id="i4", custom_id=second["custom_id"],
                                   values={"s5": "答5", "s6": "答6"})
    assert reply.text == "答案已提交。"
    run = await kernel.claim("w1")
    assert run["input"]["answers"] == {f"s{i}": f"答{i}" for i in range(7)}


async def test_stale_click_and_revise(gateway, sim):
    received = await say(gateway, "做日报")
    sim.script("supervisor:evaluate", next_step("requirements", "日报"))
    await sim.drain()
    await gateway.sync_cards()
    old = gateway.transport.last_card()
    await say(gateway, "改成周报", message_id="m2")
    sim.script("supervisor:review_input", {"decision": "upgrade", "item": next_step("requirements", "周报")})
    await sim.drain()
    await gateway.sync_cards()
    assert "已处理" in old["text"]
    reply = await gateway.on_button(channel_id=SUP_CH, user_id=USER, interaction_id="i9",
                                    custom_id=old["buttons"][0]["custom_id"] if old["buttons"] else "core:x:execute")
    assert reply.text == STALE_REPLY
    modal = (await click(gateway, "修改")).modal
    reply = await gateway.on_modal(channel_id=SUP_CH, user_id=USER, interaction_id="i10",
                                   custom_id=modal["custom_id"], values={"text": "只要周五的"})
    assert reply.text.startswith("你的想法已交给 Supervisor")
    assert (await sim.task(received["task_id"]))["state"] == "supervising"


async def test_outbox_reaches_the_right_channel(gateway, sim):
    await say(gateway, "做日报")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await say(gateway, "进展如何？", channel=SUP_CH, message_id="m2")
    sim.script("supervisor:consult", {"reply": "在等你确认"})
    await sim.drain()
    assert await gateway.deliver_outbox() == 1
    assert gateway.transport.in_channel(SUP_CH)[-1]["text"] == "在等你确认"
    assert await gateway.deliver_outbox() == 0


async def test_strangers_and_unknown_channels_are_ignored(gateway, kernel):
    assert await say(gateway, "hi", user="intruder") is None
    assert await say(gateway, "hi", channel="999") is None
    assert await kernel.active_task("p1") is None


async def test_reminders_after_one_hour_once(gateway, sim):
    await say(gateway, "做日报")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await gateway.sync_cards()
    later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
    await gateway.remind(now=later)
    await gateway.remind(now=later)
    reminders = [m for m in gateway.transport.messages.values() if m["text"].startswith("⏰")]
    assert len(reminders) == 2          # one in each channel, sent once


async def test_text_commands_in_shared_bot_mode(gateway, sim):
    await say(gateway, "做日报工具")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    assert (await say(gateway, "/status", message_id="m2"))["command"] == "/status"
    assert "做日报工具" in gateway.transport.in_channel(PM_CH)[-1]["text"]
    await say(gateway, "/cancel", message_id="m3")
    assert "/cancel 确认" in gateway.transport.in_channel(PM_CH)[-1]["text"]
    await say(gateway, "/cancel 确认", message_id="m4")
    assert gateway.transport.in_channel(PM_CH)[-1]["text"].startswith("已取消任务")


async def test_evolve_command_queues_one_review(gateway, sim):
    assert (await say(gateway, "/evolve", channel=SUP_CH))["command"] == "/evolve"
    await say(gateway, "/evolve", channel=SUP_CH)                       # same Discord message: no duplicate
    assert "复盘" in gateway.transport.in_channel(SUP_CH)[-1]["text"]
    task = await gateway.kernel.active_task("p1")
    assert task["kind"] == "evolution" and task["state"] == "pm" and task["origin"] == "user"


async def test_status_and_cancel_commands(gateway, sim):
    await say(gateway, "做日报工具")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await gateway.sync_cards()
    assert "做日报工具" in await gateway.status_text(PM_CH)
    assert "待处理卡片" in await gateway.pending_text(SUP_CH)
    assert (await gateway.cancel_active(PM_CH, USER)).startswith("已取消任务")
    card = gateway.transport.messages["1"]
    assert "已处理" in card["text"] and not card["buttons"]     # the cancelled task's card is retired
