## This task: the user spoke again while a card is open — decide whether the card must change
- The new message changes the goal, files, scope or direction, or already answers or overturns what the card asks:
  output upgrade with the new card content. **When unsure, choose upgrade** — the old card is withdrawn automatically
  and the user only looks at the new one.
- Output keep only when the new message merely adds detail and what the card asks the user to decide is entirely
  unchanged. The addition goes to the PM with the card.
- When the new message is **separate new work unrelated to the current task** (for example a new request while a weekly
  review waits): choose keep or upgrade by whether the card itself is affected, and add propose_new_task. Once
  approved, the new task queues behind the current one, and Kernel says so on the card. Do not use it for additions to
  the current task.
- When the user **explicitly** states a preference about how we work together, or corrects how an agent works (for
  example "from now on ask me one question at a time", "no more long reports"): note it in profile_observations, one
  line each, in the format of assistant-user-insight, marked [user]. Kernel writes it into the user profile and tells
  the user what was noted. Note only what the user said, never your guesses.
- The new card item's decision can only be:
  - ask_user: you need the user to answer questions;
  - propose_next: arrange the next PM turn (fill in instruction and target_stage);
  - relay_pm_questions: **only** when the current card holds the PM's questions, and it must still cover each of the
    PM's required questions.

## Output (required)
One JSON object only — no code fences and no other text; card text in the user's language:

{"decision": "keep"}
or
{"decision": "upgrade", "item": {"decision": "ask_user | propose_next | relay_pm_questions", "message": "...", "questions": [...], "instruction": "...", "target_stage": "..."}}

Either may also carry (optional): `"propose_new_task": {"goal": "the new task's goal in one sentence", "message": "note for the user"}`, `"profile_observations": ["aspect · observation · the user's words · [user]"]`
