# Simplified Agent profiles

## Meaning of Agent

In this Kernel, an Agent is only a durable configuration identity used for
audit, version pinning, Sessions, and permission checks. It is not a daemon,
container, Worker, or independently loaded model.

The Control Worker claims a Job and starts the same short-lived `codex exec`
adapter for every turn. Before startup it resolves and freezes:

1. a small base Role;
2. exactly one required `SKILL.md`;
3. MCP Tool definitions and default parameters;
4. filesystem/history/capability Scope and denied paths;
5. model, reasoning effort, timeout, and sandbox mode.

The invocation record stores hashes and version IDs. Updating a Skill changes a
document or its companion scripts; it does not create another service.

## Active layout

```text
H:\AI\assistant-data
├─ butler\                         # global router, not a Project
│  ├─ agent-config.yaml
│  └─ .agents\skills\assistant-butler-intake\SKILL.md
└─ vaults
   ├─ super-assistant\             # Butler/product behavior self-evolution
   └─ kernel\                      # Kernel/Worker/MCP/runtime self-evolution
      ├─ agent-config.yaml         # Project Manager profile
      ├─ .agents\skills\...
      └─ stages
         ├─ requirements\agent-config.yaml
         ├─ implementation\agent-config.yaml
         └─ review\agent-config.yaml
```

`super-assistant` and `kernel` are the two built-in special Projects. Ordinary
Projects are created only from an explicit user request. A Project does not
pre-create extra architecture, testing, or optimization profiles.

## Three-stage responsibility split

- `requirements`: understand the user's original intent, resolve conflicts,
  define scope and acceptance criteria, and include only necessary architecture
  boundaries. It does not modify implementation files.
- `implementation`: make the smallest design and file changes, then execute
  relevant tests. It owns both implementation and implementation-side testing.
- `review`: use an independent logical Session and read-only permissions to
  compare the result with original requirements and actual test evidence.

Optimization and self-evolution reuse the same three profiles. The implementation
runs against a candidate copy and publication still requires explicit acceptance;
there are no separate optimization-analysis or optimization-implementation Agents.

## Skill evolution

The active behavior is the readable project-local `SKILL.md`, optionally with a
small `scripts/` directory for deterministic repeated operations. Responsibilities,
inputs, tool boundaries, output, failure behavior, and evidence requirements must
be written in the Skill text. Each invocation freezes its Skill SHA-256 in the
prompt/tool manifest. Reviewed updates replace the active document while preserving
the previous text in `.agents/skill-history`; no Skill database or resident Skill
service is required.

## 2026-09-09 production simplification

- Active Projects: `super-assistant`, `kernel`.
- Active profiles: 9 total: one Butler plus four profiles per Project.
- `kernel-v2-smoke`, `mvp-smoke`, and `second-project` were marked retired.
- Architecture, testing, and both optimization profile rows were marked retired.
- Historical database rows and version files remain recoverable; they are not
  considered active configuration.
- Pre-change backup: `H:\AI\assistant-data\backups\20260909-105433`.
