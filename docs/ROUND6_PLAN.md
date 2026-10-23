# Round 6 — English by default, language packs, MIT licence, dashboard (Discord settings)

Backup of the Chinese version before this round: tag `zh-v0.45.12` (pushed 2026-10-23).
Background: [PUBLIC_RELEASE_ASSESSMENT.md](PUBLIC_RELEASE_ASSESSMENT.md) sections 1.2, 3 and 5.3.

## User decisions (2026-10-23)
- D1 Language: **language packs + one language switch**, not rewriting the source per installation.
- D2 **English is the default** for skills, roles, templates, contracts and the interface. Chinese (and Japanese,
  German, Spanish) are languages the user selects; Kernel ships their translations.
- D3 Skills and contracts exist in **one** language (English). The model writes what the user reads in the selected
  language, so skills are not kept in five copies.
- D4 Licence: **MIT**.
- D5 First dashboard page: **Discord settings** only (the user creates the Discord application themselves and enters it);
  the language switch lives on the same page.

## Decisions made while planning (small; reported, change on request)
- S1 Languages: `en` (default), `zh-CN`, `ja`, `de`, `es`. Installation setting in
  `assistant-data/config/kernel-settings.yaml` (`language:`); a project may override it with `language:` in
  `project.yaml`. No file = `en`. The dashboard preselects the browser's language until one is saved (the Kernel runs
  in a container and cannot see the host's OS language).
- S2 Interface text lives in `src/assistant_kernel/i18n/<lang>.yaml`; `i18n.t(key, lang, **values)`, falling back to `en`.
- S3 Headings Kernel reads from documents (background archive, user-profile observations, charter purpose / scope /
  routing, iteration record, project-specific notes for PM / Supervisor) have one key each and a heading per language.
  The contract tells the model the heading in the project's language; the parser accepts every language's heading
  and the old Chinese ones, so existing documents keep working. "None" markers (`无`, `None`, `N/A`, …) likewise.
- S4 Project and shared templates: English in the existing folders; translations as overlays in
  `core/template_i18n/<lang>/…`, used when a project is created in that language. Roles of existing projects are the
  projects' own files and are not touched.
- S5 Dashboard: served by `kernel-api` at `/dashboard` (host `127.0.0.1:18001`, already loopback-only). Every
  `/api/v1` call needs the dashboard token from `assistant-data/config/dashboard-token` (generated on first start,
  mode 600). The bot token is stored in `service-credentials.json` (`services.discord.bot_token`, where this
  installation's bot token already is) and shown only as its last four characters.
- S6 The gateway waits for a guild and a bot token instead of exiting when they are missing (fresh install), and exits
  when they change so supervisord restarts it with the new values — no supervisorctl call from the API.
- S7 This installation keeps Chinese: the deploy writes `language: zh-CN` once if the settings file does not exist.

## Translation review decisions (2026-10-23)
- R1 (H1) The Supervisor's instruction to the PM is written in the user's language: the user sees it on the card.
- R2 (H2) Templates are kept as one copy per language (English original + overlays), all generic, without personal
  information; a further language adds its own copies.
- R3 (M2) Source marks are English in every language ([user] / [inferred] / [suggested] / [research:…]); where anything
  conflicts, English wins.
- R4 (M1, M3, M4, by the reviewer's recommendation) "None" is written in the project's language (Kernel accepts every
  language's word); skills name buttons by what they do, since labels appear in the user's language; the industry-research
  subagent reads the charter's background sections by heading.

## Revision after the translation review (2026-10-23, the user's decision)
English interface, replies in the user's language — this replaces D1, D2, S1, S3, S4, S7, R2 and R3 above:
- V1 The interface is English (cards, buttons, notices, commands, dashboard, Kernel-written project files); the text lives in
  `i18n/en.yaml`. No language setting, no language packs for other languages, no dashboard language switch, no translated
  templates (removed; recoverable from git history if ever needed).
- V2 Models reply in the language the user writes in: the latest message; in turns without a new message the language the
  user mostly writes in; a stated preference wins; English when it cannot be told (`i18n.LANGUAGE_NOTE`).
- V3 The text of documents follows the user's language; every document heading stays in English as the contracts and
  assistant-task-continuity give it, so Kernel parses them for any language; older Chinese headings are still found.
- V4 Source marks are English ([user] / [inferred] / [suggested] / [research:…]); "None" may be written in the user's
  language (Kernel accepts several languages' words); `/cancel confirm` (and the older `确认`).

## Effect on existing projects (needs the user's OK before deploy)
Skill templates get new versions in English. On the first tick after deploy, the template rollout (ROUND5 D24)
replaces the skill copies a project **never edited** with the English versions; edited copies stay as they are and
appear in the weekly review. Kernel's interface becomes English; the agents keep replying in Chinese to a user who writes Chinese.
Charters, roles and documents already written stay Chinese and keep being parsed.

## Steps
| Step | Content | Done when |
|---|---|---|
| R6.1 | MIT `LICENSE`, `license` in pyproject, README (en/zh) licence section, example config without real ids | files in place |
| R6.2 | `core/i18n.py`, language packs, settings file, heading aliases in `skills.section_text`, language directive in the contracts | parser and directive tests pass |
| R6.3 | Dashboard: `/dashboard` page, `/api/v1/settings`, `/api/v1/discord` (inspect token: bot, application id, owner, Message Content intent, invite link, guilds; enable the limited intent; save); gateway wait / restart on change | API tests pass, page works in the browser |
| R6.4 | User-facing Python text through `i18n.t` (gateway cards, buttons, commands, notifications, digest, Kernel-written file headings); confirm words in every language | no user-facing CJK literal left in Python |
| R6.5 | Contracts (`prompts/core`) and model-facing Python text in English; 27 skills translated (version bump + changelog line each); templates in English + overlays | prompts are English; skills load |
| R6.6 | Tests follow the new strings; ARCHITECTURE / README / map.yaml; version 0.46.0; deploy step for S7 | full test suite passes |

## Out of scope this round
Installer/init, Claude engine, more dashboard pages, Postgres in the repository's compose (assessment sections 1.4, 2, 4).

## Revision log
- 2026-10-23 plan written.
- 2026-10-23 status: R6.1–R6.5 written (licence, i18n core, dashboard, Python strings via packs, contracts, 27 skills,
  templates with zh-CN/ja/de/es overlays), version 0.46.0, docs and map updated. Open: test suite — the run before the
  last fixes had 76 failures + 11 errors (tests expecting old Chinese/model-facing strings, plus two real bugs since fixed:
  `key` placeholder collision, one skill's YAML frontmatter); existing core tests now run with installation language
  zh-CN. Still to do: fix remaining test assertions, write `language: zh-CN` for this installation (S7), the user's OK on
  skill rollout to existing projects, commit, deploy.
