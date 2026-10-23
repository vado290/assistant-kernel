---
name: assistant-source-scope
description: How to find and read files in the project's granted directories — overview first, then only what the task needs; the user and Kernel decide the scope, and an agent never widens it.
metadata:
  version: "4.3.0"
  load: {roles: [pm]}
  changelog:
    - "4.3.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "4.2.0 2026-10-18 map_lookup searches the project map by keyword (ROUND5 R5.2)"
    - "4.1.0 2026-10-17 The map is the map document Kernel provides; reasons for changes are looked up with list_iterations (ROUND4)"
    - "4.0.0 2026-10-16 Rewritten in place for core v6: removed the grant-request tools and context-view flow that no longer exist; kept only the access methods and rules that match the current tools"
---

# Finding files within the grants

## What a grant is
The user grants the project some directories (for example the project's own vault, a code folder). A grant is an upper
limit: every file there now or later may be read, but **being able to read it does not mean you should**. Grants are set by
the user through Kernel; you cannot request or widen them yourself — when a new directory is needed, name its path and
purpose in the summary or a question and let the user decide.

## How to look (coarse to fine)
1. `get_authorized_workspace`: which granted directories (roots) there are, with an overview.
2. `list_authorized_tree(root_key, relative_path, max_depth)`: browse level by level, expanding only what the task needs.
3. `read_authorized_file(uri)`: read only the files you need; for a large file look at the structure and the start first,
   and read it in full once it proves relevant.
4. The project's own files and documents can also be read with `vault_list` and `vault_read_text`. Use the project map
   Kernel gives you (or `map_lookup` by keyword) to locate the relevant components first, then read the actual files; the
   map only guides — the actual files decide, and an outdated map is updated in passing during the build.
   To learn why a file or component is the way it is, look up its reasons with `list_iterations(artifact=...)`.
5. Never scan a whole granted directory recursively from a shell: it is slow and drags unrelated content into the context.

## Rules
- Access and change only the files the current task really needs; report problems unrelated to the task, never fix,
  clean up or refactor them in passing.
- Changes go only through candidate files (in the build stage); even where a directory is technically writable, never
  change released files directly.
- When access may touch sensitive information (keys, accounts, personal data) or a high-impact operation, first state the
  path, the reason and what you intend in a question, and continue only with the user's consent.
- Cite files by path, and by line where useful, so the next reader can check.
