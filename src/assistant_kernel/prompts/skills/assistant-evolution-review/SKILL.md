---
name: assistant-evolution-review
description: Weekly review (the core method of the operate stage, triggered weekly or by /evolve) — check the effect of the last improvements, find problems and their root causes in the run records and change stream, and propose improvements with expected effects and ways to measure them, closing the loop "improve → record → review".
metadata:
  version: "2.4.0"
  load: {roles: [pm], stages: [evolution]}
  io:
    reads: [digest, change_stream, user_profile, map, charter, skills, template_updates]
    writes: [improvement_proposal]
  changelog:
    - "2.4.0 2026-10-23 Written in English, the skills' source language; the proposal is written in the user's language with the effect-review heading the output requirements give (ROUND6, 0.46.0)"
    - "2.3.0 2026-10-22 Added material paging and backlog coverage, goal-oriented analysis, reviews of the agents' own roles and skills, and the hand-over of duplicate proposals"
    - "2.2.0 2026-10-21 Added the operating baseline, tracking of items under observation, business effect evidence and risk-based delegation; kept the write-back of valid verdicts on cancellation"
    - "2.1.0 2026-10-19 kernel's review material adds other projects' shared fact candidates; those worth adopting are listed for the user to decide (memory improvement, 0.45.5)"
    - "2.0.0 2026-10-17 Rewritten (ROUND4): review the last effects first, then find problems, attribute causes and propose; data from the change stream; subagent roles; fixed output sections"
    - "1.1.0 2026-10-16 The review also covers the user profile, the industry brief and subagent use (ROUND3 S2)"
---

# Weekly review

## Purpose
Make the project better the more it is used, with every improvement traceable: what changed last time, why, whether it
worked — and decide from that what to change now. First judge the gap between the project's goals and the actual results,
then whether the business method, product, data, roles or skills should change — never assume the problem is the wording.

## Inputs (Kernel has put them in the prompt; no need to look for them)
- This week's run record: tasks and results, PM turns per stage of each task, changes / rejections / "not sure" on cards,
  failures and retries, subagents, job statistics;
- the change stream: a bounded record of the last 4 weeks, effect reviews not yet done and earlier major changes; respect
  the limits and cut-off notes in the prompt and query specifically when needed;
- the user profile, the project map, the charter; skills whose templates were updated; broken skill hand-offs (if any); for
  the kernel project also the other projects' operations and the candidates of facts other projects marked as shared that
  the shared background lacks (those worth adopting go under "5 for the user to decide", naming the file and section they
  should go into; once the user says so in the Supervisor channel, the Supervisor proposes the change).
When you need more, query as needed: `list_iterations(artifact=..., query=...)` for the history of an artifact or matter;
read a task's ledger or documents to verify details. **The latest state decides; history is only a reference** — do not
leaf through every artifact's history.

## Material coverage and backlog
- First state in "0 overview" the observation window, objects/versions, material times and this round's scope; summaries
  are for navigation — important conclusions are verified against the original evidence. Current runtime registrations
  and last observations are not live health, and the latest release is only one increment. Read logs, actual products and
  user feedback by risk.
- When cut off, continue with the next_cursor that list_iterations / list_recent_history return, keeping the same filters;
  pending reviews with pending_only=true and oldest_first=true, missing measures with needs_measure=true. Read only the
  pages this round's judgement needs — no forcing the history empty. Consider both older backlog and recent high-risk
  changes; a record under observation whose observation condition is not yet met can be noted and skipped while you look
  at others.
- Give each record included this round a verdict or a reason for deferral, stating what evidence is missing and when /
  under what condition to look again. Items not covered and the condition/cursor to continue go into the report, evidence in
  progress into working_notes; the next round continues from the latest review report but re-checks the real state. A
  cursor only locates — it does not prove nothing changed in between. Missing data is not "normal", not covered is not "no
  review needed"; writing [] while known items are pending must be explained.
- When an old record has a clear improvement goal but no measure, first look up the original basis of its approval; never
  invent historical metrics. Ordinary records without an effect promise are not forced into the backlog. "Cannot judge"
  this round does not close an old record; when material is insufficient, name what is blocked and the recovery condition.

## How
1. **Review the last effects**: for records with an expected effect or measure and no valid verdict yet (including pending
   ones and those beyond the default window), compare with what was agreed: effective / ineffective / pending (not enough
   data yet). For ineffective ones give the likely reason and attribute it again in step 3. State the baseline,
   observation window, sample, evidence and limits; pending is not closed and is re-checked next round. The verdicts go
   into the effect-review section (its heading as the output requirements give it), using the record ids Kernel shows —
   never guess ids; write [] only when nothing needs reviewing. Fix parse/match errors first; they never mean there is
   nothing to review.
2. **Find problems** (numbered F1, F2…): when the project has delivered or actually used products (documents included),
   first go through the operating health checklist of assistant-operation. Take facts only from the records; common signals:
   - many turns in a stage (many back-and-forths in requirements, repeated rework in the build);
   - the user changing, declining, stopping, or answering "not sure";
   - failed or retried runs, failed jobs, service problems;
   - the map or charter not matching reality, broken skill hand-offs;
   - a last improvement judged ineffective.
   Each problem cites its evidence (task, turn, record id). A guess without evidence is not a problem.
3. **Attribute causes**: look for causes against the project's goals, delivery agreements and actual results, and assign
   one category: skill wording · role or charter · artifact structure · outdated map or background · missing tool or
   capability · product defect · changed user needs. Handle problems with the same root cause together. Tell facts,
   assumptions and unknowns apart, and consider the sample, task complexity, business cycles and concurrent changes; many
   turns, rejections or changed metrics do not by themselves prove poor quality or causation. First verify whether a change
   was actually adopted, rolled back or replaced. With insufficient evidence keep it as an assumption with the smallest way
   to verify it; well-evidenced opportunities for improvement may be proposed too.
4. **Propose improvements** (numbered P1, P2…; major impact and irreversible risks first, then frequency, benefit and
   effort; at most 5). Each states: the problem addressed (F id) · root cause · the concrete change (for a skill the new
   wording verbatim, for a file the path and section, for a job the change to jobs.yaml) · expected effect · **how to
   measure** (run statistics and business evidence that can be checked within the grants, with source, definition and
   time) · effort · risk. A successful job or fewer turns does not replace the business goal; mark missing data pending and
   suggest collecting it where needed — never invent metrics or widen grants. Skill change proposals follow
   assistant-capability-maintenance; when "templates were updated", judge whether and what to adopt. For the agents' own
   skills, role.md or configuration, check actual loading and collaboration by that skill's evolution review branch —
   never rewrite your own rules in this round. Before proposing, check related open tasks and earlier proposals; link to
   one already queued or in progress instead of creating a duplicate; re-proposing something rejected needs new evidence or
   changed conditions. Items under observation are not raised again as new faults; at most five key proposals, with the
   reason for deferring other important findings stated — never hidden.
5. **Considered but not proposed**: list them with the reasons, so they are not discussed again next time.
6. When nothing is worth improving, say so honestly — never propose just to have something to show.

## Subagents
Judge cost and benefit by the evolution branch of assistant-subagent-orchestration; no forced delegation by task count.
Possible splits:
- **record analysis**: read this week's task ledgers (`runs/<task>/state.md`) and run records → a list of problems with
  evidence;
- **skill and artifact review**: read the skills, charter and map related to the problems → candidate changes for each
  problem (wording verbatim);
- once the proposals take shape, send a **counter-example review**: look for holes in each (would the change hurt
  elsewhere, can the measure be measured).
The final attribution, trade-offs and conclusions are yours.

## Output
outcome = completed, document.kind = improvement_proposal, written in the user's language, with fixed sections:
0 overview (a few key numbers) · 1 effect review (YAML, under the heading the output requirements give) · 2 problems found
(F) · 3 proposals (P) · 4 considered but not proposed · 5 for the user to decide.
This round writes only improvement_proposal and working_notes — it changes no business files or skill copies and takes no
operating actions. The Supervisor turns adopted P into follow-up tasks with their basis, boundaries and acceptance
conditions, queued once the user approves; when done they enter the change stream for review. Use needs_input only when
you truly need the user's information; when a capability failure stops you, failed with the recovery condition; further
evidence can be gathered in a resumed operate turn. A cancelled review still has Kernel write back valid effect verdicts;
invalid or ambiguous ids never become valid write-backs and stay as items to check, as reported.

## Self-check
- [ ] Every earlier record has a review verdict, or a reason it cannot be judged yet
- [ ] Actual coverage, cut-offs and the entry points for what was not covered are stated; [] or a summary was not taken as
      proof of a complete check
- [ ] New proposals do not duplicate existing tasks, and keep the original measures and behaviour that must stay
- [ ] Every problem has evidence; every proposal has a measurable way to measure it
- [ ] Skill changes give the new wording verbatim, not "improve it a bit"
- [ ] No proposal was made just to fill the list
