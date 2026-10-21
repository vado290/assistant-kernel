# Super Assistant

English · [中文](README.zh-CN.md)

Internally the system is called the **Assistant Kernel** (Python package `assistant_kernel`); this repository is
its source.

A document-driven personal assistant. You talk to each project's **PM** and **Supervisor** in Discord; the
Kernel runs short `codex exec` turns for them, keeps all state in PostgreSQL, and keeps every project's files in
their own folder with their own git history.

The design, its decisions and the progress of each round are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/ROUND5_PLAN.md](docs/ROUND5_PLAN.md). Older documents in `docs/` describe earlier generations and are kept
for history.

## How a task flows

```text
#pm message ─► triage: reply, start a task, or hand over to another project
task ─► Supervisor evaluates ─► one card in Discord (questions / next step / decision)
you answer or click ─► PM turn in the current stage ─► Supervisor evaluates again ...
stages: requirements → design → build → (Kernel validation) → review → release → completed
```

- Each project has one active task; an active task always waits on exactly one thing: one run or one card.
- Documents (requirements, design) and every release need your approval; an approval stays valid only while the
  approved document is unchanged. A project's files change only through a task: proposed files are validated and
  released; a code project's repository and external folders granted for writing are edited in the build stage and
  committed when the release is approved.
- Messages in `#pm` go to the current task (or start one); messages in `#supervisor` are consultations.
- Text commands: `/status`, `/pending`, `/cancel 确认`, `/evolve`, `/iterations`, `/grants`, `/jobs`, `/secrets`.

## Projects

Every project is a folder `assistant-data/vaults/<project>/`:

| Path | Content |
|---|---|
| `project.yaml` | delivery mode, capabilities |
| `knowledge/charter.md` | what the project is for, scope, routing |
| `agents/<agent>/` | `role.md`, `config.yaml`, and that agent's skills in `.agents/skills/` |
| `knowledge/` (other files) | background archive, user profile, domain brief, project map — written by Kernel |
| `runtime/` | Kernel internals, including the project's credential store — never in git |
| `runs/` | per-task working files — never in git |

The default project `kernel` (maintaining this Kernel, creating and routing projects, the shared background) is
published on its own as [project-kernel](https://github.com/vado290/project-kernel); see its README.

## Sensitive data: what is stored where, and who can see it

This section is the honest picture; please read it before you put real accounts into the system.

### 1. Things that are never in any git repository

| What | Where | Notes |
|---|---|---|
| Discord bot token, model provider keys | `assistant-data/config/service-credentials.json`, `provider-secrets.json` | outside every repository; not copied by the backup script |
| Codex login | `auth.json` in each project's `runtime/codex-home/` | removed from backups |
| Project credentials (logins, API keys) | `assistant-data/vaults/<project>/runtime/credentials.json` | `runtime/` is ignored by every project's git |
| Task state, messages, cards | PostgreSQL schema `core` | in your own database only |

This repository contains code, prompts and skill templates only; the keys in its tests are placeholders.

### 2. How project credentials are collected and used

1. When a task needs a login or key, the PM first lists what the project already has (`list_credentials` shows
   names, services and purposes — never values).
2. Only if it is missing, the PM asks you with a question marked as a credential. The card field has no suggested
   answer, and says where the answer goes.
3. When you submit, Kernel writes the value into the project's credential file (created on first use, file mode
   600, writes locked). The conversation record keeps only "saved as project credential `<key>`".
4. An agent that really needs the value reads it with `get_credential`. The skills tell agents never to copy a value
   into documents, code, maps, logs or messages; this is a prompt rule, not a technical lock.
5. `/secrets` lists the names; `/secrets delete <key> 确认` removes one.

### 3. Who can see a credential's value

- **The agents of the same project** (PM, Supervisor, product agents) can read it through the Kernel tools.
- **Other projects' agents**: the Kernel tools are scoped to their own project, so they offer no way to another
  project's credentials. But an agent also has a shell: it can read files on the machine (read-only outside the
  build stage; unrestricted in the build stage, where it edits code). Projects on one installation are therefore
  **not a hard security boundary** against each other — keeping them apart rests on the agents' rules. Run work
  that must be strictly separated on a separate installation.
- **The model provider** sees a value whenever an agent reads it during a turn, as part of that turn's context.
- **Your machine** keeps Codex session logs in each project's `runtime/codex-home/` (git-ignored).

If that is not acceptable for a given secret, do not store it in the assistant; give the project a key with the
smallest permissions it needs (for example read-only exchange keys).

### 4. Personal background

Kernel builds up a memory of you and your work: the background archive (`knowledge/background.md`), the user profile
(`knowledge/user-profile.md`), domain briefs, and the shared background (`kernel/knowledge/shared/`). These files stay
in your installation. In a private project they are part of that project's own private git history. The public
`kernel` project ignores them: every installation creates its own (the shared background starts from the templates in
`core/shared_template/`, and Supervisor proposes changes on a card that you approve).

### 5. Version history and GitHub

- Kernel commits each project folder (and external folders the project may write to) before and after every
  release, so every change can be traced and undone.
- Pushing goes to repositories under **your own** GitHub account (the account `gh` is logged in as, or
  `CORE_GITHUB_OWNER`); new repositories are created **private**. Nothing is pushed until you run `gh auth login`.
- A project whose settings say `git_push: false` only commits locally and is never pushed — use it for anything
  you keep private.
- Making a repository public is always your own step on GitHub.

### 6. Backups

`scripts/Backup-AssistantState.ps1` writes a local backup (database dump, project folders including their
credential files, without Codex logins) to `assistant-data/backups/`, which no repository tracks. Keep backups on a
disk you control, not on a public share.

### 7. Known limitation

On a card with more than five questions, answers typed on earlier pages are kept as a draft in the database until
the card is submitted — including an answer to a credential question. A fix is planned.

### 8. Your checklist

- Never commit `assistant-data/config/*.json`; keep `assistant-data/` out of any repository you publish.
- Use keys with minimal permissions; rotate any key that was ever pasted somewhere it should not be.
- Keep private projects at `git_push: false`, and check a repository's content before making it public.

## Code map

| Path | Role |
|---|---|
| `core/sql/` | `core` schema migrations (applied by `assistant-kernel-migrate`) |
| `core/rules.py` | stages, allowed moves, gates, derived approvals (pure functions) |
| `core/machine.py` | task state machine — the only code that changes task state |
| `core/cards.py` | the one open card per project |
| `core/kernel.py` | entry points: messages, card actions, claim/complete/fail runs, recovery |
| `core/dispatch.py` | dispatcher: runs claimed runs concurrently, leases, maintenance |
| `core/executors/` | PM, Supervisor, triage, validation, publish, bootstrap and job executors |
| `core/tools.py` | Kernel tools for one model call (stdio MCP, scoped to the project, lease checked on every call) |
| `core/credentials.py` | the per-project credential store |
| `core/projectgit.py`, `core/projectmap.py` | project version history and GitHub; the project map |
| `core/skills.py`, `core/vault_sync.py` | skill loading and template sync; Kernel's own file writes (archive, profile, shared background) |
| `core/gateway/` | Discord gateway (cards, buttons, modals, outbox, reminders, commands) |
| `harness/` | `codex exec` runner, tool-server launch, process control |
| `services/` | reused libraries: workspace scope, documents, candidates, validation |
| `prompts/core/`, `prompts/skills/` | output contracts; skill templates copied into each agent's folder |

## Operations

```powershell
.\scripts\deploy.ps1            # build wheel → test venv + tests → prod venv → migrate → restart → check
.\scripts\deploy.ps1 -TestOnly  # test venv only
```

The Kernel runs in a Docker container (paths under `/home/v/AI`, data in `assistant-data/`). Production processes
(`deploy/supervisord.conf`): `kernel-core-dispatcher`, `kernel-core-gateway`, `kernel-api` (`/health/live`,
`/health/ready`, `/version`), and project services registered from a project's released `jobs.yaml`.

Tests: `tests/core/` (each test gets a temporary database) and a few library tests in `tests/`.
