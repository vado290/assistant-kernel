---
name: assistant-kernel-maintenance
description: Extra rules when the kernel project changes itself — the running kernel and the code being changed stay strictly apart, changes keep the core principles and are covered by tests, and the user deploys.
metadata:
  version: "1.1.0"
  load: {roles: [pm, supervisor], stages: [design, build, review, evolution], purposes: [evaluate], capabilities: [kernel_maintenance]}
  changelog:
    - "1.1.0 2026-10-23 Written in English, the skills' source language; interface text lives in i18n/en.yaml (ROUND6, 0.46.0)"
    - "1.0.0 2026-10-16 Enabled (R4b): kernel maintains itself by code delivery; Kernel commits automatically, tests run on the commit, the user deploys after the merge"
    - "0.1.0 2026-10-16 Draft: enabled with R4b (kernel self-maintenance)"
---

# Maintaining the kernel itself

## Basic facts
- You are run by the current production kernel; you change the next version. Your changes do not affect the running
  system until the user deploys them.
- `AGENTS.md` at the repository root and `docs/ARCHITECTURE.md` describe the layout, the core principles and how to test;
  read them first. When you change the architecture or a principle, update both documents.
- Never change the production virtual environments, configuration or database, and never run the deploy script.
- Kernel runs the test command on your commit during validation (the core tests create and drop their own temporary
  databases); during the build run the relevant tests in the workspace too — the command is in AGENTS.md. After the
  release main is fast-forwarded to your commit; it goes live only when the user runs `deploy.ps1`.
- Kernel's interface is English: text the user reads from Kernel itself comes from `src/assistant_kernel/i18n/en.yaml`;
  a new user-facing string gets a key there, never a literal in code. Prompt text for models is English; models reply in
  the user's language (the language note Kernel appends to every prompt).

## PM: design and build
1. The design document must say whether the change touches a core principle in `docs/ARCHITECTURE.md` (for example "each
   active task has exactly one cursor", "one card per project") and how it is kept intact.
2. A change to the state machine or to decision types adds or changes the matching scenario tests.
3. A change to prompt text or skills says which roles and occasions it affects, and updates versions and changelogs by
   assistant-capability-maintenance.
4. Database migrations only add files; a migration must run again on existing data without errors.

## Supervisor: evaluation and the release card
Assess the risk of the change (migrations, state machine, gateway, skills) and check that tests cover it; the release card
states the commit id, the test results, whether it includes a migration, and that the user must deploy after the merge.

## PM: polishing the shared templates in reviews
Besides this project's run records, kernel's weekly review gets Kernel's summary of every project's operations (by each
project's sharing level): each project's run metrics, review proposals, changes to its skill copies and why. Use it to
judge where the shared templates (the skills in `prompts/skills/`, `core/project_template/`) should change:
- several projects made the same kind of change to one skill → most likely a template problem; propose taking the change
  into the template;
- metrics around a skill are off everywhere (for example many question rounds in requirements, a high review rejection
  rate) → find the cause in the wording and propose concrete new wording;
- a change only one project needs → it stays in that project, not in the template.
State in the proposal which projects and data the evidence comes from. Once a template change is released, each project
decides in its own review whether to adopt it.
