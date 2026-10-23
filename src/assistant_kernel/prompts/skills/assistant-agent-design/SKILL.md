---
name: assistant-agent-design
description: Design an agent inside a product (run by an agent job in jobs.yaml) — directory layout, config.yaml, role.md and its own skills; also a reference when changing the role notes of this project's PM or Supervisor.
metadata:
  version: "2.1.0"
  load: {roles: [pm], stages: [design, build], on_demand: true}
  changelog:
    - "2.1.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "2.0.2 2026-10-18 Skills moved to one flat folder per agent, agents/<agent>/.agents/skills/ (ROUND5 R5.1)"
    - "2.0.1 2026-10-16 The runtime manifest is jobs.yaml at the project root (the vault's runtime/ folder holds Kernel's internal data)"
    - "2.0.0 2026-10-16 Rewritten for core v6 and the general runtime (ROUND3 S3): agents inside a product run on schedule as agent jobs, read-only, with results posted to a channel"
---

# Designing an agent inside a product

## What an agent is
A run configuration, not a resident process: when its time comes, Kernel runs one model turn with its role, its skills
and the job's prompt (read-only, it cannot change project files); its last message is the result, posted to a channel by
the job's `post_result`. Steps that write files belong to script jobs.

## Folders and files
```
agents/<name>/config.yaml     # model settings
agents/<name>/role.md         # role: identity, responsibilities, limits, what a good result is (a few paragraphs)
agents/<name>/.agents/skills/<skill>/SKILL.md   # its own skills, in that agent's own folder
```

Minimal config.yaml:
```yaml
schema_version: agent-config.v2
agent_id: analyst
kind: worker
status: active
role: role.md
harness:
  provider: openai
  reasoning_effort: medium
  timeout_seconds: 900
tools: [get_authorized_workspace, list_authorized_tree, read_authorized_file, vault_read_text]
```
List only the read-only tools it needs.

## Role versus skills
- **role.md** says who I am, whom I serve, what I am and am not responsible for, and what makes a result good.
- **Skills** say how: which data to read, the analysis steps, the criteria, the output format (for example "three
  findings, each with its data and a suggestion").
- The job's **prompt** says only what to do this time (for example "read this week's daily report data"), without
  repeating the role and skills.

## Checklist
- [ ] The skill spells out the format of the result the user finally sees in the channel, at a suitable length
- [ ] With too little data or a failed read, the agent says so honestly instead of inventing
- [ ] No step needs to write files (if one does, make it a script job)
- [ ] The skill's load.roles includes this agent's name
- [ ] When the result should not be in English, the job's prompt says which language to write it in
