---
name: assistant-git-delivery
description: Build and review for code delivery — change code, write tests and self-test in this task's own git workspace; Kernel commits automatically, runs the tests on that commit and fast-forwards the main branch after approval.
metadata:
  version: "1.1.0"
  load: {roles: [pm], stages: [build, review], capabilities: [git_delivery]}
  changelog:
    - "1.1.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "1.0.0 2026-10-16 Enabled (R4a): the PM only changes files and never runs git; Kernel commits every turn and binds the commit id"
    - "0.1.0 2026-10-16 Draft: enabled with R4a (code delivery)"
---

# Code delivery

## How it differs from file delivery
This project's real product is a code repository. You do not write candidate files; you change the code directly in the
git workspace Kernel prepared for this task (its own branch, cut from the main branch). Kernel commits at the end of every
turn; when you submit the implementation report, the report is bound to the latest commit and Kernel runs the project's
test command (validation.command in project.yaml) on that commit; once the user approves the release, the main branch is
fast-forwarded to it.

## Build stage
1. First read the README at the repository root, AGENTS.md (if any) and the code this task touches, to learn the structure
   and how tests run; change the files listed in section 4 of the design.
2. Change or add tests together with the behaviour; run the test command (and the relevant single tests) in the workspace
   and write the commands and their real results into section 3 of the implementation report.
3. Work spread over several turns is committed at the end of each turn; just carry on in the next one. When everything is
   done, outcome = ready_for_validation.
4. Never run git commit / merge / push / checkout / reset; never deploy. Database structure changes only add migration
   files, and the implementation report says prominently "includes a database migration".
5. Section 2 of the implementation report, "candidate files", becomes "changed files"; the output of
   `git diff --stat <main branch>` will do.

## Review stage
Read the changes against the requirements and the design (`git diff <main branch>...HEAD`, read-only) together with
Kernel's test results; check that the tests really cover the changes, that no existing test was deleted or skipped, and
that nothing goes beyond the design's scope.

## Common issues
- Without a configured test command Kernel only confirms the commit exists: the review must check the tests by hand and
  suggest configuring validation.command in project.yaml.
- When the main branch got new commits by release time (for example a manual commit by the user), the fast-forward fails;
  tell the user, who decides how to merge.
