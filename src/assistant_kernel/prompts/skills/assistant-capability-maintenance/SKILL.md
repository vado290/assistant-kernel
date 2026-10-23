---
name: assistant-capability-maintenance
description: Rules for writing and changing skills (and agents' config.yaml) — a skill's fixed structure, writing principles, how templates relate to project copies, and the change process. Use when proposing or implementing skill improvements.
metadata:
  version: "2.7.0"
  load: {roles: [pm], stages: [build, evolution]}
  io:
    reads: [improvement_proposal, skills]
    writes: [skills]  # written only in an approved build; evolution only writes the improvement proposal
  changelog:
    - "2.7.0 2026-10-23 Written in English, the skills' source language; skills stay language-neutral, models reply in the user's language (ROUND6, 0.46.0)"
    - "2.6.0 2026-10-22 D24 restored: unchanged copies are updated automatically with each Kernel version; skill adoption batches only for special cases such as changed copies (0.45.11)"
    - "2.5.0 2026-10-22 Added the evolution-only review of the agents' own skills, roles and loading, and the hand-over of approved changes"
    - "2.4.0 2026-10-21 Skill adoption is checked by fixed batch, full directory and project policy; blocks and effects are handed over through existing tasks and reviews"
    - "2.3.0 2026-10-19 Template updates described by the actual practice of ROUND5 D20; added the valid fields of agent configuration (config.yaml) and tool name rules (0.45.3)"
    - "2.2.0 2026-10-18 Skills moved to one flat folder per agent, agents/<agent>/.agents/skills/ (ROUND5 R5.1)"
    - "2.1.0 2026-10-17 The fixed structure gains io metadata and a 'hand-over' section; skills collaborate only through artifacts; reasons for changes go into the iteration record (ROUND4)"
    - "2.0.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): skills decide how agents behave, so changing them needs common rules to stay stable over long iteration"
---

# Writing skills

## What a skill is
A skill is a description of a working method for an agent. Kernel loads it automatically for the right roles, stages and
occasions by the `load` rules at its top. Skills decide how agents behave, and whether agents hand over to each other and to
artifacts rigorously.

## Templates and project copies
- **Shared templates**: a set of base skills kernel maintains. When a project is created, Kernel copies them by each
  template's load.roles into the matching agent's own folder `agents/<agent>/.agents/skills/` (one for the PM and one for
  the Supervisor; the same skill may appear twice) and records in each copy where it came from,
  `template: {name, version}`. Polishing the shared templates is part of kernel's self-improvement.
- **Project copies**: once copied they belong to the project. The project changes its copies in its own operation and
  reviews, and may rework or even overturn the template; Kernel loads only the project's own copies.
- **Template updates (ROUND5 D24)**: Kernel syncs the templates once per version: new template skills are installed for the
  matching agents; copies the project **never changed** (body and metadata matching the fingerprint taken at installation)
  are replaced with the new version automatically; changed copies stay as they are, and the weekly review sees "template
  updated" with its changelog, so the project decides whether and what to adopt; skills the project deleted are not put
  back.
- **Skill adoption batches (special cases, off by default)**: when a template must be merged into a copy the project
  changed, or a whole batch must be replaced with a way back, a maintainer can configure an exact approved policy executed
  through Kernel's skill update batches. A merge candidate keeps the project's conventions and has a matching approval —
  never disguise a customised merge as the pure template.
- **Hand-over and effect** (skill adoption batches only): read this project's update_id, fixed list, reasons for blocks and
  receipts with list_skill_updates; only the maintenance executor can change an adoption state. When development or a
  merge is needed, the requirements still state the goal and the behaviour that must stay; the design states the baseline,
  compatibility conditions and rollback; build delivers the fixed candidate and the full-directory diff; review checks the
  same candidate. All of this goes into the existing artifacts, with no new stage or approval. A released template alone
  does not mean the project allows adoption; without a configured path or permission, hand it to the maintainer — never let
  a model change the policy or the database directly. Operation tells "files applied", "skill loaded / discoverable on
  demand" and "business effect reviewed" apart; blocked items state the reason, who takes over and the recovery condition,
  and P / follow-up tasks cite the update_id. Never blindly retry a rolled-back or deferred target; after a fix, check the
  original problem. Skill adoption moves no business folders, deletes no runtime data and does not void old tasks'
  approvals.
- **Loading**: a skill whose `load` names occasions is loaded in full every turn there; a skill with `on_demand: true` is
  only listed by name, purpose and path, and read in full with `vault_read_text` when needed (for rarely used domain
  methods, to save context).

## Agent configuration (config.yaml)
Only these fields of an agent folder's `config.yaml` take effect: `schema_version: agent-config.v2`, `agent_id`,
`agent_key`, `kind`, `status`, `role` (role.md by default), `harness` (provider, model, reasoning_effort, context_policy,
timeout_seconds) and `tools`.
- `tools` may only name current tools (the check before release rejects unknown names; the names listed in its error are
  authoritative): it can only narrow the tools the executor offers, never widen them.
- Platform tools are always given and need not be listed: get_authorized_workspace, list_recent_history, list_iterations,
  map_lookup, list_credentials, get_credential, and the PM's task document tools. So `tools: []` for the Supervisor means
  "platform tools only".
- The sandbox and whether candidate files can be written are decided by Kernel per role and stage, not in the
  configuration; other fields have no effect — do not add them.

## evolution: reviewing the agents' own roles and skills
Read, only for an actual problem, the relevant agent's config.yaml, the file its role points to (role.md by default), the
project's skill copies and the necessary template differences; no full rewrite. The role expresses responsibilities and the
project's special requirements, skills express working methods, the charter expresses long-term business agreements; check
for gaps or overlaps in responsibility, conflicts, unavailable inputs or tools, and output nobody takes over. Broken io
hand-offs are only metadata hints — check the stage, conditions and real consumers; never judge optional or
platform-provided inputs as defects.

Judge separately: the file exists, the load rule selects it, it is discoverable on demand, it was actually read in full,
actually followed, and the business effect. Check versions with the skills/config_sha256 in existing run records and their
evidence; a hash is not a content snapshot — without a reliable old baseline mark it unknown, and never explain old turns
with the current role or skill. Check whether the context misses key inputs or loads irrelevant content; never delete
acceptance conditions, widen permissions or hide failures for a better score.

A proposal states the target path/section, the evidence of the problem, the concrete new text or diff, the customisation
that must stay, the affected roles/stages, verification and recovery. evolution only proposes in improvement_proposal;
changes to a project's role/config go through a normal task the user approves and are not automatic overwrites of the
skill update table. Project copies and shared templates are handled separately — never approve for another project. After
a change, check separately that the file was adopted, that later turns load or read it on demand, the behaviour and the
effect; when not yet adopted or rolled back, state the actual version. Approvals, candidate validation and permissions stay
with the existing process; this section does not change build's rules for making changes.

## Fixed structure
```
---
name: short English name (lowercase letters and hyphens), the same as the folder name
description: one sentence: when to use it, what it produces
metadata:
  version: "major.minor.patch"
  load: {roles: [...], stages: [...], purposes: [...], capabilities: [...], task_kinds: [...], on_demand: false}
  # stages match the PM's stage turns, purposes the other occasions (the Supervisor's evaluate / review_input / consult and
  # triage); when both are given either one is enough; roles, capabilities and task_kinds must hold; missing keys do not restrict
  io: {reads: [artifact names, ...], writes: [artifact names, ...]}   # optional, with the artifact names below; reviews use it to check hand-offs
  template: {name: template name, version: template version when copied}   # only project copies, written by Kernel
  changelog: ["version date what changed and why (citing the proposal id)"]   # required for shared templates; project copies may omit it (the reason is in the iteration record)
---
# Title
## Purpose (why it is done this way)
## When to use / not to use
## Inputs (which sections of which artifacts) and fact priority
## How (numbered steps, each checkable)
## Output (which section of which artifact)
## Hand-over (what the next skill or role that reads it needs)
## Self-check
## Common mistakes (counter-examples)
```
Sections that are not needed may be left out, but the order stays.

**io artifact names** (the same as in assistant-task-continuity): task_contract · solution_design · implementation_report ·
review_report · improvement_proposal · domain_brief · working_notes · charter · map · jobs · skills; provided by the
platform (read-only): digest (run record) · change_stream · state_ledger · user_profile · template_updates ·
shared_context · user_answers.
**Skills collaborate only through artifacts**: what A writes must be read by B in its inputs; when you change an artifact's
section names or format, check every skill that reads it. The review material's "broken skill hand-offs" lists artifacts
written but never read, or read but never written — the starting point for checking hand-over problems.

## Writing principles
1. **One skill covers one kind of work**; a rule lives in one skill only, and others refer to it by name instead of copying
   it.
2. **Say why**: follow each requirement with half a sentence of reason — an agent that understands the reason can handle
   cases not written down.
3. **Checkable**: use checkable requirements such as "every R has at least one A", not "make it as complete as possible".
4. **Aligned with artifacts**: the output part names the artifact and section (as in assistant-task-continuity); when a
   section name changes, update every skill that refers to it.
5. **Give counter-examples**: "common mistakes" describes problems that really happened, ideally from review records.
6. **Keep it short**: a skill is usually within about 3000 words; longer means it covers too much and should be split.
7. **Plain and language-neutral**: skills are written in English, concrete, with little jargon; models reply in the
   language the user writes in (the language note at the end of every prompt), so a skill never fixes the user's language;
   document headings come from the output requirements and assistant-task-continuity and stay in English. No retired
   process terms.
8. **No conflict with the contracts**: the output JSON format and the allowed decisions are set by Kernel (the output
   requirements); a skill cannot add or change fields.

## Change process
1. A review or user feedback raises a problem → the improvement proposal names the skill, section and new wording to
   change, and why.
2. Once the user approves it, it is implemented as a task: change the skill file and raise the version (template-update
   decisions depend on the version); the reason goes into the implementation report's iteration record (shared templates
   also get a changelog line).
3. A project changes only its own copies; changes to shared templates are tasks of the kernel project. When polishing the
   templates, kernel draws on the projects' review proposals and their changes to their copies: when several projects made
   the same kind of change, the template itself usually needs changing.
4. When adopting a template update, compare the new template with this project's copy section by section: keep this
   project's own changes, merge the template's improvements, write in the changelog "adopted which parts of template X.Y.Z,
   kept which local changes", and update `template.version`.
5. After the change, watch the next review's records to confirm whether the problem improved.
