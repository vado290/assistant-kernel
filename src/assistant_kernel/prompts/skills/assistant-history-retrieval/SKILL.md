---
name: assistant-history-retrieval
description: When you need to verify a past decision, approval, constraint or the user's own words, read this project's recent conversation with list_recent_history and cite where it came from.
metadata:
  version: "1.1.0"
  load: {roles: [pm, supervisor], on_demand: true}
  changelog:
    - "1.1.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "1.0.0 2026-10-16 Rewritten in place for core v6: the tool now always returns the project's latest page of records (both channels, in time order); obsolete paging and scope notes removed"
---

# Looking up past conversation

## When to look
Only in these cases — not as a routine every turn:
- the user mentions "before, last time, as I said, what we decided then";
- a judgement depends on a past decision, constraint or the user's words that are not in the current context or artifacts;
- what is said now may conflict with the artifacts, and the original words must be checked.

## How to look
Call `list_recent_history(lookback_days)`: it returns the latest messages of the project's two channels (oldest first),
looking back 365 days by default. It does no keyword or semantic search; it only shows the latest records in time order —
for anything older, the artifacts are the reference.

## How to use it
- Tell the user's own words, agent replies and system notices apart; on "did the user agree", the user's words and card
  actions decide.
- Give the message time for key conclusions; when nothing is found, say honestly how far back you looked — never fill
  the gap from memory.
- Facts already recorded in the artifacts are quoted from the artifacts first.
