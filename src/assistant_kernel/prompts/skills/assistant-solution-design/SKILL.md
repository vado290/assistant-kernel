---
name: assistant-solution-design
description: Design stage — from the approved requirements, design the complete solution from input to usable result, verify the key dependencies, and spell out the hand-over to the build and how it is accepted.
metadata:
  version: "2.4.0"
  load: {roles: [pm], stages: [design]}
  io:
    reads: [task_contract, working_notes, state_ledger, domain_brief, charter, map, change_stream]
    writes: [solution_design, working_notes]
  changelog:
    - "2.4.0 2026-10-23 Written in English, the skills' source language; the design document is written in the user's language (ROUND6, 0.46.0)"
    - "2.3.1 2026-10-20 Added handling of reception problems, the meaning of defaults and open items, and checking this task's research first; kept the existing requirements and approval mechanism"
    - "2.3.0 2026-10-20 Design only: reception checks, business flow, dependency evidence, delegation and build hand-over; the completion standard refers to the artifact conventions"
    - "2.2.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "2.2.0 2026-10-17 Read the map before designing; look up reasons for changes as needed (ROUND4)"
    - "2.1.1 2026-10-16 The runtime manifest is jobs.yaml at the project root (the vault's runtime/ folder holds Kernel's internal data)"
    - "2.1.0 2026-10-16 The design document gains a 'how it runs' section matching the general runtime's manifest (ROUND3 S3)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): pm-design.md rewritten, with requirement coverage and parallel subtasks added"
---

# Solution design

## Receiving the requirements
Only for the full track's design; never add a design stage to the quick track or project creation. Read the R/A of the
currently approved requirements, the user's decisions in effect and the relevant working notes, ledger, charter and
research. Sort out the confirmed goals, scope, hard constraints, decisions the user made, what you may choose yourself and
the unknowns, citing the original artifacts — never invent requirements. The ledger may compress early records; not seeing
a detail does not mean the user did not answer, and an assumption in the working notes is not an approved requirement.
When the approved requirements are referenced but their text is empty or unreadable, stop designing and report the
hand-over read failure and its recovery condition with failed — do not send it back for the user to restate. Key
agreements must be traceable to the formal requirements or the user's decisions in effect; defaults explicitly agreed to
take effect on approval are followed as agreed, and items still marked for the user to decide must not be confirmed by
you. When this task's research and the project archive both exist, compare their dates, scope and sources; reuse evidence
that applies to this task, clear up conflicts first, and never mix them or override the user's agreements.

- Information that is clear but scattered: gather and cite it, without asking again; check facts you can check yourself
  first.
- An unspecified platform, tool or technology is not a missing requirement: choose within the grants and say why; use
  needs_input when the user must make an important trade-off.
- When requirements are missing, contradictory or impossible and the goal, scope, hard constraints or acceptance criteria
  must change, say so with return_to_requirements and its impact — never change requirements in the design.

## Designing from the result
1. Work back from how the user will finally use the result: getting the input → processing or executing → delivering the
   result → use or follow-up → checking the effect. Expand only the steps within the confirmed scope; research, content
   and personal plans need no software process forced on them — a delivered document can be the end point. For each key
   step state the input, action, actor, output, receiver and completion signal — a file list alone is not enough. Tell
   directly acceptable deliverables from effects influenced by third parties, and never promise results you cannot
   control.
2. Read the map first to locate the relevant capabilities, then the actual files; look up reasons for changes with
   `list_iterations(artifact=...)` when needed. Prefer reuse; when the structure changes, list the map in section 4. When a
   task needs no project file changes, say why — never invent code or jobs to fill a section.
3. Compare cost, risk, effect and maintenance only between feasible options with a real trade-off, and recommend one; when
   the route is obvious, do not force alternatives. Prefer scripts for deterministic processing, services for continuous
   listening, and agents only where understanding and judgement are needed; state the human involvement and permission
   boundaries. When the product must run on a schedule, keep running or contain agents, read assistant-workflow-design;
   the way it runs stays jobs.yaml; the design itself starts no services and makes no outside changes.
4. For dependencies the solution stands or falls on (platforms, data sources, interfaces, devices, software or
   collaborators), state their purpose and the matching R/D, why they were chosen, the conditions of use, evidence and
   date, limits, and what happens when unavailable. Mark documentation claims, real samples and actual calls separately;
   reuse existing research only where it still applies, and re-check volatile key facts specifically. For further research
   read the design branch of assistant-domain-research; investigate only within the current grants, and when you cannot
   verify, say it is unknown — never write it as available.
5. Decide by the design branch of assistant-subagent-orchestration whether to delegate investigations, inventories or
   counter-example reviews; check the key evidence and act on the findings. Having sent a subagent does not mean verified;
   small ordinary solutions you can check yourself.

## Converging and handing over
Record the conclusions in the nine sections of the design document by assistant-task-continuity and check its "design
completion standard" item by item; look for omissions from R/A, and trace important D back to their requirements to avoid
unauthorised extensions. Mark facts, assumptions and recommendations with their sources; formal conclusions go into
solution_design, judgements in progress and the next step into working_notes — never only in the summary or a subagent's
reply.

Unknowns that would overturn the core solution or affect required requirements or grants are blockers: investigate, ask or
go back to the requirements first. Items that can be left to the build state their content, impact, who resolves them,
when, the pass condition and what happens on failure; local implementation details may be decided within the design's
constraints. Use outcome = completed and document.kind = solution_design only when the design is done; this stage writes no
candidate files.
