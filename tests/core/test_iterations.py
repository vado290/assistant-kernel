"""ROUND4 R4.1: the change stream — parsing, fallback records, queries, shared-context rows, effect reviews."""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from assistant_kernel.core import iterlog, machine


async def test_iteration_pages_cover_old_backlog_ties_and_exclude_new_records(kernel):
    ids = []
    old = datetime.now(timezone.utc) - timedelta(days=90)
    async with kernel.db.tx() as c:
        for index in range(73):
            row = await machine.insert_iteration(c, "p1", None, kind="task", title=f"effect {index}", reason="r",
                                                 expected_effect="better", measure="report")
            ids.append(row["iteration_id"])
        await c.execute("UPDATE core.iterations SET created_at=%s,effect_status='pending'", (old,))
        missing = await machine.insert_iteration(c, "p1", None, kind="task", title="missing", reason="r", expected_effect="x")
        await machine.insert_iteration(c, "p1", None, kind="task", title="ordinary", reason="r")
        await machine.insert_iteration(c, "p2", None, kind="task", title="foreign", reason="r", expected_effect="x", measure="y")
    page = await kernel.iteration_page("p1", pending_only=True, oldest_first=True, limit=17)
    got = [r["iteration_id"] for r in page["items"]]
    cursor = page["next_cursor"]
    async with kernel.db.tx() as c:
        fresh = await machine.insert_iteration(c, "p1", None, kind="task", title="new", reason="r", expected_effect="x", measure="y")
    while page["next_cursor"]:
        page = await kernel.iteration_page("p1", pending_only=True, oldest_first=True, limit=17, cursor=page["next_cursor"])
        got += [r["iteration_id"] for r in page["items"]]
    assert got == sorted(ids) and not page["truncated"]
    assert fresh["iteration_id"] not in got
    diagnostic = await kernel.iteration_page("p1", needs_measure=True)
    assert [r["iteration_id"] for r in diagnostic["items"]] == [missing["iteration_id"]]
    for kwargs in ({"project_id": "p2", "pending_only": True, "oldest_first": True},
                   {"project_id": "p1", "pending_only": True}, {"project_id": "p1"}):
        with pytest.raises(ValueError, match="游标"):
            await kernel.iteration_page(**kwargs, cursor=cursor)
    for bad in ("not-a-cursor", "W10=", "e30="):
        with pytest.raises(ValueError, match="游标"):
            await kernel.iteration_page("p1", cursor=bad)
    assert fresh["iteration_id"] in [r["iteration_id"] for r in (await kernel.iteration_page("p1", pending_only=True))["items"]]


async def test_history_pages_preserve_display_order_and_project_scope(kernel):
    from psycopg.types.json import Jsonb
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    async with kernel.db.tx() as c:
        for project in ("p1", "p2"):
            for index in range(9):
                await c.execute("""INSERT INTO core.messages(project_id,channel,direction,role,body,created_at)
                    VALUES (%s,'api','inbound','pm',%s,%s)""", (project, Jsonb({"text": str(index)}), stamp))
    page = await kernel.history_page("p1", limit=3)
    first_cursor = page["next_cursor"]
    assert [r["message_id"] for r in page["items"]] == sorted(r["message_id"] for r in page["items"])
    seen = {r["message_id"] for r in page["items"]}
    async with kernel.db.tx() as c:
        await c.execute("""INSERT INTO core.messages(project_id,channel,direction,role,body)
            VALUES ('p1','api','inbound','pm',%s)""", (Jsonb({"text": "arrived later"}),))
    while page["next_cursor"]:
        page = await kernel.history_page("p1", limit=3, cursor=page["next_cursor"])
        ids = {r["message_id"] for r in page["items"]}
        assert not seen & ids
        assert all(r["text"] != "arrived later" for r in page["items"])
        seen |= ids
    assert len(seen) == 9
    with pytest.raises(ValueError, match="游标"):
        await kernel.history_page("p2", cursor=first_cursor)
    assert (await kernel.history_page("p1"))["items"][-1]["text"] == "arrived later"

GOOD = """# 实施报告

## 迭代记录
```yaml
title: 提问分组
reason: 用户说问题太散（复盘 P2）
significance: minor
changes:
  - {type: skill, target: assistant-requirements, action: modify, version: "3.2.0→3.3.0", note: 第 1 轮分组}
  - {type: map, target: knowledge/map.yaml, action: modify, note: 无}
verification: 模拟一轮
expected_effect: 需求阶段 2 轮以内
measure: 各阶段 PM 回合数
followups: 无
```
"""


def test_parse_record_and_derived_fields():
    record = iterlog.parse_record(GOOD)
    assert record["title"] == "提问分组" and record["changes"][0]["version"] == "3.2.0→3.3.0"
    assert iterlog.artifacts(record["changes"], ["vault://p1/jobs.yaml"]) == ["jobs", "map", "skill:assistant-requirements"]
    assert iterlog.auto_major(["git://main/src/assistant_kernel/core/sql/0008_x.sql"])
    assert not iterlog.auto_major(["vault://p1/docs/a.md"])
    for bad in (GOOD.replace("title: 提问分组\n", ""), GOOD.replace("type: skill", "type: nonsense"),
                "# 实施报告\n没有记录"):
        with pytest.raises(ValueError):
            iterlog.parse_record(bad)


@pytest.mark.parametrize("body", ["", "## 上期效果复核\n[bad", "## 上期效果复核\n{}",
                                    "## 上期效果复核\n- {iteration: '%%%%%%%%', status: effective, note: x}",
                                    "## 上期效果复核\n- {iteration: 12345678----, status: effective, note: x}"])
def test_bad_effect_reviews_are_not_an_empty_success(body):
    with pytest.raises(ValueError):
        iterlog.parse_effect_reviews(body)
    assert iterlog.parse_effect_reviews("## 上期效果复核\n[]") == []


@pytest.mark.parametrize("invalid", ["wildcard", "ambiguous", "duplicate", "foreign"])
async def test_cancel_preserves_effects_on_invalid_reference_list(kernel, invalid):
    from psycopg.types.json import Jsonb
    async with kernel.db.tx() as c:
        ids = ["12345678-1111-1111-1111-111111111111", "12345678-2222-2222-2222-222222222222"]
        for index, ident in enumerate(ids):
            await c.execute("""INSERT INTO core.iterations(iteration_id,project_id,kind,title,source,reason)
                               VALUES (%s,%s,'task','original','user','r')""", (ident, "p2" if invalid == "foreign" else "p1"))
    evolution = await kernel.start_evolution("p1")
    refs = {"wildcard": ["%%%%%%%%"], "ambiguous": ["12345678"], "duplicate": [ids[0], ids[0][:10]], "foreign": [ids[0]]}[invalid]
    reviews = [{"iteration": ref, "status": "effective", "note": "evidence"} for ref in refs]
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s",
                        (Jsonb({"improvement_proposal": {"sha256": "proposal", "effect_reviews": reviews}}), evolution["task_id"]))
    await kernel.cancel_task(evolution["task_id"])
    async with kernel.db.tx() as c:
        rows = list(await (await c.execute("SELECT effect_status FROM core.iterations WHERE iteration_id=ANY(%s::uuid[])", (ids,))).fetchall())
    assert all(r["effect_status"] is None for r in rows)
    assert (await kernel.get_task(evolution["task_id"]))["state"] == "cancelled"


async def test_pending_effects_remain_visible_beyond_default_window(kernel):
    async with kernel.db.tx() as c:
        row = await machine.insert_iteration(c, "p1", None, kind="task", title="长期效果", reason="r",
                                             expected_effect="改善业务结果", measure="业务报表")
        await c.execute("UPDATE core.iterations SET created_at=now()-interval '60 days',effect_status='pending' WHERE iteration_id=%s",
                        (row["iteration_id"],))
    assert not await kernel.iterations("p1")
    assert [r["iteration_id"] for r in await kernel.iterations("p1", pending_only=True)] == [row["iteration_id"]]


async def test_cancel_still_writes_legal_effects_in_a_mixed_legacy_report(kernel):
    from psycopg.types.json import Jsonb
    async with kernel.db.tx() as c:
        prior = await machine.insert_iteration(c, "p1", None, kind="task", title="valid prior", reason="r")
    task = await kernel.start_evolution("p1")
    body = ("## 上期效果复核\n```yaml\n"
            f"- {{iteration: '{str(prior['iteration_id'])[:8]}', status: effective, note: 已核对业务报表}}\n"
            "- {iteration: '%%%%%%%%', status: effective, note: invalid}\n```\n")
    with pytest.raises(ValueError): iterlog.parse_effect_reviews(body)
    valid = iterlog.parse_effect_reviews(body, strict=False)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s",
                        (Jsonb({"improvement_proposal": {"sha256": "x", "effect_reviews": valid, "effect_review_error": "invalid item"}}), task["task_id"]))
    await kernel.cancel_task(task["task_id"])
    row = next(r for r in await kernel.iterations("p1") if r["iteration_id"] == prior["iteration_id"])
    assert row["effect_status"] == "effective" and row["effect_note"] == "已核对业务报表"


async def test_queries_filter_by_artifact_words_and_significance(kernel):
    async with kernel.db.tx() as c:
        await machine.insert_iteration(c, "p1", None, kind="grant", title="授权 crypto 目录", reason="用户批准",
                                       significance="major", changes=[{"type": "file", "target": "crypto"}])
        await machine.insert_iteration(c, "p1", None, kind="shared_context", title="改技能", reason="提问太散",
                                       changes=[{"type": "skill", "target": "assistant-requirements", "note": "分组"}])
    assert [r["title"] for r in await kernel.iterations("p1", artifact="skill:assistant-requirements")] == ["改技能"]
    assert [r["title"] for r in await kernel.iterations("p1", artifact="skill:*")] == ["改技能"]
    assert [r["title"] for r in await kernel.iterations("p1", major_only=True)] == ["授权 crypto 目录"]
    assert [r["title"] for r in await kernel.iterations("p1", query="分组")] == ["改技能"]
    text = iterlog.render(await kernel.iterations("p1"))
    assert "【重大】授权 crypto 目录" in text and "改动：skill assistant-requirements" in text


async def test_a_cancelled_task_gets_a_plain_record_once(kernel, sim):
    task_id = (await kernel.receive("p1", role="pm", text="做个东西"))["task_id"]
    await kernel.cancel_task(task_id)
    await kernel.cancel_task(task_id)
    records = await kernel.iterations("p1")
    assert len(records) == 1 and records[0]["title"] == "做个东西" and records[0]["complete"]


async def test_effect_reviews_are_written_back(kernel, sim):
    async with kernel.db.tx() as c:
        earlier = await machine.insert_iteration(c, "p1", None, kind="shared_context", title="旧改动", reason="r")
    evolution = await kernel.start_evolution("p1")
    async with kernel.db.tx() as c:   # the review's proposal as the PM executor stores it
        await c.execute("""UPDATE core.tasks SET documents = jsonb_build_object('improvement_proposal',
                           jsonb_build_object('kind', 'improvement_proposal', 'sha256', 'x', 'effect_reviews',
                           jsonb_build_array(jsonb_build_object('iteration', %s::text, 'status', 'effective',
                                                                'note', '回合数降到 2')))) WHERE task_id=%s""",
                        (str(earlier["iteration_id"])[:8], evolution["task_id"]))
    await kernel.cancel_task(evolution["task_id"])
    row = next(r for r in await kernel.iterations("p1") if r["title"] == "旧改动")
    assert row["effect_status"] == "effective" and row["effect_note"] == "回合数降到 2" and row["effect_reviewed_in"]


def test_collaboration_gaps(tmp_path):
    from assistant_kernel.core import skills
    def skill(name, reads, writes):
        folder = tmp_path / "agents" / "pm" / ".agents" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\nmetadata:\n  version: \"1.0.0\"\n  load: {{roles: [pm]}}\n"
            f"  io:\n    reads: [{', '.join(reads)}]\n    writes: [{', '.join(writes)}]\n---\n# x\n", "utf-8")
    skill("a", ["digest"], ["task_contract"])
    skill("b", ["task_contract", "solution_design"], ["review_report"])
    assert skills.collaboration_gaps(tmp_path) == ["review_report：由 b 写出，但没有技能读取",
                                                   "solution_design：b 要读取，但没有技能写出"]


async def test_review_material_carries_the_stream(kernel, kernel_settings, project_files):
    from assistant_kernel.core.executors.digest import evolution_material
    async with kernel.db.tx() as c:
        await machine.insert_iteration(c, "p1", None, kind="shared_context", title="近期改动", reason="r",
                                       expected_effect="少一轮", measure="各阶段 PM 回合数")
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert "近 4 周的变更流水" in text and "近期改动" in text and "衡量：各阶段 PM 回合数" in text


def test_map_location_follows_delivery(kernel_settings, project_files):
    from assistant_kernel.core.executors.common import project_map
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "map.yaml").write_text(
        "schema_version: project-map.v1\nentries:\n  - {path: knowledge/, kind: directory, purpose: 日报脚本说明在这里}\n",
        "utf-8")
    where, text = project_map(kernel_settings, "p1")
    assert "knowledge/map.yaml" in where and "日报脚本" in text and "`knowledge/`" in text
    repo = project_files.parent / "repo-p1"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "map.yaml").write_text("schema_version: project-map.v1\nentries:\n  - {path: docs/, purpose: 代码地图}\n",
                                            "utf-8")
    (project_files / "project.yaml").write_text(
        f"schema_version: project.v2\nproject_id: p1\nlayout_version: 2\ndelivery: git-repo\nrepository: {repo}\n", "utf-8")
    where, text = project_map(kernel_settings, "p1")
    assert "docs/map.yaml" in where and "代码地图" in text
