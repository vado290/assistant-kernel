"""Channel-independent questionnaire state; only final submission is an answer."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


QUESTIONNAIRE_KIND = "questionnaire.v1"


def is_questionnaire(content: dict[str, Any]) -> bool:
    return content.get("interaction_kind") == QUESTIONNAIRE_KIND


def questionnaire_eligible(questions: list[dict[str, Any]]) -> bool:
    # The legacy path retains full text for special or oversized inputs.
    if not 1 <= len(questions) <= 10:
        return False
    for question in questions:
        if question.get("input_schema"):
            return False
        options = question.get("options") or []
        if options and (not 2 <= len(options) <= 3 or any(
            not isinstance(item, dict) or not item.get("id") or not item.get("label")
            for item in options
        )):
            return False
        size = len(str(question.get("question_text") or "")) + sum(
            len(str(item.get("label") or "")) + len(str(item.get("description") or ""))
            for item in options
        )
        if size > 1200:
            return False
    return True


def make_questionnaire(
    questions: list[dict[str, Any]], *, summary: str, sequence: int,
    correlation: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "interaction_kind": QUESTIONNAIRE_KIND,
        "context_visibility": "structured",
        "questions": deepcopy(questions), "summary": summary,
        "interaction_seq": sequence, "correlation": dict(correlation),
        "questionnaire": {
            "cursor": 0, "revision": 0, "status": "draft",
            "answers": {}, "choices": {}, "additional_text": "",
        },
    }
    result["text"] = render_questionnaire(result)
    return result


def preview(value: Any, limit: int = 100) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_questionnaire(content: dict[str, Any]) -> str:
    questions = content["questions"]
    state = content["questionnaire"]
    answers = state.get("answers") or {}
    cursor = int(state["cursor"])
    if state.get("status") == "submitted" or cursor == len(questions):
        title = "答案已提交" if state.get("status") == "submitted" else "确认并提交答案"
        lines = [title, f"已回答 {len(answers)} / {len(questions)}"]
        for index, question in enumerate(questions, start=1):
            answer = answers.get(question["question_id"], "未填写（可选）")
            lines.append(f"{index}. {preview(question.get('topic') or question['question_text'], 35)}\n   {preview(answer, 90)}")
        lines.append("补充说明：" + (preview(state.get("additional_text"), 180) or "未填写"))
        lines.append("以上长答案仅显示摘要，完整原文会一并保存。")
        return "\n\n".join(lines)
    question = questions[cursor]
    qid = question["question_id"]
    lines = [
        f"{preview(question.get('topic') or '需求确认', 60)} · 第 {cursor + 1} / {len(questions)} 问",
        str(question["question_text"]),
    ]
    for index, option in enumerate(question.get("options") or [], start=1):
        recommended = "（推荐）" if option["id"] == question.get("recommended_option_id") else ""
        lines.append(f"{index}. {option['label']}{recommended}" + (
            f"\n{option['description']}" if option.get("description") else ""
        ))
    valid_ids = {item["id"] for item in question.get("options") or []}
    if question.get("recommended_option_id") in valid_ids and question.get("recommendation_reason"):
        lines.append("推荐理由：" + preview(question["recommendation_reason"], 150))
    if qid in answers:
        lines.append("当前答案：" + preview(answers[qid], 180))
    lines.append("可自由填写自己的答案；最后统一提交。" + (
        "本题必答。" if question.get("required", True) else "本题可跳过。"
    ))
    return "\n\n".join(lines)


def advance_questionnaire(
    content: dict[str, Any], *, action: str, page: int, value: str | None = None,
) -> dict[str, Any]:
    result = deepcopy(content)
    questions = result["questions"]
    state = result["questionnaire"]
    if state.get("status") != "draft" or int(state["cursor"]) != page:
        raise ValueError("questionnaire page is no longer current")
    answers = state["answers"]
    if action in {"choice", "text"}:
        if not 0 <= page < len(questions):
            raise ValueError("no question on this page")
        question = questions[page]
        qid = question["question_id"]
        if action == "choice":
            options = question.get("options") or []
            try:
                index = int(str(value))
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid choice") from exc
            if not 0 <= index < len(options):
                raise ValueError("invalid choice")
            option = options[index]
            answers[qid] = str(option["label"]) + (
                f" — {option['description']}" if option.get("description") else ""
            )
            state["choices"][qid] = str(option["id"])
        else:
            text = str(value or "").strip()
            if not text or len(text) > 4000:
                raise ValueError("answer must contain 1-4000 characters")
            answers[qid] = text
            state["choices"].pop(qid, None)
    elif action == "additional":
        if page != len(questions) or len(str(value or "")) > 4000:
            raise ValueError("invalid additional answer")
        state["additional_text"] = str(value or "").strip()
    elif action == "next":
        if page >= len(questions):
            raise ValueError("already at the final page")
        if questions[page].get("required", True) and not answers.get(questions[page]["question_id"]):
            raise ValueError("请先选择或自由填写本题答案。")
        state["cursor"] = page + 1
    elif action == "previous":
        if page <= 0:
            raise ValueError("already at the first page")
        state["cursor"] = page - 1
    elif action == "submit":
        if page != len(questions):
            raise ValueError("review all questions before submitting")
        if any(q.get("required", True) and not answers.get(q["question_id"]) for q in questions):
            raise ValueError("请先回答全部必答问题。")
        state["status"] = "submitted"
    else:
        raise ValueError("unknown questionnaire action")
    state["revision"] = int(state["revision"]) + 1
    result["text"] = render_questionnaire(result)
    return result
