---
name: assistant-subagent-orchestration
description: Hand independent, reading-heavy or parallelisable temporary work to subagents — when to send one, how to write its brief, how to handle the questions it raises, how to check and combine the results.
metadata:
  version: "2.7.0"
  load: {roles: [pm], stages: [requirements, design, build, review, evolution]}
  changelog:
    - "2.7.0 2026-10-23 Written in English, the skills' source language; charter sections are named by heading, not number (ROUND6, 0.46.0)"
    - "2.6.0 2026-10-22 Evolution only: self-contained context, coverage of read-only research, reviews of roles and skills, and acceptance of results"
    - "2.5.0 2026-10-21 Added risk- and benefit-based delegation conditions for evolution only"
    - "2.4.0 2026-10-21 Added delegation conditions, a common baseline and responsibility for combining results for review only"
    - "2.3.0 2026-10-20 Added the build division of work: the PM registers file candidates one at a time, Git works by write sets; integration and failure hand-over made explicit"
    - "2.2.0 2026-10-20 Design only: delegation conditions, read-only investigations and acceptance of results; other stages keep the original definitions"
    - "2.1.1 2026-10-18 Skills moved to one flat folder per agent, agents/<agent>/.agents/skills/ (ROUND5 R5.1)"
    - "2.1.0 2026-10-16 Added predefined subagents (industry research, codebase survey, counter-example review, parallel implementer); no fork_turns; registered in delegations (ROUND3 S2)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): subagents cannot talk to the user, so the main agent owns briefs and the return of questions"
---

# Delegating to subagents

## What a subagent is (and therefore how to use it)
- It is a temporary helper within this turn; it disappears when the turn ends and **does not exist next turn**.
- It **cannot talk to the user**: it sees no cards and gets no answers from the user. Its questions come back only to you.
- It knows only what your brief tells it; it cannot see your conversation or the artifacts unless you give it file paths
  to read. Do **not set fork_turns** when sending one (that copies your whole conversation to it — costly in tokens and an
  uncontrolled context); write the background it needs into "required inputs".
- It can search the web and use Kernel's tools (read project files, read_task_document, and edit_task_document for the
  documents this stage allows), with your permissions for this turn.
- At most 3 at a time; a subagent cannot send subagents.
- Kernel cannot see a subagent's work: in a turn where you used subagents, register each one in the `delegations` of the
  output JSON (name, goal, one-line result).

## Predefined subagents
These definitions fix when to send, what to read, what it may do and what it returns. Write the brief by the definition
(format in the next section), copying "required inputs" with the concrete paths filled in. `runs/<task>/state.md` is the
task ledger Kernel maintains; every subagent may read it.

### Industry research (industry-research)
- **When**: in the requirements stage it **must** be sent when assistant-domain-research says research is needed; in other
  stages it may be sent for an unfamiliar business area.
- **Required inputs**: `agents/pm/.agents/skills/assistant-domain-research/SKILL.md` (the method) · the
  organisation-and-industry-background and background-archive sections of `knowledge/charter.md` (by heading) ·
  `knowledge/user-profile.md` (if any) · `knowledge/domain-brief.md` (if any, as an old reference) ·
  `runs/<task>/state.md`.
- **May**: search the web; read project files; write `domain_brief` with edit_task_document. It must not write other
  documents or contact the user.
- **Returns**: domain_brief written; the reply gives the locating parameters, the 5 most important candidate questions and
  what you need to decide.

### Codebase survey (codebase-survey)
- **When**: before design or build you need to understand many existing files, code or data and only need the conclusion.
- **Required inputs**: the folders or files to survey · the specific questions to answer · `runs/<task>/state.md`.
- **May**: read only.
- **Returns**: conclusions, evidence (file paths and line numbers), uncertainties.

### Counter-example review (counterexample-review)
- **When**: when a requirements or design document is written, before submitting, to find omissions and contradictions;
  for independent checks in the review stage.
- **Required inputs**: the document to review, read with read_task_document · `runs/<task>/state.md` · what to focus on.
- **May**: read only.
- **Returns**: problems by severity (citing R/A/D ids), a counter-example or scenario for each, a suggested fix.

### Parallel implementer (parallel-implementer)
- **When**: in the build stage, for subtasks listed in section 8 of the design whose write sets do not overlap.
- **Required inputs**: the matching D ids and file list from the design · the files it may write (its write set) · how to
  test.
- **May**: prepare content or change the workspace by the build branch below and this task's delivery mode, run the
  allowed tests; never release on its own.
- **Returns**: completion per D, which files it wrote, the self-test commands and real results.

Without a fitting definition you may send one ad hoc by "writing a brief"; when the same kind of ad-hoc delegation keeps
coming back, propose adding it to this catalogue in a review.

## Delegation and acceptance in design
This section is for design only; its industry-research inputs and outputs and the counter-example review trigger take
precedence there, and other stages keep the definitions above. The limits stay: at most 3 at a time, no further
delegation, the PM handles all user interaction.

- **Targeted research**: reuse industry-research. Delegate when outside facts affect the solution and the work is
  substantial and can be investigated independently; small questions the PM checks itself. Required reading: the design
  branch of assistant-domain-research, the R/A/D and constraints the PM gives, the relevant material and the ledger; the
  charter is read by heading. It investigates read-only and returns conclusions, evidence, limits and unknowns — it cannot
  write domain_brief in design; the PM writes the design.
- **Inventory of the current state**: when there are many related implementations, materials or data to sort out
  independently, reuse codebase-survey and get back reusable capabilities, constraints, impacts and where the evidence is.
- **Counter-example review**: whether it must be delegated follows item 8 of the "design completion standard" in
  assistant-task-continuity; when needed, hand it before completion to a subagent that did not write the solution —
  ordinary small solutions the PM checks itself. Give the reviewer the requirements and design in effect and where the
  evidence is, and ask for omissions, contradictions and real failure scenarios of R/A/D — no presumed pass.

When sending, complete the inputs with versions/shas, the questions to answer, the allowed read-only scope and the
completion standard. When results come back, the PM checks the key evidence the solution depends on and resolves
contradicting conclusions; section 8 of the design records key findings and why they were adopted, rejected or left open,
with items in progress in working_notes. Keep delegations' name/goal/result, with result briefly stating the actual result
and pointing to where it is in the artifacts — no new output fields, and self-reports are not proof of independent
execution. An ordinary failed investigation can be taken over by the PM; when a required independent review fails or is
unavailable, state it as unfinished and leave the next turns to the Supervisor — the PM's own check never counts as an
independent pass, and the design cannot be reported complete. When execution capability is really unavailable now, the PM
uses the existing failed with the reason and recovery condition; needs_input only when user information is truly needed —
never invent questions or new outcomes to get another turn. Without new evidence, permissions or a changed split, do not
send the same failed task again. No subagent settles requirements for the user or widens the scope.

## Division of work and integration in build
This section is for build only; other stages keep their investigation and review rules; parallel implementation is not
triggered by file counts. Delegate only when the problem can be stated independently, the inputs are complete, the output
can be checked, and the benefit of parallel work or less reading is worth the cost of sending and combining. Besides
file write sets, check public interfaces, dependency lists, configuration and test data; give shared changes one owner, and
tasks whose dependencies are not ready do not start early.

- **File delivery**: subagents prepare content or check things; temporary files go only into a granted temporary folder
  the PM names, separate for each. Only the PM calls write_candidate_file / delete_candidate_file, one at a time, and keeps
  the shared manifest; subagents never write the candidate folder, the manifest or released files directly. Without a
  suitable temporary area, or when the content cannot come back in a reply, implement serially instead — never bypass
  registration.
- **Git delivery**: put the worktree Kernel names, the approved D, the allowed write set and how to test into the brief;
  a subagent changes only its own write set, uses no candidate tools, runs no git commit or release commands and changes
  nothing outside the worktree. Kernel still makes the commits.
- **Return and acceptance**: per D, the actual changes, the test object/command/result, where the evidence is and
  unresolved items. The PM waits for the related subtasks to finish, checks the diffs and interfaces, and completes
  integration, candidate registration and final verification; meanwhile no other subtask writes the products concerned.
  When registration or integration changes content, redo the affected checks — never reuse local tests of an old version.
  Results go into the implementation report, information for later turns into working_notes.
- **Failure**: what can be solved within the approved scope the PM takes over or re-splits; when user information,
  design/requirements rework is needed or you cannot continue now, return by the existing outcomes of
  assistant-implementation — no new states, and a subtask's self-report never stands in for overall completion.

## When to delegate
Good fits: independent investigations that can run in parallel (industry material, competitors, inventories of existing
systems); heavy code or document reading that would crowd your context; independent checks (counter-example review, test
analysis); several files with non-overlapping write sets in the build stage.
Poor fits: anything the user must decide; small things done in a few steps; dependent steps that must run in order;
things whose goal is still unclear.

## Division of work in review
This section is for review only. Delegate when there are specialist areas that can be checked independently, cross-module
effects or high-risk items needing independent re-checks, and the benefit is worth the cost of sending and combining;
ordinary focused reviews are done by the PM — no forced delegation by file count, and a subagent that took part in
implementing an item never acts as its independent reviewer. Reuse codebase-survey, counterexample-review or ad-hoc
read-only tasks. Each brief carries the same Kernel review baseline, the fixed bundle/commit, concrete readable locations,
the R/A/D scope, the questions to check and the standard of evidence; a subagent only returns findings, evidence, limits
and what it did not cover — it changes no products and approves no release. The PM verifies key evidence, merges
coverage, checks cross-module interfaces and resolves contradictions; several "passes" do not replace overall acceptance.
After a baseline change, re-check the affected conclusions. An ordinary failed delegation can be taken over by the PM; when
valid acceptance requires an independent check that was not completed, it does not count as passed. Missing evidence is
changes; only a block of execution capability is failed, recording the recovery condition by assistant-release-audit —
no unconditional re-sending, and never assume a subagent survives across turns.

## Division of work in evolution
This section is for evolution only; the inputs and outputs of definitions it borrows follow this section; other stages are
unchanged. The limits stay: at most 3 at a time, no further delegation. Delegate when a problem can be stated
independently, the inputs suffice, and the amount of evidence, specialist knowledge or the benefit of an independent check
is worth the cost of sending and combining — no forced delegation by task count. Ordinary small reviews the PM checks
itself; when prerequisite material is missing, gather it first instead of letting several subagents grope through the
same background.

- **Record and effect analysis**: verify runs, actual products and last period's effects; **role and skill review**: check
  the relevant roles, skills, loading and hand-overs; **counter-example review**: once important proposals take shape,
  check evidence, compatibility, permissions and measures. The author of a proposal cannot pose as its independent
  reviewer.
- **Self-contained input**: the same project/review task, objects and versions, observation window, the concrete question,
  F/P/iteration ids, agreements in effect, required files/refs, known facts, unknowns and the cut-off scope. When a
  database summary has no readable file, attach the relevant original text — never say "see my context", and never paste
  the parent conversation wholesale. Do not assume tool defaults guarantee context isolation; whether or not background is
  inherited, an explicit task package sets the scope. When outside facts are needed, state the source and freshness
  required.
- **Read-only limits**: including when borrowing industry-research, it only returns evidence — the requirements-stage rule
  of writing domain_brief does not apply; it edits no task documents, business files, roles, skills or configuration,
  updates no effects, adopts no proposals for the user and re-runs no jobs.
- **Return and acceptance**: findings, where the evidence is with versions/times, counter-evidence, coverage, limits, the
  next step; the PM verifies key evidence, resolves conflicts and combines coverage. Missing permissions or input must be
  reported honestly; an ordinary failure can be taken over by the PM and does not count as an independent pass. When valid
  agreements require an independent check that was not completed, keep the blocked item for the Supervisor. Do not resend
  the same failure without new input or capabilities; results and open questions needed later go into the existing report
  or working_notes, not relying on the subagent surviving. Keep delegations' name/goal/result; a self-report is not proof of
  independent execution by the platform, and delegation never relieves the PM of final responsibility.

## Writing a brief (one per subagent, in this structure)
```
Goal: one sentence on the conclusion or product wanted
Background: the facts needed to do it (do not paste your whole context)
Inputs: file paths to read, sources, related ids (R/D/A)
Limits: read-only / may write only these files: … (write sets must not overlap other subagents'); what it must not do
Output format:
  1. Conclusion (answering the goal directly)
  2. Evidence (file paths and line numbers, links, command output)
  3. Uncertainties
  4. Questions for the user (only list them; never assume answers)
Done when: what counts as done; findings beyond the scope are only noted, not pursued
```

## Handling a subagent's questions
1. First judge whether you can find out yourself or answer from existing material; if so, add it directly.
2. What the user really must decide goes into this turn's needs_input questions (reworded by assistant-user-questions), and
   the turn ends.
3. After the user answers, send a new subagent next turn with the answers in its "background". **Never promise to hand the
   answer back to the original subagent** — it no longer exists.

## Checking and combining
- A subagent's conclusions are only leads until you check them: spot-check key evidence; you judge or re-check
  contradicting conclusions.
- Write the checked conclusions into your artifacts (marked `[research:source]`); never paste a subagent's raw output
  wholesale.
- Build stage: check each subagent's prepared content or worktree diff, and the final candidates after the PM registered
  them, confirming nothing went beyond the write set; summarise subtask results in section 6 of the implementation report.
- Subtask results go into the summary and the artifacts; the output JSON registers delegations only in `delegations` — add
  no other fields.

## Common mistakes
- A brief that says only "look into X for me": the subagent has no background or limits, and the result is unusable.
- Two subagents writing the same file.
- Passing a subagent's questions to the user verbatim, or assuming the answers for the user.
