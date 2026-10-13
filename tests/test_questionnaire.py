from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from assistant_kernel.services.discord import (
    KernelDiscordClient, KernelQuestionnaireModal, build_discord_view, render_discord_text,
)
from assistant_kernel.services.questionnaire import (
    advance_questionnaire, make_questionnaire, questionnaire_eligible,
)
from assistant_kernel.services.run_processor import prompt_message_content


def questions():
    return [
        {"question_id": "scope", "topic": "功能范围", "question_text": "实现哪些功能？",
         "required": True, "options": [
             {"id": "small", "label": "最小版本", "description": "先验证核心流程"},
             {"id": "all", "label": "完整版本"},
         ], "recommended_option_id": "small", "recommendation_reason": "当前先验证核心需求"},
        {"question_id": "environment", "topic": "部署环境", "question_text": "部署在哪里？", "required": True},
    ]


def card():
    return {
        "message_id": "11111111-1111-1111-1111-111111111111",
        "message_type": "question", "chat_id": "200", "user_id": "300",
        "content_json": make_questionnaire(questions(), summary="需求确认", sequence=1, correlation={}),
    }


def test_questionnaire_navigation_free_answers_and_final_comments():
    body = card()["content_json"]
    with pytest.raises(ValueError, match="先选择"):
        advance_questionnaire(body, action="next", page=0)
    body = advance_questionnaire(body, action="choice", page=0, value="0")
    assert body["questionnaire"]["status"] == "draft"
    body = advance_questionnaire(body, action="text", page=0, value="先实现不同的范围")
    assert body["questionnaire"]["choices"] == {}
    body = advance_questionnaire(body, action="next", page=0)
    body = advance_questionnaire(body, action="text", page=1, value="我的测试服务器")
    body = advance_questionnaire(body, action="next", page=1)
    body = advance_questionnaire(body, action="additional", page=2, value="必须保留已有数据")
    body = advance_questionnaire(body, action="previous", page=2)
    body = advance_questionnaire(body, action="next", page=1)
    body = advance_questionnaire(body, action="submit", page=2)
    assert body["questionnaire"]["additional_text"] == "必须保留已有数据"
    assert body["questionnaire"]["status"] == "submitted"
    assert questions()[0]["question_text"] == body["questions"][0]["question_text"]
    assert "questionnaire" not in prompt_message_content({"message_type": "question", "content_json": body})


def test_questionnaire_oversized_special_inputs_fallback_without_losing_original():
    assert questionnaire_eligible(questions())
    long = [{**questions()[0], "question_text": "原文" * 700}]
    assert not questionnaire_eligible(long)
    assert not questionnaire_eligible([{**questions()[0], "input_schema": {"kind": "project_credential"}}])
    body = card()["content_json"]
    with pytest.raises(ValueError, match="invalid choice"):
        advance_questionnaire(body, action="choice", page=0, value="-1")
    with pytest.raises(ValueError):
        advance_questionnaire(body, action="submit", page=0)
    assert body["questionnaire"]["answers"] == {}


@pytest.mark.asyncio
async def test_views_rebuild_durable_pages_and_always_offer_free_input():
    client = SimpleNamespace(handle_kernel_component=AsyncMock())
    message = card()
    first = build_discord_view(message, client)
    assert first.is_persistent()
    assert "最小版本（推荐）" in [item.label for item in first.children]
    assert "其他 / 自由填写" in [item.label for item in first.children]
    assert "推荐理由" in render_discord_text(message)
    assert all(len(item.custom_id) <= 100 for item in first.children)
    body = message["content_json"]
    for page in range(2):
        body = advance_questionnaire(body, action="text", page=page, value=f"答案{page}")
        body = advance_questionnaire(body, action="next", page=page)
    message["content_json"] = body
    review = build_discord_view(message, client)
    assert "自由补充说明" in [item.label for item in review.children]
    assert "提交全部答案" in [item.label for item in review.children]
    message["content_json"] = advance_questionnaire(body, action="submit", page=2)
    assert build_discord_view(message, client) is None


@pytest.mark.asyncio
async def test_free_text_modal_round_trips_original_revision_and_full_text():
    client = SimpleNamespace(handle_questionnaire_component=AsyncMock())
    message = card()
    modal = KernelQuestionnaireModal(client, message, revision=0, page=0, additional=False)
    assert isinstance(modal.children[0], discord.ui.Label)
    modal.answer._value = "使用自己的方案，而不是两个选项"
    interaction = SimpleNamespace()
    await modal.on_submit(interaction)
    assert client.handle_questionnaire_component.await_args.args[2:4] == (
        "q:0:0:text", "使用自己的方案，而不是两个选项",
    )


def test_answer_semantics_exclude_navigation_and_delivery_metadata():
    content = {
        "schema_version": "questionnaire-answer.v1", "answers": {"scope": "自由答案"},
        "questions": questions(), "choice_ids": {}, "additional_text": "补充",
        "event_request": {"revision": 5, "page": 2}, "raw": {"private": "secret"},
    }
    semantic = prompt_message_content({"message_type": "answer", "content_json": content})
    assert semantic == {key: content[key] for key in ("questions", "answers", "choice_ids", "additional_text")}


@pytest.mark.asyncio
async def test_modal_opens_without_database_wait_and_stale_submit_keeps_card():
    message = {**card(), "project_id": "demo"}
    response = SimpleNamespace(
        send_modal=AsyncMock(), is_done=lambda: False, send_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        channel_id=200, user=SimpleNamespace(id=300), response=response,
        followup=SimpleNamespace(send=AsyncMock()), id=123,
    )
    client = SimpleNamespace(
        config={}, _component_message=AsyncMock(return_value=message),
        _ack_interaction=AsyncMock(return_value=SimpleNamespace(status="ok")),
        _refresh_questionnaire_card=AsyncMock(), _queue_component_retry_notice=AsyncMock(),
        repository=SimpleNamespace(record_questionnaire_action=AsyncMock(side_effect=ValueError("页面已更新"))),
    )
    await KernelDiscordClient.handle_questionnaire_component(
        client, interaction, message["message_id"], "q:0:0:write", None, message,
    )
    response.send_modal.assert_awaited_once()
    client._component_message.assert_not_awaited()
    client.repository.record_questionnaire_action.assert_not_awaited()
    response.is_done = lambda: True
    await KernelDiscordClient.handle_questionnaire_component(
        client, interaction, message["message_id"], "q:0:0:text", "旧表单答案", message,
    )
    client._refresh_questionnaire_card.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == "页面已更新"


@pytest.mark.asyncio
async def test_wrong_user_cannot_open_free_text_form():
    message = card()
    interaction = SimpleNamespace(
        channel_id=200, user=SimpleNamespace(id=999),
        response=SimpleNamespace(send_modal=AsyncMock(), is_done=lambda: False, send_message=AsyncMock()),
    )
    client = SimpleNamespace(_refresh_questionnaire_card=AsyncMock(side_effect=PermissionError()))
    await KernelDiscordClient.handle_questionnaire_component(
        client, interaction, message["message_id"], "q:0:0:write", None, message,
    )
    interaction.response.send_modal.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
