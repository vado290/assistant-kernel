## Scheduled jobs and long-running services: jobs.yaml (a format Kernel relies on)
Anything in the project that runs on a schedule or keeps running is declared in the project file `jobs.yaml` — no other
way (no crontab, no background processes of your own). Like every other file it goes "candidate → validation →
approved release": the release makes the declaration eligible for registration, then Kernel registers it and confirms,
and runs it on schedule, supervises the services, retries failures and alerts in the Supervisor channel; the user
lists, pauses, resumes and runs jobs now with `/jobs`. Kernel checks this file during validation. Release,
registration, enabling, a successful run and the business effect are each checked separately; a missing file does not
mean a deletion was approved. When a release result is uncertain, check it first — never blindly publish again.

```yaml
schema_version: runtime-manifest.v1
jobs:
  - id: daily-report                # 2–40 lowercase letters, digits or hyphens; unique in the project
    type: script                    # script | agent | service_restart
    entry: scripts/daily_report.py  # script: a script in the project (.py runs with python3, .sh with bash, others directly)
    args: ["--date", "yesterday"]   # optional
    schedule: {kind: cron, expression: "0 8 * * *"}   # or {kind: interval, anchor: "2026-10-16T00:00:00+08:00", seconds: 3600}
                                                      # or {kind: once, at: "2026-10-20T09:00:00+08:00"}
    timezone: Asia/Shanghai         # optional, default Asia/Shanghai
    timeout_seconds: 600            # 10–21600
    retries: 1                      # 0–5 retries after a failure
    notify: supervisor              # which channel gets failure alerts: supervisor | pm | none
  - id: weekly-insight
    type: agent                     # an agent inside the product: one read-only model turn per run, result posted to a channel
    entry: agents/analyst           # needs agents/analyst/config.yaml and role.md; its skills are those whose load.roles includes analyst
    prompt: Read this week's daily report data and write the three most important business findings and suggestions.
    post_result: pm                 # which channel gets the result: pm | supervisor | none
    schedule: {kind: cron, expression: "0 9 * * 1"}
services:
  - id: price-listener              # a long-running service: supervised by supervisord, restarted when it crashes
    command: python3 scripts/listener.py
    restart_schedule: {kind: cron, expression: "0 4 1 * *"}   # optional: scheduled restart (e.g. monthly)
```

- Scripts run in the project folder with only PATH, HOME, TZ, PROJECT_ID, JOB_ID, PLANNED_AT, RUN_ID, MANUAL_RUN and
  similar environment variables — **no access to Kernel's database or keys**; put the configuration they need in
  project files. A non-zero exit code is a failure. Output goes to logs under `runs/jobs/<id>/`.
- Of missed times only the latest is run; while the previous run is still going, this one is skipped. Scripts must be
  safe to run repeatedly (idempotent).
- Approving the deletion of an item means retiring it; after Kernel syncs, check the receipt that it actually stopped
  or was removed — a failure stays pending for a retry. A changed schedule applies from its next time.
- Kernel registers only a **released** jobs.yaml: editing jobs.yaml in the vault from a shell has no effect (Kernel
  says so in the Supervisor channel and ignores it).
- Code-delivery projects: jobs.yaml lives in the **repository root**, is committed with the code and takes effect when
  the main branch is fast-forwarded; script entries and service commands are relative to the repository root and run
  in the repository's working copy; an agent job's `agents/<name>` stays in the project vault. Before running, check
  that the working copy is a clean released main branch; keep runtime data and output apart from version-controlled
  code — Kernel never resets the user's changes.
