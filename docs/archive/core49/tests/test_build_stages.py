from assistant_kernel.domain.project_build import after_stage, after_approval
import pytest


def test_fixed_stage_transitions():
    state={"rework_attempt":0,"max_rework_attempts":2}
    assert after_stage("requirements",state,{}).approval=="requirements"
    assert after_stage("implementation",state,{}).next_stage=="review"
    assert after_stage("review",state,{"verdict":"approve"}).approval=="acceptance"
    with pytest.raises(ValueError, match="unknown build stage"):
        after_stage("architecture",state,{})


def test_approval_and_rework_rules():
    assert after_approval("requirements",{}, {"action":"approve"}).next_stage=="implementation"
    assert after_approval("requirements",{}, {"action":"clarify"}).next_stage=="requirements"
    assert after_approval("acceptance",{}, {"action":"approve"}).terminal=="succeeded"
    assert after_approval("acceptance",{}, {"action":"reject"}).terminal=="cancelled"
    assert after_approval("acceptance",{}, {"action":"edit"}).state["rework_attempt"]==1


def test_review_retry_limit():
    state={"rework_attempt":0,"max_rework_attempts":2}
    for _ in range(2):
        result=after_stage("review",state,{"verdict":"request_changes"})
        assert result.next_stage=="implementation"
        state=result.state
    assert after_stage("review",state,{"verdict":"request_changes"}).approval=="acceptance"


@pytest.mark.parametrize("answer", [None,False,0,"",{"action":"bad"}])
def test_invalid_business_action_is_not_an_approval(answer):
    with pytest.raises(ValueError):
        after_approval("requirements",{},answer)


def test_complete_stage_prompt_refuses_silent_truncation():
    from assistant_kernel.services.project_build import CodexStageExecutor
    with pytest.raises(ValueError, match="context budget"):
        CodexStageExecutor._stage_prompt("requirements", {"request": {}}, "role", {"requirements": "x" * 524289})
