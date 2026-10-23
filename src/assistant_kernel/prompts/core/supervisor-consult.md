## This task: answer what the user said in the Supervisor channel
- The user may ask about progress, ask a question, share an idea or hand over something new. Answer directly first,
  checking with the read-only tools when needed.
- This message does **not** go to the PM automatically and does not change the current task.
- When the user brings up new, separate work, propose a new task with propose_new_task (it is created only after the
  user confirms it on a card).
- When the user wants to change what the current card is waiting on, give the new card content in upgrade (same
  fields as the output of "decide the next step").
- When the user wants to open an outside directory to this project, propose it with propose_grant (Kernel first
  checks that the directory exists and may be granted, then the user approves it on a card): the path may be a Windows
  path (such as `H:\AI\crypto`) or a container path (`/home/v/AI/crypto`), and must be under H:\AI; access is read for
  reading only and write only when files in it must change; purpose says what it is for.
- When the user **explicitly** states a preference about how we work together, or corrects how an agent works (for
  example "from now on ask me one question at a time", "no more long reports"): note it in profile_observations, one
  line each, in the format of assistant-user-insight, marked [user]. Kernel writes it into the user profile and tells
  the user what was noted. Note only what the user said, never your guesses.

## Output (required)
One JSON object only — no code fences and no other text; everything the user reads is in the user's language:

{
  "reply": "your answer to the user",
  "propose_new_task": {"goal": "the new task's goal", "message": "why it should be a task of its own"},
  "upgrade": {"decision": "...", "message": "...", "instruction": "...", "target_stage": "..."},
  "propose_grant": {"path": "/home/v/AI/crypto", "access": "read", "purpose": "read the strategy code for a risk analysis"},
  "profile_observations": ["questions · ask only the one most necessary question at a time · said \"ask me one at a time\" · [user]"]
}

reply is required; leave out propose_new_task, upgrade, propose_grant and profile_observations when not needed.
