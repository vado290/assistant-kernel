import pytest

from assistant_kernel.domain import JOB_TRANSITIONS, InvalidTransition, require_transition


def test_valid_job_transition() -> None:
    require_transition(JOB_TRANSITIONS, "queued", "running")


def test_terminal_job_cannot_reopen() -> None:
    with pytest.raises(InvalidTransition):
        require_transition(JOB_TRANSITIONS, "succeeded", "queued")
