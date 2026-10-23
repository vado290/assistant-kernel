---
name: assistant-project-chartering
description: When creating a project, agree and write with the user the project charter (purpose, goals, background, scope, routing) and the creation parameters (id, name, capabilities, delivery mode, role notes); Kernel creates the project once the user approves.
metadata:
  version: "1.3.0"
  load: {roles: [pm, supervisor], task_kinds: [bootstrap], capabilities: [project_management]}
  changelog:
    - "1.3.0 2026-10-23 Written in English, the skills' source language; the charter is written in the user's language with the headings the contract gives (ROUND6, 0.46.0)"
    - "1.2.0 2026-10-21 The charter only collects known long-term locations and retention agreements; no up-front directory design or extra hurdles for creating a project"
    - "1.1.0 2026-10-16 Code delivery (R4a) is live: code projects use git-repo"
    - "1.0.1 2026-10-16 Before code delivery (R4a) went live only vault-files was supported"
    - "1.0.0 2026-10-16 Enabled (R3): projects are created through a task; the charter is this task's requirements document, in the format of the pm-bootstrap contract"
    - "0.1.0 2026-10-16 Draft: enabled with R3 (projects created through a task)"
---

# Writing the project charter

## Purpose
The charter is the "project handbook" the new project reads every turn from now on: it decides how the PM and the
Supervisor understand the business and how Kernel routes requests to it. A charter that is off makes all later work go
off. This task's requirements document is the charter; its sections and the format of the creation parameters are in the
contract (Kernel gives it in the prompt), headings exactly as given there.

## How (with the round-based questioning of assistant-requirements and the reading of the user in assistant-user-insight)
1. **Check for duplicates first**: read the project catalog Kernel provides (each project's purpose, scope and routing).
   When it overlaps an existing project, ask the user in the first round: merge into the existing project, or create a new
   one? Organisation and environment facts already in the shared background are not asked again; the charter covers only
   what is specific to this project.
2. **Industry research**: when the project concerns a specific industry, send an industry-research subagent by
   assistant-domain-research; its result goes into this task's domain_brief (stored as the new project's industry brief
   when the project is created), and its question bank organises your questions.
3. **Collect and confirm**: the project's purpose, business goals and success criteria, users and scenarios, organisation
   and industry background, scope (in scope / out of scope), constraints. When existing repositories or granted
   directories, fixed delivery locations, data retention requirements and important exceptions are already clear, note
   them briefly under constraints with their source; the detailed structure follows the map or the existing project
   documents. Engineering organisation that is not yet clear is left to later design — do not ask the user to choose a
   directory tree first, and do not add a question round for it. The charter's notes do not mean a directory exists or
   access is granted; outside directories still go through the creation parameters and the grant mechanism.
4. **Routing**: aliases, typical requests (3–5, written the way the user would actually say them), requests it does not
   handle. This decides which messages are sent to this project later; keep it distinct from the existing projects'
   routing.
5. **Role notes**: what the PM and the Supervisor must especially watch in this project (a few lines, for example
   "operations involving money need a second confirmation"); empty if there is nothing.
6. **Creation parameters**: suggest a project id (short and memorable), a name, capabilities (ordinary projects enable
   none), the delivery mode (git-repo for code projects, vault-files otherwise) and the sharing level (improvements by
   default); ask the user to confirm the id and name on the card.
7. Submit the charter and check the "Kernel check" result at the end of the document: when it reports an error, fix it and
   submit again.

## The Supervisor in this task
Help the user judge whether a separate project is worth it, and check that the routing does not clash with existing
projects; once the charter passes the Kernel check, ask for approval with propose_project — the card lists the creation
parameters and what approval will create.
