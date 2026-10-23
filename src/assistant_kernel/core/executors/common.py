"""Shared executor helpers: agent files, prompt text, documents, JSON output and the model call."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.harness.base import AgentTurnRequest, AgentTurnResult
from assistant_kernel.core import drafts, i18n, rules, skills as skill_rules
from assistant_kernel.core.agents import AgentConfig, AgentLoader
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.storage import ProjectRuntimeManager

PROMPTS = Path(__file__).parents[2] / "prompts" / "core"
AUTH_SYNC_SECONDS = 20   # how soon a login refreshed during a model call reaches the other projects

# Tools available in the MVP (plan D3); a project's own tool list can only narrow this.
MVP_TOOLS = frozenset({
    "get_authorized_workspace", "list_authorized_tree", "read_authorized_file",
    "write_candidate_file", "delete_candidate_file",
    "read_project_document", "list_recent_history",
    "vault_list", "vault_read_text",
    "volcengine_model_status", "volcengine_generate", "volcengine_ocr",
    "read_task_document", "edit_task_document", "list_iterations", "map_lookup", "list_credentials",
    "get_credential", "list_skill_updates",
})
CANDIDATE_TOOLS = frozenset({"write_candidate_file", "delete_candidate_file"})
# Kernel's own document protocol for PM turns: granted whenever offered, a project's tool list does not narrow it.
DOCUMENT_TOOLS = frozenset({"read_task_document", "edit_task_document"})
# Platform tools every model turn gets (among those its executor offers) whatever the project's own tool list says:
# the initialising tool (every other tool needs it called first), recent conversation, the change stream (ROUND4),
# the map and the credentials (ROUND5). An empty tool list (Supervisor) therefore means "platform tools only".
PLATFORM_TOOLS = DOCUMENT_TOOLS | {"get_authorized_workspace", "list_recent_history", "list_iterations", "map_lookup",
                                   "list_credentials", "get_credential", "list_skill_updates"}
READ_ONLY_TOOLS = MVP_TOOLS - CANDIDATE_TOOLS - DOCUMENT_TOOLS - {"volcengine_generate", "volcengine_ocr"}


def effective_tools(offered: frozenset[str], configured) -> tuple[str, ...]:
    """The tools a model turn gets: what its executor offers, narrowed by the agent's config list (platform tools stay)."""
    return tuple(sorted(offered & (set(configured) | PLATFORM_TOOLS)))


def prompt_text(name: str) -> str:
    """A contract from prompts/core with its {{heading:key}} placeholders filled (the English headings Kernel parses)."""
    return i18n.fill((PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip())


def agent_home(settings, project_id: str, agent: str) -> Path:
    """An agent's own folder: its working directory, so Codex finds exactly its skills (ROUND5 D2)."""
    return ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root / "agents" / agent


def path_note(vault: Path, agent: str) -> str:
    return (f"## Paths\n\nYour working directory is `agents/{agent}` (your skills are in it; Codex lists them). The "
            f"project root is `{vault}`; relative paths in this text (such as `runs/…`, `knowledge/…`) are relative to the "
            "project root; prefer the Kernel tools for reading and writing project files.")


def load_agent(settings, project_id: str, role: str, *, stage: str | None = None, purpose: str | None = None,
               task_kind: str | None = None, native: bool = True) -> AgentConfig:
    """The project's live agent files plus the skills and shared context its load rules select for this run.
    `native`: the run works in the agent's own folder (False only for a code-delivery worktree)."""
    loader = AgentLoader(settings)
    agent = "manager" if role == "pm" else "supervisor"
    config, files = loader.load(project_id, f"project:{project_id}:{agent}")
    vault = loader.runtime.ensure(project_id).vault_root
    capabilities = skill_rules.project_config(vault)["capabilities"]
    selection = skill_rules.select(vault, settings.data_root, role=role, stage=stage, purpose=purpose,
                                   capabilities=capabilities, task_kind=task_kind)
    skills_text = selection.render(vault, native=native)
    if native:
        skills_text = "\n\n".join(part for part in (path_note(vault, config.agent_id), skills_text) if part)
    return AgentConfig(**{**config.__dict__, "skills_text": skills_text,
                          "skill_labels": tuple(selection.labels),
                          "config_sha256": skill_rules.digest(files + selection.files)})


def capabilities(settings, project_id: str) -> frozenset[str]:
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    return skill_rules.project_config(vault)["capabilities"]


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Read the single JSON object a model was asked to return (tolerates a code fence)."""
    stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (text or "").strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def write_document(settings, project_id: str, task_id: str, run_id: str, kind: str,
                   payload: dict[str, Any], actor: str) -> dict[str, Any]:
    ref = TaskDocumentService(settings).write_revision(
        project_id=project_id, task_id=task_id, document_kind=kind.replace("_", "-"),
        document_schema=f"core-{kind.replace('_', '-')}.v1", payload=payload, status="submitted",
        actor_type=actor, actor_id=actor, run_id=run_id, reason=f"{kind} submitted in run {run_id}")
    return {"kind": kind, "sha256": ref["sha256"], "uri": ref["uri"], "ref": ref}


def read_document(settings, project_id: str, document: dict[str, Any]) -> str:
    """Markdown body of a stored core document ('' when unreadable)."""
    return drafts.revision_markdown(settings, project_id, document)


USER_PROFILE = "user-profile.md"    # per project, one section per user; Kernel appends on requirements approval
DOMAIN_BRIEF = "domain-brief.md"    # the latest approved industry research
PROFILE_HEADING = ("## User profile (built up in this project; [inferred] items are not confirmed by the user, and the "
                   "user's corrections win)\n\n")
BRIEF_HEADING = "## Industry brief (knowledge/domain-brief.md; mind its dates and validity)\n\n"


def knowledge_text(settings, project_id: str, name: str) -> str:
    """A file under the project's knowledge/ folder; empty when it does not exist."""
    path = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root / "knowledge" / name
    return path.read_text("utf-8").strip() if path.is_file() else ""


def project_map(settings, project_id: str) -> tuple[str, str]:
    """(where the project map lives, its entries as compact lines): `knowledge/map.yaml`, or the repository's map
    for code delivery (ROUND5 D12). Text is '' when there is no map yet; an unreadable map is said so."""
    from assistant_kernel.core import projectmap
    _, path, shown = projectmap.location(settings, project_id)
    try:
        return shown, projectmap.render(projectmap.load(path))
    except (ValueError, OSError) as exc:
        return shown, f"(the map cannot be read: {exc}; fix it in the build stage in the format of assistant-task-continuity)"


BACKGROUND_TITLE = "archive"   # heading key (any language's heading is found, ROUND6)


def charter(settings, project_id: str) -> str:
    """The project charter as every agent reads it: knowledge/charter.md followed by its background archive.
    Since 0.45.6 the archive is its own file, knowledge/background.md (Kernel appends to it), so the charter file holds
    only the project's definition and can be shared; readers still find the archive as the charter's section.
    Empty when the project has neither."""
    text = knowledge_text(settings, project_id, "charter.md")
    background = knowledge_text(settings, project_id, "background.md")
    if background:
        body = background.split("\n", 1)[1].strip() if background.startswith("# ") else background   # drop its title
        text = (text + "\n\n" if text else "") + \
            f"## {i18n.heading(BACKGROUND_TITLE)} (appended by Kernel, knowledge/background.md)\n\n{body}"
    return text


CATALOG_SECTIONS = ("purpose", "scope", "routing")   # charter heading keys


def project_catalog(settings, project_ids: list[str], *, exclude: str) -> str:
    """Routing view of the other projects (R2.2): purpose, scope and routing info from each charter."""
    runtime = ProjectRuntimeManager(settings.data_root)
    parts = []
    for project_id in project_ids:
        if project_id == exclude:
            continue
        vault = runtime.ensure(project_id).vault_root
        text = charter(settings, project_id)
        sections = [f"**{i18n.heading(key)}**\n{skill_rules.section_text(text, key)}" for key in CATALOG_SECTIONS
                    if skill_rules.section_text(text, key)]
        if not sections:   # no charter yet: name and purpose from project.yaml
            config = yaml.safe_load((vault / "project.yaml").read_text("utf-8")) if (vault / "project.yaml").is_file() else {}
            sections = [f"**{i18n.heading('purpose')}**\n{(config or {}).get('purpose') or '(not given)'}"]
        parts.append(f"### Project `{project_id}`\n\n" + "\n\n".join(sections))
    return "\n\n".join(parts) or "(no other projects)"


DOCUMENT_TITLES = {"task_contract": "Requirements document", "solution_design": "Design document",
                   "implementation_report": "Implementation report", "review_report": "Review report",
                   "improvement_proposal": "Improvement proposal", "working_notes": "PM working notes",
                   "domain_brief": "Industry brief (this task's research)"}


def document_sections(settings, task: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(kind, fingerprint, rendered text) for each current document; the fingerprint changes with the content."""
    sections = []
    for kind, title in DOCUMENT_TITLES.items():
        document = task["documents"].get(kind)
        if document:
            body = read_document(settings, task["project_id"], document)
            if document.get("spec_error"):
                body += f"\n\n> Kernel check: the creation parameters are wrong, the project cannot be created: {document['spec_error']}"
            elif document.get("spec"):
                body += "\n\n> Kernel check: the creation parameters are valid; creating the project can be proposed."
            if document.get("iteration_error"):
                body += ("\n\n> Kernel check: the iteration record is wrong (the release is not affected, but the record "
                         f"will be incomplete); please fix it: {document['iteration_error']}")
            elif document.get("iteration"):
                body += "\n\n> Kernel check: the iteration record is valid and is stored when the release is approved."
            if document.get("effect_review_error"):
                body += "\n\n> Kernel check: the effect review did not pass and needs fixing: " + document["effect_review_error"]
            sections.append((kind, document["sha256"], f"### {title} (sha {document['sha256'][:12]})\n\n{body}"))
    validation = task["documents"].get("validation")
    if validation:   # written by Kernel, it has no revision sha of its own
        text = f"### Kernel validation result\n\n{'passed' if validation.get('passed') else 'failed'}\n\n{validation.get('report') or ''}"
        sections.append(("validation", rules.validation_fingerprint(task), text))
    if task.get("kind", "delivery") == "delivery" and task.get("track") != "quick" and task.get("stage") in ("review", "release"):
        basis = rules.review_baseline(task)
        text = ("### Kernel review baseline (document revision SHAs, not content SHAs; bound by Kernel, never filled in "
                "by the model)\n\n")
        text += json.dumps(basis, ensure_ascii=False, sort_keys=True) if basis else "Input incomplete; the review cannot pass."
        text += "\nCurrent review state: " + (rules.review_error(task) or
                                               "baseline matches and the review passed; the release still needs the user's approval")
        bundle = rules.validated_bundle(task) or ""
        if bundle.startswith("git:"):
            text += (f"\nReview the Git commit {bundle[4:]}. Read that commit's content; when the working directory "
                     "differs, its check results do not stand in for it.")
        elif bundle.startswith("no-files:"):
            text += ("\nDelivery without files: check the actual results, delivery location and acceptance evidence the "
                     "current implementation report refers to.")
        elif bundle:
            text += (f"\nFixed delivery list: workspace://project/runtime/candidate-snapshots/{bundle}/bundle.json "
                     "(read it with read_authorized_file; files lists additions, changes and deletions with content SHAs. "
                     "The vault://<this project>/ prefix of artifact_uri corresponds to workspace://project/; read only "
                     "that snapshot and never re-seal the candidates.)")
        sections.append(("review_context", drafts.text_sha(text), text))
    return sections


def render_documents(settings, task: dict[str, Any]) -> str:
    return "\n\n".join(text for _, _, text in document_sections(settings, task)) or "(no documents in effect yet)"


def workspace_snapshot(settings, project_id: str, task_id: str, variant: str, *,
                       candidates: bool) -> tuple[str, str]:
    """Pin the authorized roots for one invocation; a build snapshot is also what the publisher checks against."""
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    uri, sha = WorkspaceScopeService(settings).snapshot(
        project_id, task_id, None, candidate_enabled=candidates, variant=variant, shared_agreement=True)
    if candidates:
        plan = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root / "runs" / task_id / \
            "resolved" / "execution-plan-build.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(json.dumps({"schema_version": "core-build-scope.v1", "bindings": {
            "workspace_scope_snapshot_uri": uri, "workspace_scope_snapshot_sha256": sha}}), "utf-8")
    return uri, sha


async def call_model(ctx, run: dict[str, Any], config: AgentConfig, prompt: str, *,
                     task_id: str | None, stage: str | None, session_id: str | None,
                     tools: frozenset[str], sandbox: str, candidates: bool = False,
                     subagents: int = 0, cwd: Path | None = None) -> AgentTurnResult:
    """One Codex invocation with the project's model settings and the scoped Kernel tools."""
    settings, harness = ctx.services["settings"], ctx.services["harness"]
    project_id = run["project_id"]
    invocation_id = f"{run['run_id']}-a{run['attempt']}-{uuid.uuid4().hex[:8]}"
    allowed = effective_tools(tools, config.allowed_tools)
    model, effort, window, compact = config.model, config.reasoning_effort, None, None
    if hasattr(harness, "resolve_model_profile"):
        profile = await harness.resolve_model_profile(
            project_id=project_id, provider=config.provider or settings.default_codex_provider,
            model=config.model or settings.default_codex_model, reasoning_effort=config.reasoning_effort,
            context_policy=config.context_policy,
            custom_context_window_tokens=config.model_context_window_tokens,
            custom_auto_compact_token_limit=config.model_auto_compact_token_limit)
        model, effort = profile.model, profile.reasoning_effort
        window, compact = profile.model_context_window_tokens, profile.model_auto_compact_token_limit
    scope_context: dict[str, str] = {}
    if allowed:
        owner = str(task_id or run["run_id"])
        scope_uri, scope_sha = workspace_snapshot(settings, project_id, owner, invocation_id, candidates=candidates)
        scope_context = {
            "ASSISTANT_DATA_ROOT": str(settings.data_root), "POSTGRES_DB": settings.postgres_database,
            "KERNEL_SCOPE_PROJECT_ID": project_id, "KERNEL_SCOPE_RUN_ID": str(run["run_id"]),
            "KERNEL_SCOPE_TASK_ID": owner, "KERNEL_SCOPE_CLAIM_TOKEN": str(run["claim_token"]),
            "KERNEL_SCOPE_WORKER_INSTANCE_ID": str(run["worker_id"]), "KERNEL_SCOPE_CORE": "1",
            "KERNEL_SCOPE_ALLOWED_TOOLS": ",".join(allowed), "KERNEL_SCOPE_STAGE": stage or "",
            "KERNEL_SCOPE_SNAPSHOT_URI": scope_uri, "KERNEL_SCOPE_SNAPSHOT_SHA256": scope_sha,
            "KERNEL_SCOPE_EFFECTIVE_PERMISSIONS_JSON": "{}", "KERNEL_SCOPE_CONTEXT_VIEW_JSON": "{}",
        }
    from assistant_kernel.core.executors.auth_sync import sync
    await asyncio.to_thread(sync, settings, project_id, direction="in")

    async def share_refreshed_login() -> None:
        """A login Codex refreshes mid-turn invalidates every other copy: hand it on soon, not when the turn ends."""
        while True:
            await asyncio.sleep(AUTH_SYNC_SECONDS)
            try:
                await asyncio.to_thread(sync, settings, project_id, direction="out")
            except OSError:
                pass

    sharing = asyncio.create_task(share_refreshed_login())
    try:
        return await harness.run_turn(AgentTurnRequest(
            project_id=project_id, prompt=f"{prompt}\n\n{i18n.LANGUAGE_NOTE}",   # replies in the user's language (ROUND6)
            cwd=cwd or agent_home(settings, project_id, config.agent_id),
            physical_session_id=session_id, provider=config.provider, model=model, reasoning_effort=effort,
            context_policy=config.context_policy, model_context_window_tokens=window,
            model_auto_compact_token_limit=compact, sandbox=sandbox, timeout_seconds=config.timeout_seconds,
            invocation_id=invocation_id, cancel_event=ctx.cancel, scoped_tools=allowed, scope_context=scope_context,
            multi_agent_enabled=subagents > 0, max_concurrent_subagents=subagents))
    finally:
        sharing.cancel()
        await asyncio.to_thread(sync, settings, project_id, direction="out")


async def call_for_json(ctx, run, config, prompt, **kwargs) -> tuple[dict[str, Any] | None, AgentTurnResult]:
    """Call the model; if the final message is not a JSON object, ask once in the same session to fix it."""
    result = await call_model(ctx, run, config, prompt, **kwargs)
    parsed = parse_json_object(result.final_text)
    if parsed is None and result.physical_session_id:
        fix = ("Your last message was not a valid JSON object. Follow the output requirements exactly: output only that "
               "JSON object and do no new work.")
        result = await call_model(ctx, run, config, fix, **{**kwargs, "session_id": result.physical_session_id})
        parsed = parse_json_object(result.final_text)
    return parsed, result
