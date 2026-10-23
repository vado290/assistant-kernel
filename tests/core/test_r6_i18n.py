"""Round 6 / R6.2: English interface text, headings (English, older Chinese still found), replies in the user's language."""
from __future__ import annotations

from assistant_kernel.core import i18n, skills
from assistant_kernel.core.executors.common import prompt_text


def test_interface_text_comes_from_the_english_pack():
    assert i18n.t("heading.archive") == "Background archive"
    assert i18n.t("cmd.cancelled", goal="weekly report") == "Task cancelled: weekly report"
    assert i18n.t("no.such.key") == "no.such.key"
    assert i18n.t("cmd.secrets_confirm", key="binance_api", word="confirm").endswith("/secrets delete binance_api confirm")


def test_headings_are_english_and_older_chinese_headings_are_still_found():
    chinese = "# 章程\n\n## 1 项目用途\n做日报\n\n## 8 背景信息存档\n- 事实\n\n## 变更记录\n旧\n"
    english = "# Charter\n\n## 1 Purpose\nDaily report\n\n## Background archive (from the task)\n- fact\n"
    assert skills.section_text(chinese, i18n.heading("purpose")) == "做日报"
    assert skills.section_text(english, "项目用途") == "Daily report"
    assert skills.section_text(english, "archive") == "- fact"            # by key
    assert skills.section_text(chinese, "revision_log") == "旧"             # legacy heading
    assert skills.section_text(english, "background ARCHIVE") == "- fact"  # case-insensitive
    assert skills.section_text("## Notes\nx\n", "Notes") == "x"            # ordinary headings unchanged
    replaced = skills.replace_section(english, "背景信息存档", "- new")   # the existing heading is kept, not duplicated
    assert replaced.count("Background archive") == 1 and "背景信息存档" not in replaced and "- new" in replaced


def test_none_markers_and_contract_headings():
    assert all(i18n.is_none(v) for v in ("", " 无 ", "None", "N/A", "なし", "Keine.", "ninguna", "暂无"))
    assert not i18n.is_none("- a real fact")
    assert i18n.fill("Write {{heading:archive}}.") == "Write Background archive."
    output = prompt_text("pm-output")
    assert '"Background archive"' in output and '"User profile observations"' in output and "{{" not in output


def test_replies_follow_the_user_and_markers_accept_older_words():
    note = i18n.LANGUAGE_NOTE
    assert "language of the user's latest message" in note and "use English" in note and "stay in English" in note
    assert i18n.shared_fact("Shared: the company is in Shanghai") == "the company is in Shanghai"
    assert i18n.shared_fact("公共：公司在上海") == "公司在上海" and i18n.shared_fact("other fact") == ""
    assert i18n.sounds_unsure("not sure yet") and i18n.sounds_unsure("我也不清楚")
    assert i18n.CONFIRM_WORDS == {"confirm", "确认"}
