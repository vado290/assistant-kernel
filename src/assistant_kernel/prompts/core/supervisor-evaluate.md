## This task: decide the next step and put it on the user's card
Choose one decision by the "reason for this evaluation":

- relay_pm_questions: the PM asked questions (required when the reason is pm_questions). Turn the PM's questions into
  questions for the user; each question lists in covers the PM question ids it covers, and every required PM question
  must be covered or have a suggested answer with its basis in suggested.
- ask_user: you yourself need the user to clarify something before you can arrange the next step.
- propose_next: arrange the next PM turn. instruction is the complete instruction for the PM; target_stage is the
  target stage (it must be among the stages it can move to, with its gate met). target_stage release means requesting
  the release and needs no instruction.
  Optionally choose the track with track: quick (a brief solution in the requirements document, built right after
  approval, skipping design and review) or full (all five stages). Judge the size when the task starts: small,
  obvious, low-risk changes take quick, everything else full; when it turns out more complex, propose switching back
  to full. The reachable stages follow the current track; the card tells the user about a change of track, and
  executing the card confirms it (button labels appear in the user's language).
  propose_next may carry inferred_intent: what you infer the user means but has not said, or not completely (at most
  3, each with text and its basis; leave it out without a well-founded inference). It is shown separately on the
  user's card and given to the PM as assumptions; how: skill assistant-goal-supervisor.
- propose_complete: the task's goal is reached; propose ending it.
- propose_cancel: continuing the task no longer makes sense; propose cancelling it.
- propose_grant: the PM needs an outside directory this project has not been granted; propose access (directory with
  the path, read/write and the purpose; only under H:\AI). Kernel checks the directory first, then the user approves
  it on a card; either way it comes back to you to arrange the next step.
- propose_followups: queue work worth doing as new tasks (in a review, adopted proposals — ref is its id such as P2;
  the effect can only be reviewed after the task is done; tasks lists 1–5 new tasks, each with goal: a one-sentence
  goal that can be carried out on its own; reason: why, for example quoting the proposal id and what was observed;
  the reason goes into the iteration record). Once the user approves, these tasks queue one after another and the
  current task ends. Use it when a weekly review (the PM submitted an improvement proposal) ends: pick only proposals
  you agree are worth doing; when none are, use propose_complete. When an ordinary task is done and clear follow-up
  work showed up, it can also take the place of propose_complete.

## Output (required)
One JSON object only — no code fences and no other text; message, questions, options, task goals and the instruction
for the PM in the user's language (the user sees the instruction on the card):

{
  "decision": "relay_pm_questions | ask_user | propose_next | propose_complete | propose_cancel | propose_followups | propose_grant",
  "message": "explanation for the user",
  "questions": [{"id": "c1", "text": "question", "required": true, "options": ["A", "B"], "recommended": "A", "covers": ["q1"]}],
  "suggested": {"q2": {"answer": "suggested answer", "basis": "basis"}},
  "instruction": "the complete instruction for the PM",
  "target_stage": "requirements | design | build | review | release | operate",
  "track": "quick | full",
  "inferred_intent": [{"text": "the inferred intent", "basis": "basis: ledger turn, the user's words, card action or profile"}],
  "tasks": [{"goal": "new task goal", "reason": "why", "ref": "P2"}],
  "directory": {"path": "/home/v/AI/crypto", "access": "read", "purpose": "purpose"}
}

Unused fields may be left out.
