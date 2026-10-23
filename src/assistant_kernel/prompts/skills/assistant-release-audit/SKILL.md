---
name: assistant-release-audit
description: Review stage — independently and read-only, check the implementation against the requirements and design in effect and the fixed delivery, and conclude whether the release can be requested.
metadata:
  version: "2.3.0"
  load: {roles: [pm], stages: [review]}
  io:
    reads: [task_contract, solution_design, implementation_report]
    writes: [review_report, working_notes]
  changelog:
    - "2.3.0 2026-10-23 Written in English, the skills' source language; the report is written in the user's language (ROUND6, 0.46.0)"
    - "2.2.0 2026-10-21 Added the review baseline, evidence coverage, re-review and failure exits; kept the existing stage and result protocol"
    - "2.1.0 2026-10-20 Check the final delivery list, items the design left open, integration and test evidence; kept the existing review result protocol"
    - "2.0.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): pm-review.md turned into a skill that checks each acceptance criterion"
---

# Independent review

## How
1. You are an independent reviewer: review starts in a new session and may continue within the stage. Never change the
   reviewed product or write candidate files; write only review_report and update working_notes through the task
   document tools. Never believe something because the implementation report says "done".
2. For each A: find evidence (candidate file content, Kernel's validation result, read-only checks you run yourself),
   judge pass or fail, and write it into section 2 of the report.
3. Check for changes beyond the design's scope and for files from section 4 of the design that were missed.
4. Decide on delegation by the review branch of assistant-subagent-orchestration; you check the evidence, cross-module
   effects and the conclusion.
5. outcome = completed, document.kind = review_report, and set verdict:
   - "pass": every required R/A/D of this task is covered with passing evidence, so the release can be requested — it does
     not mean released or running;
   - "changes": section 3 states what must change (citing the A or D ids).
6. When the problem lies in the design or the requirements themselves, use return_to_design or return_to_requirements.

## Baseline and re-review
First check the current review baseline Kernel provides and why an earlier one became void: the requirements, design and
implementation report in effect, the validation basis and the fixed delivery. Document revision SHAs differ from the
content SHA read_task_document returns — compare by revision_sha256; bundles and commits are not interchangeable either.
Read file snapshots by the fixed list (including deletions) and Git by the complete commit; a check that uses the working
directory first confirms it matches the commit. Cited evidence must be readable and belong to the final delivery; tests
you cannot re-run under read-only limits are judged from existing evidence — never write "not run" as "passed".

Look for omissions from R/A → implementation and evidence, then for overreach from the actual changes → the approved
scope; check higher-risk interfaces, integrations and failure recovery specifically, without one test suite for every
task. A re-review closes last round's required issues one by one and checks the affected regressions; evidence that has
not changed and is still valid may be reused. When an old report has no baseline or the baseline changed, its conclusion
is only a reference; check again and submit review_report with a verdict explicitly — copying the old text is not a
re-review. Missing required evidence or a failed check is changes, with the issue, impact, evidence and how to fix or
supply the evidence; use failed only when a failure of execution capability really stops this turn, stating the recovery
condition for the Supervisor to arrange. Use needs_input only when you truly need the user's information; never add a
verdict or outcome or temporarily lower the acceptance bar.

## Delivery closure
Only in an existing review stage — never add a review to the quick track. Check by the "build submission standard" of
assistant-task-continuity:
- The actual delivery is the candidate bundle or Git commit Kernel bound, and the report's list must match it; a task that
  needs files cannot pass with missing, unregistered or no-files deliveries. A legitimate delivery without files still has
  its document content, delivery location and the requirements in effect verified — the marker passes nothing by itself.
- Check each required D, its A, and what the design handed to the build; checking that files exist is not enough, and items
  left open in section 7 do not bypass the report and validation.
- Tests target the final delivered content; integration or changes after local tests have matching checks. Tell static,
  mocked, rehearsed, real calls and outside effects apart; no configured test command, or a passed Kernel structure check,
  does not mean business acceptance.
- Runtime preconditions, configuration, how credentials are provided and delivery paths fit what the actual executor can
  do; mark what you do not know as unverified, and never invent evidence that something is enabled.
Missing evidence required for this delivery is changes, naming the D/A and how to supply the evidence; effects the
requirements in effect explicitly leave to a later stage are recorded as agreed — never lower the acceptance bar during
review. The review performs no unauthorised outside actions to fill in tests.
