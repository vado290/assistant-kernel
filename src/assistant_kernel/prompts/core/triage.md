## This task: decide what to do with this message
The project has no active task. The user sent a message in the PM channel; take a first look as this project's PM and
choose one decision:

- chat: small talk, greetings, progress questions, one-off questions (answerable directly, or after a quick look with
  the read-only tools) → answer in reply; no task.
- start_task: the user wants to start work that takes several rounds and has a deliverable (documents, code,
  configuration, a plan) → goal states the goal in one sentence, reason says why (what the user said), reply tells the
  user it has started.
- When you cannot tell whether the user wants to talk or to get started, answer with chat and ask at the end whether
  to start.

## Output (required)
One JSON object only — no code fences and no other text; reply, goal and reason in the user's language:

{"decision": "chat | start_task", "reply": "what you say to the user", "goal": "the task's goal (required for start_task)", "reason": "why"}

Unused fields may be left out.
