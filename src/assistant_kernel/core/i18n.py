"""Text and language rules (ROUND6): interface text, document headings, and the language of model replies.

The interface is English: every text the user reads from Kernel itself (cards, buttons, notices, commands, files Kernel
writes into projects) comes from `assistant_kernel/i18n/en.yaml`, never from literals in code. Models reply in the
user's language — the language the user writes in (`language_note`). Headings Kernel reads from documents have one key
each and are written in English; the parser also accepts the Chinese headings of older documents.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

import yaml

PACK = Path(__file__).parents[1] / "i18n" / "en.yaml"

# Headings of older (Chinese) documents, still found by the parser.
LEGACY_HEADINGS = {
    "purpose": ("项目用途",), "goals": ("业务目标与成功标准",), "users": ("使用者与使用场景",),
    "background": ("组织与行业背景",), "scope": ("范围",), "constraints": ("约束",), "routing": ("路由信息",),
    "archive": ("背景信息存档",), "creation": ("创建参数",), "profile": ("用户画像观察",),
    "revision_log": ("修订记录", "变更记录"), "pm_notes": ("本项目对 PM 的特别要求",),
    "supervisor_notes": ("本项目对 Supervisor 的特别要求",), "iteration_record": ("迭代记录",),
    "effect_review": ("上期效果复核",),
}
# What a model writes into a section that has nothing new, in whatever language it writes the document.
NONE_MARKERS = frozenset({"无", "无。", "（无）", "(无)", "暂无", "none", "none.", "(none)", "n/a", "-", "なし", "なし。",
                          "keine", "keine.", "ninguno", "ninguna", "ninguno.", "ninguna."})
# The confirm word of commands like `/cancel confirm` ("确认" still works for users used to it).
CONFIRM_WORDS = frozenset({"confirm", "确认"})


@functools.lru_cache(maxsize=None)
def pack() -> dict[str, str]:
    data = yaml.safe_load(PACK.read_text("utf-8")) or {}
    return {str(k): str(v) for k, v in data.items()}


def t(key: str, /, **values: Any) -> str:
    """Interface text by key; `{name}` placeholders filled from values (the key itself when unknown)."""
    text = pack().get(key, key)
    return text.format(**values) if values else text


STAGES = ("requirements", "design", "build", "review", "release", "operate")


def stage_name(stage: str) -> str:
    return t(f"stage.{stage}") if stage in STAGES else stage


def heading(key: str) -> str:
    return t(f"heading.{key}")


@functools.lru_cache(maxsize=None)
def heading_keys() -> dict[str, tuple[str, ...]]:
    """Every heading key with all its spellings: the English heading plus the Chinese one of older documents."""
    keys = {name[len("heading."):]: [text] for name, text in pack().items() if name.startswith("heading.")}
    for key, legacy in LEGACY_HEADINGS.items():
        keys.setdefault(key, []).extend(legacy)
    return {key: tuple(dict.fromkeys(texts)) for key, texts in keys.items()}


# A fact archived for every project starts with one of these ("Shared: …", or the user's language's word).
SHARED_MARKERS = ("shared", "公共", "共有", "gemeinsam", "compartido")
# Words in an answer that say the user does not know (for the weekly review's statistics).
UNSURE_WORDS = ("not sure", "don't know", "no idea", "whatever", "不清楚", "不知道", "不确定", "随便", "都行",
                "わからない", "分からない", "どちらでも", "weiß nicht", "keine ahnung", "egal", "no sé", "no estoy seguro",
                "me da igual")


def shared_fact(line: str) -> str:
    """The fact of a line marked as shared ("Shared: x" / "公共：x" → "x"), or "" for any other line."""
    for marker in SHARED_MARKERS:
        if line.casefold().startswith(marker):
            return line[len(marker):].lstrip("：: ").strip()
    return ""


def sounds_unsure(text: str) -> bool:
    folded = text.casefold()
    return any(word in folded for word in UNSURE_WORDS)


def is_none(text: str) -> bool:
    return not text.strip() or text.strip().casefold() in NONE_MARKERS


LANGUAGE_NOTE = (
    "## Language\n\nReply in the user's language: the language of the user's latest message. In a turn without a new "
    "message from the user, use the language the user has mostly been writing in (see the recent conversation and the "
    "task ledger); a language preference the user stated explicitly wins; when you cannot tell, use English. Write "
    "everything the user reads — summaries, questions, options, card text, the text of documents — in that language, "
    "even though the instructions above are in English. Document headings stay in English exactly as the instructions "
    "give them; keep JSON keys, enum values, tool names, file paths and code as specified.")


_PLACEHOLDER = re.compile(r"\{\{\s*heading:([A-Za-z0-9_.]+)\s*\}\}")


def fill(text: str) -> str:
    """Fill a contract's {{heading:key}} placeholders with the English headings Kernel parses."""
    return _PLACEHOLDER.sub(lambda match: heading(match.group(1)), text)
