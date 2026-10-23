---
name: assistant-workflow-design
description: Design products that run on their own — how to split scheduled scripts, agents inside the product and long-running services, how to declare them in jobs.yaml, how to make them safe to re-run and recover from failures; use when a product must run on a schedule or keep running.
metadata:
  version: "2.2.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  io:
    reads: [task_contract, map]
    writes: [solution_design, jobs]
  changelog:
    - "2.2.0 2026-10-23 Written in English, the skills' source language (ROUND6, 0.46.0)"
    - "2.1.0 2026-10-21 Clarified the boundaries of runtime data, periodic output, static deliverables and working directories; kept the existing release and execution mechanisms"
    - "2.0.2 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "2.0.1 2026-10-16 The runtime manifest is jobs.yaml at the project root (the vault's runtime/ folder holds Kernel's internal data)"
    - "2.0.0 2026-10-16 Rewritten for core v6 and the general runtime (ROUND3 S3): every way of running is declared in jobs.yaml and registered on release"
---

# Designing how it runs

## Choose the simplest way to run first
| Need | Use | Notes |
|---|---|---|
| Deterministic computing, totals, conversion, fetching data | **script job** | First choice. Cheap, testable, stable results |
| Understanding, judgement, writing analysis or advice | **agent job** | One read-only model turn per run; the result goes to a channel |
| Continuous listening, long connections, high-frequency collection | **service** | Supervised by supervisord, restarted on crashes; optional scheduled restarts |
| One-off or triggered by hand | not registered | Done by a task, or the user runs a registered job with `/jobs run` |

Prefer scripts to agents and schedules to long-running services. One pipeline can be split into several jobs: a script
prepares the data, then an agent writes the analysis from it.

## What the design's "how it runs" section states
One line per job or service, the fields matching `jobs.yaml`: id · type · entry · schedule (with time zone) · timeout ·
retries · alert channel; for agent jobs also the gist of the prompt and where the result goes; for services the start
command and whether it restarts on a schedule. Also state:
- **Inputs and outputs**: what is read, where it is written, who uses it; paths name their root and relative location,
  and outside directories need an existing grant. Scripts run in the vault for file delivery and in the repository for
  git delivery; business agent configuration is still read from the vault. Check how paths are actually configured — do
  not assume Kernel mounts data folders or injects new path environment variables.
- **Safe to re-run**: re-running the same time does not send or book anything twice (for example check first whether
  today's result exists).
- **Incremental**: periodic jobs continue from the last successful boundary instead of recomputing everything; the
  boundary is kept in project files.
- **On failure**: how many retries, who gets the alert, how a person repairs it (`/jobs run <id>`), whether half-finished
  output needs cleaning up.
- **Configuration and credentials**: scripts cannot reach Kernel's database or keys; how outside services' credentials
  are provided is stated in the design for the user to confirm.

## Runtime data and deliverables
- Code, templates, configuration and static documents promised in this task go through the task's delivery/release;
  approved jobs produce periodic data and reports within the agreed scope without a new build each time. Whether
  something is a released deliverable is judged by its purpose, producer and the agreements in effect — not just by
  folder names such as artifacts/ or data/.
- Persistent business data and runtime output use the project's existing convention or an explicit persistent location,
  never only a worktree, candidate area or test scratch area that gets cleaned up. Without a convention data/ or
  artifacts/ may be chosen as needed; Git projects state the actual data root, how it is configured and that it is
  excluded from commits — never assume a relative path means the vault.
- State what is version-controlled, what is mutable runtime data and what can be rebuilt; keep test data apart from real
  business data, and credentials out of commits. Do not use Kernel's managed runs/ and runtime/ areas as business resource
  templates or default data folders.
- Moving folders requires checking entry points, references and consumers; reverting code does not undo runtime output
  or delete business data. For overwriting, migration or clean-up, state the authorised retention and recovery method;
  without an agreement keep the existing data — never set clean-up deadlines or add clean-up jobs on your own. Read-only
  business agents still only read and hand back results; production steps that write files belong to scripts or
  services with the matching grant.

## Agents inside the product
When an agent job is needed, design its `agents/<name>/config.yaml`, `role.md` and its own skills by
assistant-agent-design.

## Verification and review
- Build stage: run each script for real at least once (with test data or a read-only/dry-run option) and put the command
  and output into the implementation report.
- During validation Kernel checks the manifest's format, schedules and that entry files exist.
- The review checks: safe re-runs, failure handling, where alerts go, credentials not stored, schedules matching the
  requirements.
