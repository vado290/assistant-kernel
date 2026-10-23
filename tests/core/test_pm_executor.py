"""Phase 2b: the PM executor turns a scripted model reply into a state-machine result."""
from __future__ import annotations

import pytest

from assistant_kernel.core import drafts, rules
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.common import read_document
from assistant_kernel.core.executors.pm import execute_pm

from .sim import next_step
from .test_dispatch import Scripts, settle


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    scripts = Scripts()
    executors = {**scripts.executors(), "pm_turn": execute_pm}
    d = Dispatcher(kernel, executors, services={"settings": kernel_settings, "harness": fake_harness})
    d.scripts = scripts
    return d


async def start_requirements_turn(kernel, sim, d, text="做一个日报工具"):
    task_id = (await kernel.receive("p1", role="pm", text=text))["task_id"]
    d.scripts.add("supervisor:evaluate", next_step("requirements", "梳理日报需求"))
    await settle(d)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    return task_id


async def test_pm_question_turn(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", {"outcome": "needs_input", "summary": "需要确认读者",
                               "questions": [{"id": "q1", "text": "日报给谁看？", "options": ["个人", "团队"]}],
                               "document": None})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "relay_pm_questions", "message": "请确认",
                                                    "questions": [{"id": "c1", "text": "日报给谁看？", "covers": ["q1"]}]})
    await settle(dispatcher)
    prompt = fake_harness.calls[-1].prompt
    assert "梳理日报需求" in prompt and "做一个日报工具" in prompt and "Current stage: requirements" in prompt
    assert fake_harness.calls[-1].physical_session_id is None       # first turn of a stage is fresh
    assert fake_harness.calls[-1].sandbox == "read-only"            # requirements never writes the vault
    item = await sim.item("p1", task_id)
    assert item["deliver_to"] == "pm" and item["content"]["pm_questions"][0]["id"] == "q1"

    # the answer goes straight back to the PM, in the same native session
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"c1": "团队"}})
    fake_harness.script("pm", {"outcome": "completed", "summary": "需求已整理",
                               "questions": [], "document": {"kind": "task_contract", "markdown": "# 日报需求\n给团队看"}})
    dispatcher.scripts.add("supervisor:evaluate", next_step("design", "开始设计"))
    await settle(dispatcher)
    last = fake_harness.calls[-1]
    assert "日报给谁看？: 团队" in last.prompt
    first_session = next(s for s, key in fake_harness.session_keys.items() if key == "pm")
    assert last.physical_session_id == first_session          # same stage → same native session
    task = await sim.task(task_id)
    contract = task["documents"]["task_contract"]
    assert read_document(dispatcher.services["settings"], "p1", contract) == "# 日报需求\n给团队看"
    assert (await sim.item("p1", task_id))["content"]["approve"] == ["requirements"]


def editing(settings, reply, *, content=None, edits=None, kind="task_contract"):
    """A scripted PM turn that changes its stage draft first, the way edit_task_document does."""
    def turn(request):
        task_id = request.scope_context["KERNEL_SCOPE_TASK_ID"]
        draft = drafts.load(settings, "p1", task_id, kind)
        markdown = content if content is not None else drafts.apply_edits(draft["markdown"], edits)
        drafts.save(settings, "p1", task_id, kind, markdown, draft["base_sha256"])
        return reply
    return turn


async def ask_then_answer(kernel, sim, dispatcher, fake_harness, reply, between=None):
    """A fresh question turn, then the answer turn in the same native session; returns both prompts."""
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", {"outcome": "needs_input", "summary": "需要确认读者",
                               "questions": [{"id": "q1", "text": "日报给谁看？"}], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "relay_pm_questions", "message": "请确认",
                                                    "questions": [{"id": "c1", "text": "日报给谁看？", "covers": ["q1"]}]})
    await settle(dispatcher)
    first = fake_harness.calls[-1]
    if between:
        between()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "submit", {"answers": {"c1": "团队"}})
    fake_harness.script("pm", reply)
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements", "再补充一点"))
    await settle(dispatcher)
    second = fake_harness.calls[-1]
    assert first.physical_session_id is None and second.physical_session_id is not None
    return task_id, first.prompt, second.prompt


async def test_resumed_turn_sends_only_what_changed(kernel, sim, dispatcher, fake_harness, kernel_settings):
    reply = editing(kernel_settings, {"outcome": "completed", "summary": "需求已整理", "questions": [],
                                      "document": {"kind": "task_contract"}}, content="# 日报需求\n给团队看")
    task_id, fresh, resumed = await ask_then_answer(kernel, sim, dispatcher, fake_harness, reply)
    assert "这是日报项目的 PM。" in fresh and "## Documents in effect" in fresh and "## Output (required)" in fresh
    assert "A resumed turn of the same session" in resumed and "日报给谁看？: 团队" in resumed
    for unchanged in ("这是日报项目的 PM。", "Artifact conventions", "## Output (required)", "## Documents in effect", "## Updated documents"):
        assert unchanged not in resumed, unchanged
    task = await sim.task(task_id)
    assert task["pm_session"]["seen"]["task_contract"] == task["documents"]["task_contract"]["sha256"]

    # The PM wrote the contract itself, so it is not sent back; a Kernel-written validation result is.
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET documents = documents || '{"validation": {"passed": false,
                           "bundle_sha": "b1", "report": "2 个测试失败"}}'::jsonb WHERE task_id=%s""", (task_id,))
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "completed", "summary": "已补充", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("design"))
    await settle(dispatcher)
    third = fake_harness.calls[-1].prompt
    assert "## Updated documents" in third and "2 个测试失败" in third and "给团队看" not in third


async def test_config_change_resends_role_and_skills(kernel, sim, dispatcher, fake_harness, project_files):
    role = project_files / "agents" / "pm" / "role.md"
    reply = {"outcome": "completed", "summary": "完成", "questions": [], "document": None}
    _, fresh, resumed = await ask_then_answer(kernel, sim, dispatcher, fake_harness, reply,
                                              between=lambda: role.write_text("这是日报项目的 PM，注意保密。", "utf-8"))
    assert "这是日报项目的 PM。" in fresh
    assert "A resumed turn of the same session" in resumed and "这是日报项目的 PM，注意保密。" in resumed and "Artifact conventions" in resumed
    assert "## Output (required)" not in resumed and "## Documents in effect" not in resumed


async def test_draft_snapshot_creates_a_revision_and_invalidates_the_approval(kernel, sim, dispatcher,
                                                                             fake_harness, kernel_settings):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", editing(kernel_settings, {"outcome": "completed", "summary": "需求已整理",
                                                        "questions": [], "document": {"kind": "task_contract"}},
                                      content="# 日报需求\n给团队看"))
    dispatcher.scripts.add("supervisor:evaluate", next_step("design", "开始设计"))
    await settle(dispatcher)
    first = (await sim.task(task_id))["documents"]["task_contract"]
    assert read_document(kernel_settings, "p1", first) == "# 日报需求\n给团队看"

    # approve requirements → design; the design turn sends the task back to requirements
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    assert rules.approval_valid(await sim.task(task_id), "requirements")
    fake_harness.script("pm", {"outcome": "return_to_requirements", "summary": "读者要改", "questions": [],
                               "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements", "读者加上老板"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")

    # a new session edits the draft (started from the approved revision) and names no document: still saved
    fake_harness.script("pm", editing(kernel_settings, {"outcome": "completed", "summary": "已改", "questions": [],
                                                        "document": None},
                                      edits=[{"old": "给团队看", "new": "给团队和老板看"}]))
    dispatcher.scripts.add("supervisor:evaluate", next_step("design"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    second = task["documents"]["task_contract"]
    assert second["sha256"] != first["sha256"]
    assert read_document(kernel_settings, "p1", second) == "# 日报需求\n给团队和老板看"
    assert read_document(kernel_settings, "p1", first) == "# 日报需求\n给团队看"   # revisions are immutable
    assert not rules.approval_valid(task, "requirements")
    assert (await sim.item("p1", task_id))["content"]["approve"] == ["requirements"]


async def test_named_document_with_an_empty_draft_is_invalid(kernel, sim, dispatcher, fake_harness):
    await start_requirements_turn(kernel, sim, dispatcher)
    empty = {"outcome": "completed", "summary": "x", "questions": [], "document": {"kind": "task_contract"}}
    fake_harness.script("pm", empty, empty, empty)
    await settle(dispatcher)
    assert (await sim.item("p1"))["content"]["decision"] == "run_failed"


def test_draft_edits_must_match_exactly_once():
    assert drafts.apply_edits("a b c", [{"old": "b", "new": "B"}, {"old": "B c", "new": "x"}]) == "a x"
    with pytest.raises(ValueError, match="not found"):
        drafts.apply_edits("a b c", [{"old": "d", "new": "D"}])
    with pytest.raises(ValueError, match="occurs 2 times"):
        drafts.apply_edits("a b a", [{"old": "a", "new": "A"}])
    with pytest.raises(ValueError, match="not found"):
        drafts.apply_edits("a", [{"old": "", "new": "A"}])


async def test_non_json_reply_is_fixed_in_the_same_session(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", "我已经分析完了（忘了输出 JSON）",
                        {"outcome": "completed", "summary": "完成", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    first, fix = fake_harness.calls[-2:]
    assert first.physical_session_id is None and fix.physical_session_id is not None   # the fix resumes the turn
    assert "was not a valid JSON object" in fix.prompt
    assert (await sim.task(task_id))["state"] == "awaiting_user"


async def test_unparseable_reply_is_retried(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    good = {"outcome": "completed", "summary": "完成", "questions": [], "document": None}
    fake_harness.script("pm", "坏输出", "还是坏输出", good)
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    async with kernel.db.tx() as c:
        run = await (await c.execute("SELECT attempt, status FROM core.runs WHERE run_type='pm_turn'")).fetchone()
    assert (run["attempt"], run["status"]) == (2, "succeeded")
    assert (await sim.task(task_id))["state"] == "awaiting_user"


async def test_document_of_another_stage_is_rejected(kernel, sim, dispatcher, fake_harness):
    await start_requirements_turn(kernel, sim, dispatcher)
    wrong = {"outcome": "completed", "summary": "x", "questions": [],
             "document": {"kind": "solution_design", "markdown": "设计"}}
    fake_harness.script("pm", wrong, wrong, wrong)
    await settle(dispatcher)
    item = await sim.item("p1")
    assert item["content"]["decision"] == "run_failed"   # three invalid results → the user decides


async def test_build_without_candidate_files_gets_a_bound_marker(kernel, sim, dispatcher, fake_harness, kernel_settings):
    from assistant_kernel.core.executors.pm import seal_candidates
    assert seal_candidates(kernel_settings, "p1", "00000000-0000-0000-0000-000000000000", "abc") == "no-files:abc"


async def test_review_baseline_is_kernel_owned_hashed_and_renewed_with_same_prose(
        kernel, sim, dispatcher, fake_harness, kernel_settings):
    from psycopg.types.json import Jsonb
    from assistant_kernel.services.document_runtime import TaskDocumentService
    from .test_scenarios import review_ready, execute_next

    task_id = await review_ready(kernel, sim)
    expected = rules.review_baseline(await sim.task(task_id))
    reply = {"outcome": "completed", "summary": "核对完成", "document": {
        "kind": "review_report", "markdown": "# Review\n验收通过", "verdict": "pass",
        "review_baseline": {"untrusted": "model cannot set the baseline"}}}
    fake_harness.script("pm", reply)
    dispatcher.scripts.add("supervisor:evaluate", next_step("release"))
    await settle(dispatcher)
    first = (await sim.task(task_id))["documents"]["review_report"]
    service = TaskDocumentService(kernel_settings)
    stored = service.read("p1", first["ref"])["payload"]
    assert stored["review_baseline"] == first["review_baseline"] == expected
    assert "review baseline" in fake_harness.calls[-1].prompt and "candidate-snapshots/b1/bundle.json" in fake_harness.calls[-1].prompt
    first_seen = (await sim.task(task_id))["pm_session"]["seen"]["review_context"]

    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "reject")
    task = await sim.task(task_id)
    task["documents"]["implementation_report"]["sha256"] = "i2"
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb(task["documents"]), task_id))
    await execute_next(kernel, sim, task_id, "review", "重新核对")
    fake_harness.script("pm", reply)
    dispatcher.scripts.add("supervisor:evaluate", next_step("release"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    second = task["documents"]["review_report"]
    assert second["sha256"] != first["sha256"]  # same prose, different reviewed version → different approval target
    assert service.read("p1", first["ref"])["payload"] == stored
    assert second["review_baseline"]["implementation_report"] == "i2"
    assert task["pm_session"]["seen"]["review_context"] != first_seen
    assert "i2" in fake_harness.calls[-1].prompt and "the review basis or the delivery changed" in fake_harness.calls[-1].prompt


def test_review_context_is_scoped_and_distinguishes_delivery_types(monkeypatch):
    from assistant_kernel.core.executors import common
    from .test_scenarios import reviewed_task
    monkeypatch.setattr(common, "read_document", lambda *args: "body")
    task = reviewed_task()
    task["project_id"] = "p1"
    for bundle, expected in (("b1", "candidate-snapshots/b1/bundle.json"), ("git:abc123", "Review the Git commit abc123"),
                             ("no-files:i1", "Delivery without files")):
        task["documents"]["implementation_report"]["bundle_sha"] = bundle
        task["documents"]["validation"]["bundle_sha"] = bundle
        sections = dict((key, text) for key, _, text in common.document_sections(None, task))
        assert expected in sections["review_context"]
    for kind, track, stage in (("delivery", "quick", "build"), ("delivery", "full", "design"),
                               ("bootstrap", "full", "requirements"), ("evolution", "full", "operate")):
        task.update(kind=kind, track=track, stage=stage)
        assert "review_context" not in [key for key, _, _ in common.document_sections(None, task)]


async def test_review_cannot_stamp_a_version_changed_during_the_model_turn(
        kernel, sim, dispatcher, fake_harness, monkeypatch):
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.executors import pm as pm_executor
    from .test_scenarios import review_ready
    task_id = await review_ready(kernel, sim)
    original = pm_executor.call_for_json

    async def changed_during_call(*args, **kwargs):
        result = await original(*args, **kwargs)
        task = await sim.task(task_id)
        task["documents"]["implementation_report"]["sha256"] = "i2"
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb(task["documents"]), task_id))
        return result

    monkeypatch.setattr(pm_executor, "call_for_json", changed_during_call)
    fake_harness.script("pm", {"outcome": "completed", "document": {
        "kind": "review_report", "markdown": "核对完成", "verdict": "pass"}})
    dispatcher.scripts.add("supervisor:evaluate", next_step("review"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    assert task["documents"]["review_report"]["review_baseline"]["implementation_report"] == "i1"
    assert rules.review_baseline(task)["implementation_report"] == "i2"
    assert not rules.review_passed(task)


def test_git_review_workspace_must_match_without_changing_it(tmp_path):
    import subprocess
    from assistant_kernel.core.executors.pm import review_worktree_error

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    (tmp_path / "a.txt").write_text("first", "utf-8")
    git("add", "a.txt")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "first")
    sha = git("rev-parse", "HEAD")
    basis = {"bundle_sha": "git:" + sha}
    assert review_worktree_error(tmp_path, basis) is None
    (tmp_path / "a.txt").write_text("local work", "utf-8")
    assert review_worktree_error(tmp_path, basis)
    assert (tmp_path / "a.txt").read_text("utf-8") == "local work"
    assert git("rev-parse", "HEAD") == sha
    assert review_worktree_error(tmp_path / "absent", basis)


async def test_missing_candidate_fails_build_before_validation_and_can_retry(
        kernel, sim, dispatcher, fake_harness, kernel_settings, project_files):
    from assistant_kernel.core.executors.delivery import execute_validation
    from .test_delivery_executors import write_candidate

    dispatcher.executors["validation"] = execute_validation
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    for kind, target in (("task_contract", "design"), ("solution_design", "build")):
        fake_harness.script("pm", editing(
            kernel_settings,
            {"outcome": "completed", "summary": kind, "questions": [], "document": {"kind": kind}},
            content=f"# {kind}\n交付 docs/hello.md", kind=kind))
        dispatcher.scripts.add("supervisor:evaluate", next_step(target))
        await settle(dispatcher)
        await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")

    write_candidate(kernel_settings, str(task_id))
    source = project_files / "runs" / str(task_id) / "candidate" / "project" / "docs" / "hello.md"
    source.unlink()
    reply = {"outcome": "ready_for_validation", "summary": "提交",
             "questions": [], "document": {"kind": "implementation_report", "markdown": "# 实施报告\n已写 hello.md"}}
    fake_harness.script("pm", reply, reply, reply)
    await settle(dispatcher)

    task = await sim.task(task_id)
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "run_failed"
    assert "workspace://candidate/project/docs/hello.md" in item["content"]["message"]
    assert "implementation_report" not in task["documents"]
    assert "validation" not in task["documents"]
    async with kernel.db.tx() as c:
        row = await (await c.execute(
            "SELECT count(*) AS n FROM core.runs WHERE task_id=%s AND run_type='validation'",
            (task_id,))).fetchone()
    assert row["n"] == 0
    await sim.assert_consistent()

    # Restore the registered file, then use the existing failure card and real validation executor.
    expected = write_candidate(kernel_settings, str(task_id))
    await kernel.act("p1", item["item_id"], "retry")
    fake_harness.script("pm", reply)
    dispatcher.scripts.add("supervisor:evaluate", next_step("review"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    assert task["documents"]["implementation_report"]["bundle_sha"] == expected
    assert task["documents"]["validation"]["passed"]
    assert task["documents"]["validation"]["bundle_sha"] == expected
    assert (await sim.item("p1", task_id))["content"]["target_stage"] == "review"
    await sim.assert_consistent()


@pytest.mark.parametrize("stage,kind,sandbox", [
    ("requirements", "work", "read-only"), ("design", "work", "read-only"), ("review", "work", "read-only"),
    ("operate", "work", "read-only"), ("operate", "evolution", "read-only"), ("build", "work", "danger-full-access"),
])
async def test_only_build_turns_may_write_outside_the_kernel(monkeypatch, kernel_settings, project_files,
                                                             stage, kind, sandbox):
    """Outside build the PM runs in Codex's read-only sandbox; project files change only via candidates."""
    from types import SimpleNamespace

    from assistant_kernel.core.executors import pm
    from assistant_kernel.core.executors.common import CANDIDATE_TOOLS

    seen = {}

    async def fake_call(ctx, run, config, prompt, **kwargs):
        seen.update(kwargs)
        return None, SimpleNamespace(final_text="")

    class StubKernel:
        async def get_task(self, task_id):
            return {"task_id": task_id, "project_id": "p1", "kind": kind, "goal": "日报", "documents": {},
                    "pm_session": {}, "stage": stage, "approvals": {}}

        async def task_ledger(self, task_id):
            return {"turns": [], "actions": []}

        async def turn_inputs(self, run):
            return []

        async def project_digest(self, project_id):
            return {}

    monkeypatch.setattr(pm, "call_for_json", fake_call)
    async def no_material(*args):
        return ""
    monkeypatch.setattr(pm, "evolution_material", no_material)
    ctx = SimpleNamespace(kernel=StubKernel(), services={"settings": kernel_settings})
    run = {"run_id": "r1", "task_id": "t1", "project_id": "p1", "input": {"stage": stage}}
    assert (await pm.execute_pm(run, ctx))["outcome"] == "invalid_output"
    assert seen["sandbox"] == sandbox
    assert seen["candidates"] is (stage == "build")
    assert bool(seen["tools"] & CANDIDATE_TOOLS) is (stage == "build")
