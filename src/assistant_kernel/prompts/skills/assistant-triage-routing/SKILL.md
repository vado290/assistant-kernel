---
name: assistant-triage-routing
description: Triage and routing — decide whether a user message is small talk, work for this project, or belongs to another existing project, and accordingly answer directly, start a task or hand it over.
metadata:
  version: "1.1.0"
  load: {roles: [pm, supervisor], purposes: [triage, consult]}
  changelog:
    - "1.1.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "1.0.0 2026-10-16 Enabled (R2), replacing assistant-butler-intake: in production every task was a question; small talk and Q&A should not become delivery tasks"
---

# Triage and routing

## Purpose
The user may say anything to any role. Small talk and questions should not become tasks (tasks are for work that takes
several rounds and has a deliverable); what belongs to another project should go to the right place; new ideas are
discussed before a project is set up.

## Order of judgement
1. **Small talk or a one-off question**: greetings, progress questions, "what can you do", "do you have this
   capability", anything answerable directly or after a quick look → answer directly, no task.
   Be concrete: when files or records need checking, check them with the read-only tools first, then answer.
2. **Work for this project**: the user wants a deliverable (document, code, configuration, plan, report) that fits the
   purpose and scope in this project's charter → start a task. The goal is one sentence saying what is delivered; the
   reason says why the user wants it. When you cannot tell whether the user wants to talk or to get started, answer first
   and ask "shall I start on it?".
3. **Belongs to another existing project** (only when the context has a project catalog): the request matches a project's
   purpose, aliases or typical requests and is not among what it does not handle → hand it over. When the match is
   uncertain, name one or two candidate projects and ask the user instead of forcing a choice; never hand over what this
   project can do itself.
4. **Needs a new project** (kernel only): no project matches and this is new work needing continuing effort → discuss it
   with the user first: purpose, who uses it, whether it is part of an existing project; when the user confirms, start it
   as a task of this project with the goal "New project: …".
5. **A fact for the shared background** (for example "from now on everything deploys to remote host B"): in kernel's
   Supervisor channel propose an update by assistant-shared-context; in other projects, hand it to kernel, saying in the
   handoff note "this is an update to the shared background".

## Writing a handoff note
```
The user said: …
My understanding: the user wants … (one or two sentences)
Known background: facts relevant to this request
Suggested first step: for example, confirm …
```
The other project sees only the handoff note, so make it readable on its own (in the user's language).

## Common mistakes
- Treating "hello", "are you there", "which skills are there now" as tasks.
- Creating a new task when the user only asked about a project's progress: answer it, or hand it to that project.
- Handing over for its own sake: never push to another project what this project can do.
