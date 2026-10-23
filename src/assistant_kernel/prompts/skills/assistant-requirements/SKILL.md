---
name: assistant-requirements
description: How the requirements stage works — first learn the user's organisation, industry and the project's current state, then collect and clarify requirements in rounds of multi-question cards, resolve contradictions and add requirements the user had not thought of, and finally submit the requirements document and archive the background.
metadata:
  version: "3.3.0"
  load: {roles: [pm], stages: [requirements]}
  io:
    reads: [charter, user_profile, domain_brief, shared_context, state_ledger, user_answers]
    writes: [task_contract, working_notes]
  changelog:
    - "3.3.0 2026-10-23 Written in English, the skills' source language; the requirements document is written in the user's language with the headings the output requirements give; shared facts are marked with the language's shared marker (ROUND6, 0.46.0)"
    - "3.2.4 2026-10-20 The Supervisor's inferences become this round's confirming questions (0.45.7)"
    - "3.2.3 2026-10-20 The background archive is appended to knowledge/background.md, no longer to the charter file (0.45.6)"
    - "3.2.2 2026-10-19 Charter sections are referred to by heading (section numbers differ between projects); profile observations are archived also when a task ends unapproved (memory improvement, 0.45.5)"
    - "3.2.1 2026-10-17 Added io metadata (which artifacts are read and written) to check broken skill hand-offs (ROUND4)"
    - "3.2.0 2026-10-16 The refining round adds 'runtime requirements' (ROUND3 S3)"
    - "3.1.0 2026-10-16 Decide first whether industry research is needed and send a research subagent; round-1 questions come from the question bank; questions follow the user profile; the requirements document gains 'user-profile observations' (ROUND3 S2)"
    - "3.0.0 2026-10-16 Rewritten in place for core v6 (the old version used retired protocol terms and output fields): users often cannot say clearly what they need and contradict themselves, so locate the background first, ask in concentrated rounds and archive the background for reuse"
---

# Requirements discovery

## Purpose
Users often cannot say what they want, or say contradictory things. The requirements stage pins down the user's real
needs in as few rounds as possible, narrows the scope, removes contradictions, and archives what was learned about the
organisation and industry so later tasks use it directly instead of asking again.

## Inputs and fact priority
From high to low: the user's words and answers in this task → approved artifacts → the project charter
(`knowledge/charter.md`, with its background archive) → the shared background (organisation and user, technical
environment) → your investigation → your inference. When a higher and a lower one conflict, the higher wins, noted in the
document.

## How

### Step 0: look before you ask (costs the user no time)
Read the charter (especially its organisation-and-industry-background and background-archive sections, found by heading in
whatever language the charter is written), the user profile, the industry brief and the shared background Kernel injects,
and the relevant project files. When the instruction comes with "the Supervisor's inference", rewrite each inference as a
confirming question and add it to this round's card — never write it into the requirements directly. Do not ask what is
already known; check what may be outdated (older than six months, or contradicted by the user) with a confirming question:
"The charter records that you are in the XX industry — is that still so?"

**Industry research**: decide by the conditions in assistant-domain-research whether research is needed (no industry brief,
older than 180 days, or this task's industry, region or size differs). When it is, send an industry-research subagent
(defined in assistant-subagent-orchestration) before the first round of questions; it writes its result into this task's
domain_brief, and after checking it you pick the first round's questions from its question bank. Research takes a few
minutes and pays off: well-aimed questions often save a round or two. Simple tasks unrelated to an industry (for example
changing a piece of copy) need no research.

### Round 1: locating (only when background is missing, all in one round)
Find the missing items by this list and combine them into one multi-question card (see assistant-user-questions); with an
industry brief, use its candidate questions and industry terms for the user's region, language and company size, and adjust
the number and wording of questions to the user profile by assistant-user-insight:
- **Organisation**: industry and niche, company size, business model (for example business-to-business or consumer), the
  user's own role and decision rights;
- **Current state**: how it is done now, the biggest pain, why now, who will use it, how often, which systems and data are
  involved;
- **Goal**: what counts as success (measurable where possible), timing, the resources available;
- **Industry constraints**: compliance or regulatory requirements, industry practice and common metrics.

### Round 2: refining
From round 1's answers, ask about the concrete usage scenarios, where the input data comes from, who sees the output, the
frequency, how it connects to existing systems, and exceptions. For things that run on a schedule or continuously (for
example a daily report, continuous monitoring), clarify the **runtime requirements**: how often it runs, by when the result
is needed, who is told about failures, whether missed runs are made up, whether it can run repeatedly; write them into the
requirement items and acceptance criteria.

### Round 3: trade-offs and additions
- **Add requirements the user had not thought of**: from industry practice and similar situations, list 3–6 candidate
  requirements marked `[suggested]` in one question for the user to tick (for example "Which of these are also needed?
  A alerts on anomalies B comparison with history C export to Excel D none").
- **Prioritise**: sort the requirement items into must / should / could and ask the user to confirm.
- **Resolve contradictions**: put inconsistent statements side by side for the user to choose (see
  assistant-user-questions).

Rounds may be merged or skipped: when the information suffices, move on — never ask just to follow the process. At the end
of each round tell the user in two or three sentences of the summary "so far I understand …, still to confirm …".

### When to submit
All of these hold: every R has at least one A; both "in scope" and "out of scope" are stated; no required question is
open; every contradiction found has the user's decision. Otherwise keep asking with needs_input, or put what cannot be
settled under open items with a default assumption for the user to confirm on approval.

## Output
- outcome = completed, document.kind = task_contract, sections strictly by assistant-task-continuity, written in the
  user's language with the headings the output requirements give.
- **The background-archive section (section 10) is required**: the organisation, industry and project facts collected this
  time, each with source and date; mark facts that also hold for other projects (for example the company, the deployment
  environment) as shared by starting the line with the shared marker (`Shared:` in English; `公共：`, `共有：`,
  `Gemeinsam:`, `Compartido:` in the other languages) — kernel's review uses them to propose updates to the shared
  background. Once the requirements document is approved, Kernel appends this section as it is to the project's
  knowledge/background.md (given every turn with the charter), so every later task reads it — write facts that make sense
  outside this task; without new information write "None" (or the word for it in the user's language).
- **The user-profile observations section is required**: traits of the user observed in this task that matter for how we
  work together (in the format of assistant-user-insight); once approved, Kernel appends them to the project's user profile
  (also, marked unapproved, when the task ends before approval); without new observations write "None" (or the word for it in the user's language).
- Investigation work (industry material, competitors, existing systems) can go to subagents in parallel (see
  assistant-subagent-orchestration), but only you ask the user questions.

## Self-check
- [ ] Nothing already in the charter or earlier documents was asked again
- [ ] Each round's questions are focused, with options, recommendations and their purpose
- [ ] Every requirement has a priority and source mark; every A can actually be verified
- [ ] Every contradiction has the user's decision recorded
- [ ] Section 10 holds reusable background facts; the user-profile observations section holds new observations or "None"
- [ ] When industry research was needed it was done, and questions were adjusted to the user's region, language, size and
      profile

## Common mistakes
- Discussing technical solutions right away: the requirements stage answers only what, why and to what extent.
- Taking the user's first sentence as the complete requirement: it is usually just a symptom — ask about the current state
  and the pain.
- Stopping when the user answers "not sure": give a default assumption, put it under open items and keep going.
