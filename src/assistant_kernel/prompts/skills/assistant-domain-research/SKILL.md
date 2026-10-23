---
name: assistant-domain-research
description: Industry research (usually done by an industry-research subagent) — collect online, along fixed dimensions, the key facts of the user's industry, region and company size, and write them up as an industry brief, a question bank and a list of candidate requirements to base the requirements questions on.
metadata:
  version: "1.2.0"
  load: {roles: [pm], stages: [requirements, design], on_demand: true}
  io:
    reads: [charter, user_profile, domain_brief]
    writes: [domain_brief]  # this write belongs to requirements; design only returns evidence and the PM writes the design document
  changelog:
    - "1.2.0 2026-10-23 Written in English, the skills' source language; the brief is written in the user's language (ROUND6, 0.46.0)"
    - "1.1.0 2026-10-20 Added a targeted-investigation branch for design only: it returns evidence, the PM writes the design, domain_brief is not edited"
    - "1.0.2 2026-10-19 Charter sections are referred to by heading (section numbers differ between projects' charters) (memory improvement, 0.45.5)"
    - "1.0.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "1.0.0 2026-10-16 First version (ROUND3 S2): research before asking, use fixed dimensions to locate quickly and narrow the requirements; the result is archived with the approved requirements for reuse"
---

# Industry research

## Targeted investigation in the design stage
Use this section only when the current stage is design; it replaces the triggers, steps and writing of the industry
research below; other stages follow the original rules. Starting from the approved requirements and the current design
question, establish the facts that affect whether and how a solution works — for personal, content, research, software and
operating tasks alike. Do not redo a full industry study because there is no brief, and do not force company size or fixed
search dimensions; investigate only what the current decision needs.

- Input: the specific question from the PM, the relevant R/A/D, the constraints in effect, paths to material and the
  permissions available; charter sections are located by heading, not by number.
- Evidence: prefer official documentation of the actual interface or service, existing implementations and applicable
  samples; verify read-only within the current grants when needed. Reuse old conclusions that still apply; re-check
  volatile key facts such as costs, access conditions and interface capabilities — a brief younger than 180 days does not
  replace verification.
- Output: direct conclusions, sources of evidence with dates, scope and limits, what is still unclear and its impact,
  questions the PM or the user must decide. Distinguish what documentation claims, samples and real calls can each prove;
  when you cannot access something or lack permission, say so — never widen grants or make outside changes.
- Hand-over: design research only returns results and edits neither domain_brief nor any other task document; the PM
  checks them and writes them into solution_design, with unfinished items in working_notes. When a conclusion requires
  changing the requirements, the PM proposes return_to_requirements.

## Purpose
Users often name only a symptom. Knowing the common practices, constraints and pain points of the user's industry first
makes the questions hit the point and lets you add requirements the user had not thought of. The research is **the basis
for questions**, not the requirements themselves: the user's words always win.

## When to research
Research when any of these holds; otherwise use the existing industry brief directly, without researching again:
- the project has no `knowledge/domain-brief.md` yet;
- the existing brief is older than 180 days;
- the industry, region, company size or business area of this task differs from the existing brief's locating
  parameters.

## Step 1: settle the locating parameters
Read these parameters from the charter's "Organisation and industry background" and "Background archive" sections (by
heading, in whatever language the charter is written), the user profile and the shared background; assume the most likely
value for missing ones, mark it `[inferred]`, and put it into group 1 of the question bank:

| Parameter | Examples |
|---|---|
| Industry and niche | retail · convenience store chain; manufacturing · automotive parts |
| Region | country, province or city (decides regulation and habits) |
| Language | the language of the user and the end users, industry terms |
| Company size | micro (<10 people) / small (10–99) / medium (100–999) / large (≥1000) |
| Business model | business-to-business / business-to-consumer / internal management |
| User's role | owner, business lead, front-line staff, technical staff |

## Step 2: search along fixed dimensions
Search online, 2–3 sources per dimension, preferring government and regulators, industry associations, listed companies'
announcements and annual reports, authoritative research institutes; then mainstream media and professional communities.
Use only material from the last two years unless it is a long-standing rule. Try keywords in English and in the user's
language. When nothing is found write "not found" — **never invent**.

| Dimension | Questions to answer |
|---|---|
| 1 State and trends | market size and growth, main players, changes in the last two years |
| 2 Typical business processes | core processes of such companies, common roles, key documents and data |
| 3 Metrics and terms | the industry's usual operating metrics and terms (ask in the user's jargon) |
| 4 Regulation and compliance (by region) | applicable laws, licences, data and privacy requirements, recent policy changes |
| 5 Tools and competitors | software and services such companies commonly use, typical practices |
| 6 Common pain points and opportunities | what such companies complain about most, common entry points for automation and digitisation |
| 7 Differences by size | how companies of different sizes differ in practice, budget and staff |
| 8 Language and localisation | terms, formats (date, currency, units), usage habits |

## Step 3: write the industry brief (domain_brief)
Write it with `edit_task_document(kind="domain_brief", content=...)`, in the user's language, with fixed sections:
0 locating parameters and validity (state the research date; valid for 180 days) · 1 industry brief (key points within one
page) · 2 business processes and terms · 3 regulation and compliance · 4 tools and competitors · 5 pain points and
opportunities · 6 differences by size · 7 question bank · 8 candidate requirements · 9 sources

- **7 Question bank**: grouped as "locate → refine → trade-off"; each question with the question, why ask (what it
  affects), 2–5 options, a suggested answer and its basis. Word it for the user's region, language and size; leave out
  questions whose answers are known.
- **8 Candidate requirements**: 3–8 requirements the user may need, marked `[suggested]`, with their basis (which
  dimension and source).
- **9 Sources**: title · URL · published or accessed date · credibility (official / industry / media / community). Facts
  in the text are marked `[research:source number]`.

## Discipline
- The brief is background, not a conclusion: where it conflicts with the user's words the user wins, recorded in the
  requirements document under resolved contradictions.
- Never paste the whole brief to the user; pick the key questions from the question bank and build cards by
  assistant-user-questions.
- When a subagent has finished, the PM spot-checks the key sources to make sure nothing is attributed wrongly before using
  it.
