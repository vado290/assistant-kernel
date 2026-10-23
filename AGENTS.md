# Working on assistant-kernel

This repository is the Kernel of the personal super assistant: the control core that runs every project
(tasks, runs, cards, Discord gateway, dispatcher, Codex harness, Kernel tools, job runtime). Read
`docs/ARCHITECTURE.md` before changing anything; it lists the core principles a change must not break.

## Facts that matter while you work
- The running Kernel is the deployed production version. You are editing the next version in a task worktree;
  nothing you change affects the running system until the user deploys it.
- Do not run `git commit / merge / push / checkout / reset`: Kernel commits your worktree at the end of each turn
  and fast-forwards `main` after the user approves the release. Do not deploy, do not touch production
  databases, venvs (`assistant-data/runtime/kernel-venvs/*`) or `/etc/supervisor`.
- Tests: `python -m pytest tests -q -p no:cacheprovider` with `PYTHONPATH=src`. The core tests create and drop
  their own temporary PostgreSQL databases (connection from `ASSISTANT_POSTGRES_HOST/PORT` and
  `/home/v/AI/rag-stack/.env`).
- Database changes: add a new file in `src/assistant_kernel/core/sql/` (next number). Never edit an applied
  migration — the migrator refuses edited files, and production deploys fail.
- Prompt text in `src/assistant_kernel/prompts/core/` is the **contract** (output formats, Kernel-parsed sections):
  change it only together with the code that reads it. Skills in `src/assistant_kernel/prompts/skills/` are
  **templates** copied into projects; bump `metadata.version` and add a changelog line when you change one.
- Language (ROUND6): the interface, prompt text, skills and templates are English. Kernel's own user-facing text comes
  from `src/assistant_kernel/i18n/en.yaml` — a new string gets a key there, never a literal in Python. Models reply in the
  user's language (`i18n.LANGUAGE_NOTE`); headings Kernel parses are keys (`heading.*`), English, with older Chinese
  headings still found.
- `docs/map.yaml` is this repository's map (an index entry per file): adding, removing or renaming a Python file
  means updating it in the same change, or validation fails.
- Keep changes small and on the main path. No machinery for rare cases; the user prefers patches later over
  heavy code now.

## Layout (short)
- `src/assistant_kernel/core/` — the control core (see ARCHITECTURE.md for each module)
- `src/assistant_kernel/harness/` — Codex CLI harness (one model turn per call)
- `src/assistant_kernel/services/` — shared services kept from the earlier generation (workspace scope, documents,
  candidate bundles, validation runner, project map, schedule arithmetic)
- `src/assistant_kernel/prompts/` — contracts (`core/`) and skill templates (`skills/`)
- `tests/core/` — tests of the control core; `tests/` — tests of harness and services
- `scripts/deploy.ps1` — the user's deploy (build wheel → test venv tests → prod venv → migrate → restart)
- `docs/` — plans and records (`REBUILD_PLAN.md`, `ROUND2_PLAN.md`, `ROUND3_PLAN.md`)
