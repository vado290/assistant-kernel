---
name: assistant-shared-context
description: Maintain kernel's shared background (organisation and user, technical environment, working conventions) — recognise what is shared and put it on an update card for the user to approve; never write keys.
metadata:
  version: "1.2.0"
  load: {roles: [pm, supervisor], capabilities: [shared_context]}
  changelog:
    - "1.2.0 2026-10-23 Written in English, the skills' source language; the shared marker is recognised in every supported language (ROUND6, 0.46.0)"
    - "1.1.0 2026-10-19 Background facts other projects marked as shared are listed by Kernel as candidates; adopted ones are proposed section by section (memory improvement, 0.45.5)"
    - "1.0.0 2026-10-16 First version: shared facts every agent uses need someone to maintain them, and every change needs the user's confirmation"
---

# Maintaining the shared background

## What the shared background is
Three files under `knowledge/shared/` in kernel's vault, which Kernel injects (read-only) into every project agent's
context as the occasion requires:
- `organization.md` organisation and user: the organisation, industry and niche, business model, the user's own role and
  decision rights, communication preferences;
- `environment.md` technical environment: development machine, Docker containers and path mapping, database, model
  services, Discord, remote hosts and their purposes, deployment targets and methods;
- `conventions.md` working conventions: language, code and commit rules, the deployment process, security rules.

Every agent works by these facts, so one wrong entry affects every project. That is why every change needs the user's
approval.

## What belongs here
- Yes: facts that hold for two or more projects and are fairly stable. For example "finished services deploy to remote
  host B (alias prod-1)".
- No: facts about one project only (they go in that project's charter); temporary states (for example "the server is in
  maintenance today"); routing information (routing comes from each project's charter).
- **Never keys, passwords, tokens or private keys**: describe hosts by alias and purpose, and credentials only by where
  they are stored and what they are called. When a user's message contains such things, do not copy them, and remind the
  user they should not appear in chat.

## Proposing an update (propose_shared_update)
1. Decide which file and section it belongs to; read the current content first to avoid duplicates or conflicts.
2. Write the section's **complete new content** (Kernel replaces the whole section with it), each fact ending with its
   source and the date it was verified, for example "(user 2026-10-16)", "(verified: docker ps 2026-10-16)". Write it in
   the user's language.
3. Say why: what the user said, or where it was found.
4. When it conflicts with existing content, name the old and the new statement and let the user confirm which holds.
5. One section per update; when several need changing, propose them one at a time so the user can confirm each.

## Where the information comes from
- the user tells kernel directly;
- handoff notes routed from other projects;
- facts in other projects' background archives marked as shared (a line starting with `Shared:` — or `公共：`, `共有：`,
  `Gemeinsam:`, `Compartido:` in the other supported languages): Kernel lists those the shared background does not hold
  yet as candidates in kernel's weekly review material and the Supervisor's consultation prompt; when the user says which
  to adopt, propose them section by section with propose_shared_update — never several sections at once;
- technical facts can be verified with the read-only tools (for example looking at containers or folders).

## Regular checks
In reviews, check entries not verified for more than 90 days: verify and re-date those you can verify with tools; list
the others for the user to confirm.
