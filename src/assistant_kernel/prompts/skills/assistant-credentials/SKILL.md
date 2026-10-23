---
name: assistant-credentials
description: How to look up, ask the user for, name and use project credentials (logins, API keys) — look before you ask, store them in the project's credential store, never write them into any document.
metadata:
  version: "1.1.0"
  load: {roles: [pm, supervisor], on_demand: true}
  changelog:
    - "1.1.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "1.0.1 2026-10-20 Clarified how credentials reach jobs.yaml jobs; no longer promises automatic environment injection from product.yaml"
    - "1.0.0 2026-10-18 New (ROUND5 R5.2): one credential store per project, runtime/credentials.json; look before you ask"
---

# Project credentials

## Where credentials are
Each project has one credential store (`runtime/credentials.json`, managed by Kernel) that belongs only to it. Each
credential has a key, a value, the service it is for, its purpose, who uses it (used_by), when it was updated and where it
came from.

## Look before you ask
1. When you need to log in to a site or call an outside service, first check with `list_credentials` whether a matching
   key exists (it lists descriptions only, never values).
2. If it does: get the value with `get_credential(key)` and use it.
3. If not: output needs_input this turn and mark that question with `credential` (key, service, purpose), for example
   `{"id": "q1", "text": "Please give the Binance API key and secret (read-only permission is enough)", "credential":
   {"key": "binance_api", "service": "Binance API", "purpose": "read market data and positions"}}` — written in the
   user's language. Such a question goes straight to the user on a card; Kernel stores what the user enters.
4. Ask only for what is really missing; never ask again for what the user already gave.

## Naming
- key uses lowercase letters, digits and underscores, preferably `<service>_<purpose>`, such as `binance_api`,
  `ib_account`, `smtp_report`.
- service names the system clearly (with the address or environment, such as "Binance mainnet"); purpose says what it is
  used for, so it is recognisable at a glance later.

## Rules of use
- A credential's value never goes into requirements, designs, reports, the map, code, logs or any message; documents name
  only the key.
- The product's scripts and services never hard-code values. The current script/service path of jobs.yaml has no
  mechanism that injects credentials from product.yaml; design and build must state and verify the way the executor they
  use actually provides credentials, naming only keys or non-sensitive references. Without a supported path, report the
  capability gap — never treat a configuration declaration as wired up, and never copy credential values into project
  files to get around it.
- When a credential stops working (login fails, access refused), name the key in the report and let the user update it;
  never guess or try other accounts.
- Supervisor: when the user asks which credentials are saved, answer with `list_credentials`; when the user wants one
  deleted, tell them to send `/secrets delete <key>`.
