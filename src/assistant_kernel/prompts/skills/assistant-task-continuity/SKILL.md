---
name: assistant-task-continuity
description: The fixed sections and numbering rules of every artifact (charter, requirements, industry brief, design, implementation report, review report, improvement proposal); the PM writes and the Supervisor reads artifacts by them.
metadata:
  version: "2.15.0"
  load: {roles: [pm, supervisor]}
  changelog:
    - "2.15.0 2026-10-23 Written in English, the skills' source language; artifacts are written in the user's language, and headings Kernel parses come from the output requirements (ROUND6, 0.46.0)"
    - "2.14.0 2026-10-23 The ledger gains 'Card questions': every question asked through cards (by the PM and the Supervisor) with the answer or why there is none — the reference for what was asked and answered (0.45.12)"
    - "2.13.0 2026-10-22 The change stream also holds commits outside tasks that Kernel imports (outside, manual, system); look at them too for recent changes (0.45.11)"
    - "2.12.0 2026-10-21 Clarified which artifact holds which folder agreement, stage boundaries and compatibility with old projects, without widening the map's automatic gate"
    - "2.11.0 2026-10-21 Only added the hand-over from release to operation and from review to follow-up tasks, using existing artifacts"
    - "2.10.0 2026-10-21 Only added review inputs, the re-review loop and the release hand-over; other stages keep their formats"
    - "2.9.0 2026-10-20 Added build progress, evidence and submission checks, linking items the design left open to the existing D/A and report; no new artifacts or states"
    - "2.8.0 2026-10-20 Only added the design document's hand-over content and the completion standard shared by PM and Supervisor; other artifacts and stage rules unchanged"
    - "2.7.0 2026-10-20 The background archive is stored in knowledge/background.md; Kernel no longer appends to the charter file (0.45.6)"
    - "2.6.1 2026-10-19 The map can index files of the project folder and outside directories with project: and grant: prefixes (ROUND5 R5.2 follow-up)"
    - "2.6.0 2026-10-18 The project map became one map.yaml index per project (ROUND5 R5.2)"
    - "2.5.0 2026-10-17 Added the project map format and 'changes and history' rules: artifacts are changed in place, reasons go into the iteration record, history is looked up with list_iterations as needed (ROUND4)"
    - "2.4.1 2026-10-16 The runtime manifest is jobs.yaml at the project root (the vault's runtime/ folder holds Kernel's internal data)"
    - "2.4.0 2026-10-16 The design document gains section 9 'how it runs' (ROUND3 S3)"
    - "2.3.0 2026-10-16 The requirements document gains section 11 'user-profile observations'; added the sections of the industry brief (domain_brief) (ROUND3 S2)"
    - "2.2.0 2026-10-16 Added the division between the task ledger and the PM's working notes: sessions may be replaced any time, so hand-over relies only on the ledger, notes and artifacts (ROUND3 S1)"
    - "2.1.0 2026-10-16 Documents became drafts revised in place with edit_task_document instead of full text every time (saves output tokens)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): unified artifact sections and numbering so agents hand over to each other and to artifacts by one structure"
---

# Artifact conventions

## Purpose
Artifacts are the only reliable hand-over between agents: you cannot see another's session, and they cannot see yours. The
next reader can only rely on the artifacts, so each artifact's section names and numbers stay fixed — readers find things
by section, cite by number and check item by item.

**Language**: the text of artifacts is written in the language the user writes in; section headings stay in English
exactly as listed here and in the output requirements (Kernel parses some of them: background archive, user-profile
observations, iteration record, effect review, creation parameters). Section numbers stay as listed.

## General rules
1. **Numbering**: requirement items `R1`, `R2`…; acceptance criteria `A1`…; design points `D1`…; improvement proposals
   `P1`…. A number never changes once it appears; a deleted item keeps its number marked "(deleted: reason)", and new items
   continue the numbering.
2. **References**: cite other artifacts by number, for example "meets R3, A3" — do not restate the text.
3. **Source marks**: end factual statements with their source: `[user]` the user's words or confirmation,
   `[research:source]` what you found, `[inferred]` your inference, `[suggested]` what you propose and the user still has to
   decide. Readers treat unmarked content as unverified.
4. **Revise in place**: when changing an existing document, change only the paragraphs that need it with
   edit_task_document and leave the rest as it is (Kernel always stores the complete document); add a line to the revision
   log at the end saying what changed and why.
5. **When unsure, say so**: put it under open items — never hide it in vague wording.

## Hand-over: ledger and working notes
The PM's session is replaced by a brand-new one when the stage changes or after several turns in a row. A new session cannot
see earlier conversation; it sees only three things:
- **The task ledger** (recorded by Kernel automatically, `runs/<task>/state.md`): each turn's instruction, the user's
  answers and additions, the PM's results and questions, card actions. These are facts and win over your memory. Its
  "Card questions" section lists every question asked through cards in this task (by the PM and the Supervisor alike) with
  the user's answer, or why there is none (a change was asked for instead, the card was replaced, the task ended); it
  decides whether a question was asked and answered.
- **The PM's working notes** (`working_notes`, kept by you): update them with edit_task_document before every turn ends.
  Fixed sections: 1 current understanding (what the user really wants and why) · 2 decided (conclusions and their basis,
  citing ledger turns or artifact numbers) · 3 open questions (not yet asked or answered, and how you plan to ask) ·
  4 assumptions and defaults (adopted before confirmation, marked to be confirmed) · 5 next step (what to do next turn, what
  to read first). Do not copy the words and answers the ledger already holds; write your understanding, judgement and
  plans — until someone else could take over at once.
- **Artifacts**: formal conclusions are those in the artifacts; once a conclusion in the notes is settled, write it into
  the artifact.

The Supervisor reads the notes to follow the PM's thinking, but arranges the next step by the artifacts and the ledger.

## Changes and history
- Every long-lived artifact (charter, map, skill copies, profile, industry brief, jobs.yaml) is **changed in place and holds
  only its current content**; what changed each time and why goes into this task's implementation report's iteration
  record, stored in the project's **change stream** after the release.
- The change stream also holds **commits outside tasks** that Kernel imports (manual edits, other tools, Kernel's own
  maintenance such as template syncs), of type imported, dated by the commit, with the commit message as the reason — they
  were not approved through a task; look at both when judging "what changed recently".
- Revisions of task documents (requirements, design, reports) are stored by Kernel for the approval mechanism; you need not
  look at them normally.
- Judge by the **latest state**; history is only a reference. When you need history, look it up with `list_iterations`,
  only for the question at hand:
  - why an artifact is the way it is: `list_iterations(artifact="skill:<name>")`, `artifact="map"`, `artifact="charter"`;
  - whether something was done before: `list_iterations(query="keywords")`;
  - recent major changes: `list_iterations(major_only=True, days=180)`.
  Do not look through every artifact's history each time.

## Fixed sections of each artifact

### Project charter (`knowledge/charter.md`, long-lived, one per project)
1 Purpose · 2 Business goals and success criteria · 3 Users and usage scenarios · 4 Organisation and industry background ·
5 Scope (in scope / out of scope) · 6 Constraints (budget, time, compliance, technology) · 7 Routing (aliases, typical
requests, requests it does not handle).
The background archive is not in the charter file but in `knowledge/background.md`: when a requirements document is
approved, Kernel appends its section 10 there automatically with date and task; it is given to you every turn with the
charter (shown as the charter's last section, the background archive), and the latest entry wins. Charters of early
projects may still have a change-log section that is no longer updated; reasons for changes go into the iteration record.
Known, long-lasting constraints on folders or delivery locations, data retention requirements and important exceptions may
be noted briefly under constraints; no new required section and no complete file list. Old projects without such a note
are still checked against their configuration, map and actual files — no need to complete the charter first. The detailed
structure is best expressed in the map and existing READMEs or business documents. Update the charter only when this
task's scope needs it and the delivery path supports it; a Git build changes only the worktree and cannot reach beyond it
to change the vault's charter.

### Project map (one `map.yaml` per project: `knowledge/map.yaml` for file delivery; in the repository for code delivery, `docs/map.yaml` by default)
A pure index, one entry per file or folder, paths relative to the map's root (the project folder for file delivery, the
repository for code delivery):
```yaml
schema_version: project-map.v1
entries:
  - {path: product/entries/, kind: directory, purpose: the project's entry scripts (an example path, not the only system entry location)}
  - {path: product/entries/daily_report.py, name: daily report, kind: entry, purpose: totals yesterday's trades and notifies,
     summary: reads data/trades/, writes data/reports/, calls analyst, related: [product/lib/report.py]}
```
- path and purpose are required; kind is one of directory / entry / module / script / config / doc / skill / agent /
  data / test / other.
- Maintained business Python files should have entries; the automatic check covers product/ for file delivery or the
  repository for Git delivery, with the exclusions of the existing check (such as __init__.py, tests, data and generated
  folders). Business scripts and key entry points added or substantially changed in this task outside that scope are
  checked by the build's self-check, the Supervisor and, where there is one, the review; an automatic pass does not mean
  the index is complete, and unrelated historical files are not chased.
- Folder entries describe the structure and the purpose and entry point of data or periodic output, without registering
  each daily report, raw data or cache; output expected but not yet produced is not listed as existing.
- The map indexes the whole project: for code delivery the map is in the repository, and files in the project folder
  (charter, skills, …) are written as `project:knowledge/charter.md`; files in granted outside directories as
  `grant:<directory key>/<path>`.
- The map holds only the current state: when a file changed is in git (`map_lookup` adds it), why it changed in the change
  stream (`list_iterations`).
- When the build adds, deletes, renames files or changes their purpose, update the map at the same time (through candidate
  files for file delivery, directly in the workspace for code delivery), and add an item `type: map` to the iteration
  record's changes. When project files must be maintained and the map is missing, create it within this task's relevant
  scope, still meeting the existing validation requirements; deliveries of task documents only create no map or business
  folders.

#### Handing over folder information
The charter holds long-term agreements, the map the current index, the design this task's plan, the implementation report
the actual delivery, and Kernel's states and receipts the facts of approval, release and runtime; the map is neither a
permission nor proof of success, and folder names never override real configuration and evidence. Expand only what this
goal needs, and add no hurdles to ordinary requirements, quick or document-only tasks.
- The requirements stage keeps its existing collection of current state and constraints, with no up-front folder design;
  design states the necessary locations in its existing file list, release impact, how it runs and rollback sections.
- build checks the actual working root, released targets, runtime inputs/outputs and consumers, and updates the map and
  deliverable documents this task affects; plans not yet released are not written as agreements in force.
- review checks entry points, references, data protection and the approved solution read-only; operation checks real output
  and broken references read-only, and anything needing change goes to a follow-up task.
- Subtask briefs carry the relevant roots, relative paths, read/write scope and where to return results — never assume a
  subagent has read the charter; folder agreements are no reason in themselves to delegate.
- Static deliverables such as code and templates go through the existing release; periodic output of approved jobs is
  produced, checked and kept by the runtime agreements, not through a build each time. Candidates, temporary workspaces and
  Kernel's internal folders are not default homes for persistent business data; reverting code does not revert or delete
  business data automatically.

### Requirements document (task_contract)
1 Background and current state · 2 Goals · 3 Users and scenarios · 4 Scope (in / out) · 5 Requirement items (R, each with
its priority: must / should / could) · 6 Acceptance criteria (A, each naming its R) · 7 Constraints and assumptions ·
8 Resolved contradictions (the contradiction → the user's decision) · 9 Open items · 10 Background archive (organisation and
industry facts collected now and reusable later, each with source and date) · 11 User-profile observations (traits of the
user that matter for collaboration, format in assistant-user-insight; "None" (or the word for it in the user's language) if there are none) · Revision log

### Industry brief (domain_brief, requirements stage, stored as `knowledge/domain-brief.md` when the requirements are approved)
Sections and how to write them in assistant-domain-research: 0 locating parameters and validity · 1 industry brief ·
2 business processes and terms · 3 regulation and compliance · 4 tools and competitors · 5 pain points and opportunities ·
6 differences by size · 7 question bank · 8 candidate requirements · 9 sources

### Design document (solution_design)
1 Overview · 2 Requirement coverage (which D for each R) · 3 Design points (D) · 4 File change list (target path, new /
change / delete) · 5 Test plan (how each A is verified) · 6 Release target and impact · 7 Risks and rollback ·
8 Parallel subtasks · 9 How it runs (when the product runs on a schedule or keeps running: one line per job and service,
fields matching jobs.yaml; "None" otherwise) · Revision log

#### Filling in and checking in the design stage
The following is only for the PM writing the design document and the Supervisor evaluating design results; it adds no
hurdles to requirements, quick, bootstrap or other stages. Keep the nine sections above and the R/A/D numbers, adding
within the existing sections — no separate hand-over file; state why an item does not apply, and scale the length to the
task's complexity.

- Section 1: the requirements version it is based on (the sha Kernel provides), the input check result, the main route and
  important trade-offs.
- Section 2: R → D → A and what this round covers; deferrals or adjustments cite the user's decisions in effect — "stating
  a reason" does not replace approval.
- Section 3: inputs, actors, outputs and receivers of the key business steps, the necessary interfaces, dependency choices
  and evidence, and the basis of important decisions.
- Sections 4–6: the concrete changes and their purposes; for each A the verification method, data or environment, pass
  condition and evidence needed; the deliverable's receiver, how it is used, enabling conditions and impact.
- Section 7: failures and recovery that actually matter; open items state content, impact, who resolves them, when, the
  pass condition and failure handling, telling blockers from items that can go to the build. Items handed to the build that
  must be done link to their D; changes go into section 4, verification into section 5 linked to existing R/A, and this
  section refers to them — never a separate task list nobody accepts. Changing acceptance requirements still goes back to
  the requirements for confirmation.
- Section 8: the goal, inputs, write set, output, receiver and acceptance of build subtasks; design-stage investigations and
  reviews get their own subsection recording where the key evidence is and whether it was adopted, rejected or left open —
  a temporary subagent is never treated as an executor still running in the next stage.
- Section 9: follows the existing agreements when it must run; "None" for document-only and similar tasks.

#### Design completion standard
Only for judging whether design is complete; the PM's self-check and the Supervisor's decision on the next step share this
standard, with no separate approval or state:
1. It matches the requirements currently in effect; goals, scope, hard constraints and important user decisions have no
   unresolved conflicts.
2. Every required requirement of this round has a solution; deferral, cancellation or downgrading has a valid user
   decision; important D trace to requirements or the granted scope.
3. Necessary business steps and hand-overs have owners, and their output is usable by the receivers.
4. The evidence for key dependencies is enough to support the solution, without mixing claims, measurements, assumptions
   and unknowns.
5. Business scope, key route, permission boundaries and acceptance definitions are settled; the builder may handle local
   implementation details within the constraints.
6. Every A has an executable verification method and pass condition; controllable deliverables are told apart from effects
   influenced by outside factors.
7. No unresolved design blockers; items that can go to the build and the related failure handling meet section 7's
   hand-over requirements.
8. When this task adds or substantially changes key cross-system or cross-role hand-overs or key outside dependencies whose
   errors could make a required requirement fail, exceed permissions or be hard to recover, or involves high-impact
   decisions on money, important permissions or irreversible outside actions, a subagent that did not write the solution has
   completed an independent counter-example review and the required changes are handled. Parts reused unchanged whose
   conditions have not changed and whose verification evidence is still valid may cite the original evidence without a new
   review; ordinary small solutions may be self-checked by the PM, and the number of sites or files triggers nothing by
   itself. How to delegate: the design branch of assistant-subagent-orchestration.

### Implementation report (implementation_report)
1 Completion (per D: done / partly done / not done and why) · 2 Candidate files · 3 Self-tests (command or method, real
result) · 4 Deviations from the design and why · 5 Known problems · 6 Subtask results (if delegated) · Iteration record
(YAML, format in the output requirements; stored in the project's change stream with the release approval)

#### Resumed turns and delivery in build
Only for build and later checks of this delivery; it changes no requirements or bootstrap formats and adds no stage to
quick. Use the existing document sections, writing briefly and cross-referencing — no separate progress table:
- working_notes: under "decided" record the current approval basis, the actual workspace, the implementation/verification
  progress of each D and its evidence; under "open questions" and "next step" record blocks, recovery conditions and the
  order of take-over. Tell implemented-but-unverified, verification failed and not implemented apart; check against the
  actual files/diffs — notes never override Kernel's state.
- Implementation report section 1: the requirements/design versions it is based on (for quick the requirements and brief
  solution), completion per D with the related A; section 2: the actual delivery location and the list of additions,
  changes and deletions (changed files for Git); section 3: test object, environment, method, result and where the evidence
  is; sections 4–6: design deviations and how they were decided, unresolved or unverified items, subtask results and the
  PM's integration check. A final bundle/commit not available this turn is not guessed — Kernel's receipt fills it in;
  nothing not yet released or running is written as in force.

#### Build submission standard
Shared by the PM before submitting ready_for_validation and by the Supervisor evaluating build results; these are semantic
checks, adding no approval or database state:
1. The current approval basis is valid and its text readable; the required D of full, or the required work of quick's brief
   solution, is done, and the necessary items the design left open have evidence of being done.
2. The approved scope, the report's list and the actual products match; when files must be delivered but are missing or
   unregistered, it is not a delivery without files; unrelated changes are excluded.
3. The final content is integrated and the A checks this delivery needs are done with re-checkable evidence; checks not run,
   environment blocks and outside effects are listed honestly — required acceptance is never turned into "observe after
   go-live" on the spot. When the requirements in effect explicitly stage acceptance, follow that, without claiming later
   effects are achieved.
4. Configuration, runtime preconditions and the related failure handling are grounded; capabilities the current executor
   does not support are never written as available.
5. The map, report and iteration record match the actual changes, and the notes suffice to continue; Kernel's sealing,
   validation and release results are each stated and do not stand in for each other.

### Review report (review_report)
1 Conclusion (pass / changes) · 2 Item-by-item check (each A: evidence → pass / fail) · 3 Must change · 4 Suggested
changes · 5 Release risks

The following additions apply only to the review of full delivery tasks; no stages or hurdles are added to quick,
bootstrap or evolution. Inputs reuse the requirements/design in effect, the implementation report, Kernel's validation and
the fixed delivery list; unverified items, items the design left open and last round's required issues must also be
findable. Write briefly within the five sections by task size — no separate hand-over file:
- Section 1: the versions reviewed, the actual delivery and the scope of the conclusion; the technical baseline is bound by
  Kernel — never invent a SHA.
- Section 2: evidence for the related R/A/D, pass/fail/unverified; a re-review states the closing evidence of required
  issues and the affected regressions.
- Section 3: required issues with trigger, impact, where the evidence is, and the acceptance condition of the fix or the
  missing evidence.
- Section 4: suggestions that do not block this approval; new wishes are not mixed into must-change.
- Section 5: release preconditions, remaining risks, later observations the requirements in effect agree and their
  receivers; with required conditions of this task still open it cannot be pass.
working_notes keep only evidence references, blocks and the next step for later turns; old reports or notes never override
Kernel's verdict that a baseline is void. The review → Supervisor hand-over is explicit: pass may request the release,
implementation problems go back to build, solution/requirements problems to their stage, capability blocks with their
recovery condition.

### Improvement proposal (improvement_proposal)
0 Overview · 1 Effect review (YAML, format in the output requirements) · 2 Problems found (F: what was observed and the
evidence) · 3 Proposals (P: the F addressed · root cause · concrete change · expected effect · how to measure · effort ·
risk) · 4 Considered but not proposed · 5 For the user to decide (method in assistant-evolution-review)

For evolution and operating hand-overs only: verify the charter, the actual products and the released change entry point
Kernel provides (task, version, document references, receipts). Tell this increment from existing agreements; without a
release source mark it unknown, and never take a file-less delivery or unregistered jobs as all operating duties gone.
Within the existing sections state the baseline of effect comparison, the observation window, the sample and where the
evidence is; for unverified or observed items say what is missing and the condition for the next check. When P go to
follow-up tasks, keep the source review/P id, the version it is based on, the goal, scope, acceptance and measure;
goal/reason/ref may be brief and cite the original artifact. Whoever takes over checks against the source before running
the existing process — an improvement proposal is not approved requirements; information for later turns still goes into
working_notes, with no separate hand-over ledger.

## Common mistakes
- Writing conclusions only in the summary: the summary is a brief for the user; the next agent reads the artifacts.
- Renumbering after changing requirements: it breaks every reference in the design and review.
- Writing inferences as facts: mark them `[inferred]`.
