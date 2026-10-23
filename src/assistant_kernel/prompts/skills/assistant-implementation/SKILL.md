---
name: assistant-implementation
description: Build stage — take over the approved solution, implement, integrate and verify it in the granted workspace, check the actual deliverables and submit the implementation report; supports file and Git delivery.
metadata:
  version: "2.5.0"
  load: {roles: [pm], stages: [build]}
  io:
    reads: [solution_design, task_contract, working_notes, state_ledger, map]
    writes: [implementation_report, working_notes, map, jobs, charter, skills]
  changelog:
    - "2.5.0 2026-10-23 Written in English, the skills' source language; the report is written in the user's language (ROUND6, 0.46.0)"
    - "2.4.0 2026-10-20 Completed build reception, delivery modes, integration self-tests, resumed turns and failure hand-over; reused the existing states and report"
    - "2.3.0 2026-10-18 The map is map.yaml; every .py of a product is registered (ROUND5 R5.2)"
    - "2.2.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "2.2.0 2026-10-17 Update the map during the build; the report ends with the iteration record (ROUND4)"
    - "2.1.1 2026-10-16 The runtime manifest is jobs.yaml at the project root (the vault's runtime/ folder holds Kernel's internal data)"
    - "2.1.0 2026-10-16 Build delivery includes jobs.yaml and real trial runs of scripts (ROUND3 S3)"
    - "2.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): pm-build.md rewritten, with subtask summaries added"
---

# Build and self-test

## Responsibility and reception
You are responsible for implementing, integrating and evidencing the approved solution — not just writing content from a
file list. On first entry, after a new session, a failed retry or rework, first read the current artifacts, working notes
and Kernel state; make sure the requirements/design text is readable and the approved versions are valid. The full track
builds by the design; the quick track by the "brief solution" in the approved requirements — never add design or review to
quick. Use answers that already exist; when an artifact is referenced but unreadable, report the execution problem — never
guess requirements or ask the user to restate them.

Call get_authorized_workspace and, with the delivery mode, task workspace and map Kernel provides, check the actual files,
differences, dependencies, data and test environment. Notes are not facts on disk: a failed turn may have left changes —
check before continuing when you take over; when the design's basis differs from reality, state the impact instead of
blindly overwriting or redoing. Items section 7 of the design leaves to the build must trace to a D, the file list or the
test plan; missing agreements that affect the build follow the rework rules below.

## Implementation and integration
1. Order the work by the dependencies of the D, and verify first the preconditions that could overturn the route. Decide
   details within the approved scope yourself (function structure, local algorithms, layout), writing down the basis of
   important decisions; never change the business scope, key interfaces, grants or acceptance on your own. A change list
   beyond what was approved first states why it is needed and goes through a design adjustment.
2. **File delivery**: the PM registers creations, updates and deletions one at a time through the candidate tools and never
   changes released files directly; use the baseline sha you read, expected_state="absent" for new files, and when the
   target already changed, check again — never drop the baseline check to force an overwrite.
   **Git delivery**: change only the worktree Kernel names; the candidate tools are unavailable; follow the git-delivery
   contract — Kernel makes the commits.
3. Delegate by the build branch of assistant-subagent-orchestration only for independent work with a clear benefit. The
   PM checks differences, interfaces, configuration, shared dependencies and subtask results; finish integration and
   candidate registration first, then check the final list and verify. During integration and final verification no
   other subtask keeps changing the products concerned.
4. Check that the approved list, the report's list and the real products match; when a required file is missing or
   unregistered, do not submit for validation and do not call it "delivery without files". A legitimate document-only or
   file-less delivery states what was actually delivered and where — an implementation report claiming completion is not
   enough on its own.
5. When files are added, deleted, renamed or change purpose, update the map; test output goes to allowed temporary
   locations; before submitting check for unrelated files and temporary products, without cleaning up the user's existing
   files. Git commits the workspace automatically, so never mix run logs or credentials into the delivery. Kernel may also
   save a workspace commit midway or in a failed turn; that is not an accepted delivery. When unrelated changes cannot be
   excluded safely, report it and hold back submitting for validation, leaving the decision to the user — never claim that
   returning failed prevented the automatic snapshot.

## Self-tests and evidence
- Choose verification that fits the product, by the A in effect and section 5 of the design: running code and tests,
  checking data, rendering documents or checks a person can repeat; not every simple change needs a new unit test, and
  complete headings or exit code zero do not stand in for the business result.
- State the test object, environment, data, method, real result and where the evidence is. File delivery tests the final
  candidate content; when released dependencies are needed, combine them in a granted temporary area — never copy
  candidates back into the released folders to try them. Git tests in the task worktree and records the HEAD/diff tested;
  Kernel produces the final bound commit — never fake a commit that does not exist yet. When content changes after a test,
  redo the affected checks.
- A subtask's local test does not replace an integration check; cover relevant cases such as empty input, failure and
  repeated runs by actual risk, not mechanically. Tell static checks, mocks, rehearsals, real calls and outside effects
  apart; no configured test command does not mean business verification passed.
- Build's execution rights are not authorisation for outside actions; test with test data, read-only or rehearsal modes
  first. When real sending, trading or changes to production data are needed, check the existing authorisation; without
  enough, ask a concrete question — never act on your own. Record environment or permission blocks honestly; never turn
  them into "passed" or temporarily lower an A.

## Resumed turns, failure and submission
Update working_notes every turn, recording progress, evidence, blocks and the next step by the build hand-over rules of
assistant-task-continuity. Use the existing outputs, with no new state fields:
- Progress this turn but the build is not finished: completed, with the summary naming what is still to be done or
  verified; this is not a release request.
- Missing user information or credentials: needs_input with concrete questions; for credentials look before you ask.
- The full track's solution does not hold: return_to_design; the goal, scope or acceptance changes:
  return_to_requirements. When the quick track's brief solution does not hold, go back to requirements too; switching to
  full is proposed by the Supervisor through the existing card mechanism, never switched by you.
- Tools or environment really stop you now: failed, with the reason, the work kept and the recovery condition; without a
  change of conditions do not repeat the same failed attempt.
- Once the "build submission standard" of assistant-task-continuity is met: ready_for_validation,
  document.kind = implementation_report. End the report with the iteration-record YAML (its section heading as the output
  requirements give it), recording the changes, reason, verification and expected effect; the final bundle/commit and
  validation state are Kernel's.

## Products that run
When section 9 of the design has jobs or services:
- write a `jobs.yaml` candidate file (format in the description Kernel gives), matching section 9 item by item;
- run each script for real at least once (test data or a rehearsal option) and put the command and output into the
  implementation report; for agent jobs write agents/<name>/ and its own skills;
- after the release Kernel registers them and runs them on schedule — never write a crontab or start background processes
  yourself.
- First make sure the product has a supported registration and delivery path: candidate writes accept only UTF-8 text, at
  most 2 MiB per file; agent jobs of Git projects still read their configuration from the vault, so a new agent in the
  worktree is not a released vault agent. How credentials are provided follows the current executor (see
  assistant-credentials) — never assume a declared configuration is injected automatically. When a required product has
  no usable delivery or runtime path, report the specific capability gap and rework — never hand-write a manifest, change
  the vault beyond your bounds or swap the deliverable on your own.
