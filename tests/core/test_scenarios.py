"""Acceptance scenarios S1–S22 from docs/REBUILD_PLAN.md section 9.

Each test is a skeleton: the docstring is the specification (steps → assertions).
Remove the skip marker when the phase noted in the reason implements it.
Every scenario ends with assert_invariants(): one active task per project,
one cursor per active task, one open card per project.
"""
import pytest

phase1 = pytest.mark.skip(reason="rebuild phase 1: state machine (injected run results)")
phase2 = pytest.mark.skip(reason="rebuild phase 2: executors + dispatcher")
phase3 = pytest.mark.skip(reason="rebuild phase 3: ingress + Discord cards")
phase6 = pytest.mark.skip(reason="rebuild phase 6: weekly evolution")


@phase1
def test_s01_mainline_to_completion():
    """New request → PM asks → user answers → requirements doc → approve+design → design →
    approve+build → validation pass → review → release approval → publish → completed →
    next queued task activates. Exactly one cursor at every step; two approvals + one release approval."""


@phase1
def test_s02_pm_channel_input_while_pm_running():
    """User text in the PM channel while a pm_turn runs → attached to the task; the next pm_turn
    receives it and messages.consumed_by_run_id is set."""


@phase1
def test_s03_directional_input_upgrades_card():
    """Card open; directional text in the PM channel → supervisor(review_input) upgrades the card to a
    new revision keeping drafts; clicking the old revision only replies 'updated', creates nothing."""


@phase1
def test_s04_plain_supplement_keeps_card():
    """Card open; non-directional text in the PM channel → card unchanged, supplement attached and
    delivered to the PM together with the answers on submit."""


@phase1
def test_s05_supervisor_consult_is_read_only():
    """Active task; text in the Supervisor channel → consult reply; task state and revision unchanged."""


@phase1
def test_s06_new_work_from_supervisor_channel():
    """Supervisor channel proposes new work → 'create task?' decision item on the card →
    approve → new task queued behind the active one."""


@phase1
def test_s07_urgent_task_suspends_and_resumes():
    """Urgent task approved → active task suspended at a safe point → urgent task runs and completes →
    original task resumes through a supervisor(resume) evaluation."""


@phase1
def test_s08_cancel_while_pm_running():
    """Cancel during a running pm_turn → its result is discarded, recorded effects are kept and
    reported, the card items of the task are removed, the next queued task activates."""


@phase1
def test_s09_supervisor_exhausted_falls_back():
    """Supervisor evaluation fails until retries are exhausted → Kernel fallback item (raw PM
    questions or retry/continue/stop) appears; mainline is not stuck."""


@phase1
def test_s10_pm_exhausted_asks_user():
    """pm_turn fails until retries are exhausted → decision item retry / revise / stop."""


@phase1
def test_s11_validation_failure_returns_to_build():
    """Validation fails → supervising → user accepts the fix proposal → pm_turn in build."""


@phase1
def test_s12_review_requests_design_change():
    """Review returns to design → new design revision → design approval is derived invalid;
    build is blocked until re-approved."""


@phase1
def test_s13_requirements_change_invalidates_design_approval():
    """Requirements revised after design approval → design approval derived invalid."""


@phase1
def test_s14_turn_budget_exceeded():
    """turn_count exceeds max_turns at settlement → continue/stop decision item."""


@phase1
def test_s15_duplicate_channel_event_is_idempotent():
    """The same gateway event delivered twice → processed once."""


@phase1
def test_s16_two_projects_run_in_parallel():
    """Two projects with active tasks → their pm_turns are claimed concurrently; each project has
    its own card; neither blocks the other."""


@phase2
def test_s17_publish_crash_goes_to_reconciliation():
    """Publish run lease expires mid-way → reconciling + reconcile decision item; never replayed."""


@phase1
def test_s18_runtime_question_shares_the_card():
    """Card open for the task; a runtime program asks → new item on the same card; answering only that
    item routes the answer to the program and leaves the task item open."""


@phase2
def test_s19_dispatcher_restart_mid_run():
    """Dispatcher dies during a pm_turn → lease expiry requeues the pm_turn; a publish goes to
    reconciliation instead."""


@phase3
def test_s20_credential_never_reaches_model_or_logs():
    """Credential item → modal → stored in the credential store; value absent from model context,
    logs and messages; PM resumes with 'provided'."""


@phase6
def test_s21_weekly_evolution_queues_once():
    """Weekly evolution tick while the project is busy → evolution task queued; a second tick in the
    same week creates nothing."""


@phase2
def test_s22_config_change_between_turns():
    """Published agent package changes between turns → next turn loads the new config; nothing is
    invalidated or rejected."""
