"""0.45.3 (system map gen-20261019-fef3d31 F1–F3): every agent can initialise its tools, skills and prompts name
only tools the agents that read them can use, and agent configs hold only what the loader reads."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

from assistant_kernel.core.executors.common import DOCUMENT_TOOLS, MVP_TOOLS, READ_ONLY_TOOLS, effective_tools
from assistant_kernel.services.validation_runner import ValidationRunner

SRC = Path(__file__).parents[2] / "src" / "assistant_kernel"
TEMPLATE = SRC / "core" / "project_template" / "agents"
# Who reads each core prompt: triage runs with the PM config (executors/triage.py), consult with the Supervisor's;
# routing is read by both, so it is checked against the narrower Supervisor set.
PROMPT_ROLES = {"pm-": "pm", "git-delivery": "pm", "runtime-manifest": "pm",
                "supervisor-": "supervisor", "triage": "pm", "routing": "supervisor"}


def template_tools(agent: str) -> list[str]:
    return yaml.safe_load((TEMPLATE / agent / "config.yaml").read_text("utf-8")).get("tools") or []


def usable() -> dict[str, set[str]]:
    """What each role's model turn gets with the template config (PM: its widest stage, build)."""
    # Shared artifact rules (assistant-task-continuity) tell the writer, the PM, to edit documents with the document
    # tools; Supervisor only reads documents, which its prompt already contains, so naming them is not a gap there.
    return {"pm": set(effective_tools(MVP_TOOLS, template_tools("pm"))),
            "supervisor": set(effective_tools(READ_ONLY_TOOLS, template_tools("supervisor"))) | DOCUMENT_TOOLS}


def named_tools(text: str) -> set[str]:
    return {t for t in MVP_TOOLS if re.search(rf"\b{t}\b", text)}


def test_an_empty_tool_list_still_gets_the_platform_tools_and_can_initialise():
    got = set(effective_tools(READ_ONLY_TOOLS, ()))
    assert {"get_authorized_workspace", "list_recent_history", "list_iterations", "map_lookup",
            "list_credentials", "get_credential"} <= got


def test_template_configs_use_current_tool_names_and_no_unread_keys():
    for agent in ("pm", "supervisor"):
        data = yaml.safe_load((TEMPLATE / agent / "config.yaml").read_text("utf-8"))
        assert set(template_tools(agent)) <= MVP_TOOLS, agent
        assert not {"aliases", "entrypoints", "triggers", "scope_profile", "mcp_servers", "skills"} & set(data), agent


def test_every_tool_a_skill_or_prompt_names_is_usable_by_its_readers():
    roles, problems = usable(), []
    for path in sorted((SRC / "prompts" / "skills").glob("*/SKILL.md")):
        meta = yaml.safe_load(path.read_text("utf-8").split("---")[1])["metadata"]
        for role in meta["load"].get("roles", []):
            missing = named_tools(path.read_text("utf-8")) - roles[role]
            problems += [f"{path.parent.name} ({role}): {t}" for t in sorted(missing)]
    for path in sorted((SRC / "prompts" / "core").glob("*.md")):
        role = next(r for prefix, r in PROMPT_ROLES.items() if path.stem.startswith(prefix))
        problems += [f"{path.name} ({role}): {t}" for t in sorted(named_tools(path.read_text("utf-8")) - roles[role])]
    # The on-demand skill list is shown only to a PM working in a code worktree (skills.Selection.render).
    problems += [f"skills.py (pm): {t}" for t in sorted(named_tools((SRC / "core" / "skills.py").read_text("utf-8"))
                                                         - roles["pm"])]
    assert problems == []


def check_config(settings, tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "agents" / "analyst" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    issues: list[dict[str, str]] = []
    ValidationRunner(settings)._validate_agent_config(path, "agents/analyst/config.yaml", issues)
    return [str(issue) for issue in issues]


def test_validation_accepts_the_product_agent_example_and_rejects_retired_tools(kernel_settings, tmp_path):
    design = (SRC / "prompts" / "skills" / "assistant-agent-design" / "SKILL.md").read_text("utf-8")
    example = re.search(r"Minimal config\.yaml:\n```yaml\n(.*?)```", design, re.S).group(1)
    assert check_config(kernel_settings, tmp_path, example) == []
    retired = example.replace("vault_read_text]", "vault_read_text, run_status]")
    assert len(check_config(kernel_settings, tmp_path, retired)) == 1
    assert len(check_config(kernel_settings, tmp_path, "agent_id: x\n")) == 1          # no schema_version


def test_migration_drops_unread_keys_and_retired_tools_and_keeps_project_narrowing():
    spec = importlib.util.spec_from_file_location("migrate", Path(__file__).parents[2] / "scripts" / "r0453_agent_config_migrate.py")
    migrate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrate)
    old = {"schema_version": "agent-config.v2", "agent_id": "pm", "aliases": ["manager"], "entrypoints": {},
           "harness": {"provider": "openai", "sandbox": "project-scope"}, "scope_profile": "current-project",
           "tools": ["get_authorized_workspace", "run_status", "write_candidate_file"]}   # no volcengine: kept so
    new, notes = migrate.clean(old, "pm")
    assert new["tools"] == ["get_authorized_workspace", "write_candidate_file", "read_project_document", "vault_list",
                            "vault_read_text"]
    assert new["harness"] == {"provider": "openai"} and not {"aliases", "entrypoints", "scope_profile"} & set(new)
    assert migrate.clean(new, "pm")[1] == []                                          # running again changes nothing
    supervisor, _ = migrate.clean({"tools": [], "mcp_servers": {}}, "supervisor")
    assert supervisor == {"tools": []}
