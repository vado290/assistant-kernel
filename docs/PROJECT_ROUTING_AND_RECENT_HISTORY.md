# Project routing and recent history

Date: 2026-09-12

Application: Assistant Kernel 0.10.0

Schema: core4.v5 (no database migration)

## Project Card

Each Runtime v2 Project keeps its routing description in `project.yaml`. The optional
`routing` object contains `aliases`, `topics`, `typical_requests`, `exclusions`, and
`related_projects`. `ProjectDirectoryService.list()` combines the registered Project
entry with this file and returns one `project-card.v1` plus a content hash. The LLM
therefore receives the complete routing catalog through one `list_projects` call and
does not read each PM Role.

Project Card matching remains an LLM classification over a small catalog. No vector
table, embedding request, or semantic index is part of this release. The Butler Skill
requires ambiguity clarification and treats exclusions as negative evidence.

## Route resolution

`RouteResolver` is shared by generic API intake and Feishu intake. Its precedence is:

1. a correlated reply;
2. an explicit Project supplied by an authorized intake adapter;
3. the latest durable `(channel, chat_id, user_id)` route binding;
4. file-owned channel binding;
5. `system-butler`.

Feishu `/butler` writes a durable binding back to `system-butler` without starting an
LLM Run. Project handoff continues to write the target binding and enqueue the target
PM Run transactionally.

## Recent history Tool

`list_recent_history` is a read-only per-invocation MCP Tool. The Kernel fixes its
Project from the leased Run; the LLM cannot pass another Project ID. Parameters are:

- `lookback_days`: 1 through 3650, default 365;
- `scope`: `current_conversation` or the fixed current `project`;
- `limit`: 1 through 100, default 50;
- `cursor`: opaque descending-time pagination cursor.

Only a bounded readable projection is returned: IDs, channel/chat, direction, message
type, time, and up to 4000 characters of text. Raw channel payloads are not returned.
System routing records are excluded. The Tool performs no keyword, semantic, or vector
search.

The `assistant-history-retrieval` Skill defines when the LLM should call this Tool,
how to expand from current conversation to Project scope, and how to cite original
message IDs rather than treating summaries as authority.

## Deferred

- Vector or hybrid semantic retrieval.
- Topic/month summaries and a summary index.
- Tool-trace indexing and retention/redaction policy.
- Automatic topic-switch classification against an existing sticky binding.
