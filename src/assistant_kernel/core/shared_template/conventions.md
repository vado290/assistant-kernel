---
name: conventions
description: "Working conventions: code and commit rules, the deployment process, security rules"
metadata:
  version: "1"
  load: {roles: [pm], stages: [design, build, review]}
---

# Shared background · Working conventions

## 1 Language
- Replies, documents and card text for the user are in the language the user writes in; Kernel's own interface, skills
  and code identifiers are in English.

## 2 Code and commits
- A code repository has main as its only integration branch; every change is made on a branch and fast-forwarded into
  main after testing.
- A commit message says what was done and why, citing requirement and design ids.
- Database migrations only add files; a migration that has run is never edited.

## 3 Deployment
- Released files change only through candidate files, validation, the user's approval and the release.
- The user deploys Kernel itself with `deploy.ps1`; agents never deploy.

## 4 Security
- Keys, passwords and tokens are never written into documents, skills, commits or chat messages; name only where they
  are stored.
- Archive data before deleting it; old data is archived, never deleted for good.

## 5 Change log
- first draft (R1)
