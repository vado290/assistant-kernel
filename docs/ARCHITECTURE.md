# Kernel architecture

The Kernel runs every project of the personal super assistant. A project has a vault
(`assistant-data/vaults/<project>/`: charter, agents, skills, files, run evidence) and two Discord channels:
#pm (requests, conversation with the PM agent) and #supervisor (cards, consultation with the Supervisor agent).

## Core principles (a change must keep all of them)
1. **One state owner.** Only `core/machine.py` changes task state; only `machine.py` and `kernel.py` write
   `core.runs`; only `cards.py` writes `core.interactions` (checked by `tests/core/test_core_rules.py`).
   Executors return result dicts; they never change state themselves.
2. **One cursor per active task.** An active task waits on exactly one thing: one run or one card item
   (`rules.CURSOR_RUN`, `invariants.py`). One active task per project; others queue.
3. **One card per project.** Everything that needs the user is an item on the project's single card in #supervisor.
   A handled or replaced item gets a new id, so old buttons are recognised as stale.
4. **Approvals are derived, never cleared by hand.** An approval stores the sha of what was approved
   (document revision, commit); it is valid only while the current sha still matches (`rules.approval_valid`).
5. **Claim in one statement.** `Kernel.claim` checks every execution condition in a single SQL statement;
   it first locks the candidate project, then rechecks all conditions in the claiming UPDATE so skill adoption cannot race a new Run.
   Leases expire and are recovered; a stale result (not the task's current cursor) is discarded.
6. **Contracts vs skills.** What Kernel code parses or relies on lives in `prompts/core/` and changes with the code.
   Skills are project-owned templates and can change freely.
7. **Kernel writes what follows from state.** Files that mirror database state (charter archive, iteration log,
   shared context, worktree cleanup) are written by `vault_sync.py` on the dispatcher's maintenance tick, each
   step idempotent.
8. **Each agent owns its skills and works in its own folder** (ROUND5). Skills live only in
   `agents/<agent>/.agents/skills/` (one copy per agent that uses a skill); no `.agents/skills` above an agent's
   folder and no `AGENTS.md` in a project (`skills.layout_problems`, checked at validation). A model call runs in
   the agent's folder, so Codex lists exactly that agent's skills; Kernel injects only the core skills of the
   current stage in full and switches off the skills Codex ships with. Code-delivery build / review run in the
   task worktree and get Kernel's on-demand skill list instead.
9. **A task waiting 7 days for the user ends on its own** (`Kernel.expire_idle_tasks`), so it never blocks the
   project's next task.

## Flow of a task
user message in #pm → triage (chat / start a task / route) → task `queued` → `supervising` (Supervisor proposes
the next step) → card item (user confirms) → `pm` (one PM turn of the current stage) → `supervising` → …
Stages: requirements → design → build → review → release (full track) or requirements → build → release (quick
track). Build output is a candidate bundle (file delivery) or a commit in the task's worktree (code delivery);
`validating` runs Kernel validation; release needs the user's approval; `publishing` applies it.
A project-creation task (`bootstrap`) only drafts its charter; approval creates the project. A weekly
`evolution` task reviews the project and proposes follow-up tasks.

## Modules (`src/assistant_kernel/core/`)
| Module | Role |
|---|---|
| `kernel.py` | API used by gateway and dispatcher: receive messages, card actions, claim/complete/fail runs, digests |
| `machine.py` | the state machine: every transition, card item and run enqueue |
| `rules.py` | stage moves, approvals, creation parameters — pure functions |
| `cards.py` | the per-project card |
| `dispatch.py` / `dispatch_main.py` | claims runs and executes them; maintenance tick (lease recovery, evolution schedule, vault sync, job runtime), each step isolated so a failing one never stops claiming |
| `executors/` | `pm` (PM turns), `supervisor`, `triage`, `delivery` (validation, publish), `bootstrap`, `job`, plus helpers (`common`, `state` ledger, `digest`) |
| `tools.py` | the MCP tool server for model turns; every call checks the run's lease |
| `drafts.py` | task documents as drafts edited through tools, saved as immutable revisions at turn end |
| `skills.py` / `agents.py` | skill loading by `metadata.load` rules; agent config loading |
| `skill_updates.py` | project skill-adoption batches, verified full-directory baselines, guarded writes and recovery; disabled without an approved policy; see SKILL_ADOPTION.md |
| `gitdelivery.py` | code delivery: worktrees, Kernel commits, tests on the commit, fast-forward publish |
| `runtime.py` / `jobs.py` | released `jobs.yaml` (vault top level; code delivery: repository root on main) → scheduled jobs and supervisord services of projects |
| `vault_sync.py` | Kernel's idempotent file writes that follow from state; pushes project history to GitHub |
| `projectgit.py` | each project folder (and each folder it may write to) is a repository: snapshot before a release, commit after it, private GitHub remote via `gh` |
| `projectmap.py` | the project map `map.yaml` (one index entry per file; every product Python file listed), checked at validation, `map_lookup` tool |
| `credentials.py` | the project's credentials `runtime/credentials.json`: look first, ask with a `credential` question, never in the conversation |
| `gateway/` | Discord gateway: messages, cards, buttons, text commands (`/status /pending /cancel /evolve /jobs /iterations /grants /secrets`) |
| `sql/` | migrations, applied in order, never edited after applying |

## Processes (supervisord in the AI container)
`kernel-core-dispatcher`, `kernel-core-gateway`, `kernel-api` (health/version), plus project services
`proj-<project>-<service>` registered by the job runtime. Deploy: `scripts/deploy.ps1` (the user runs it).
