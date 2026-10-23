## This project maintains the shared background (an optional field in the output)
When the user said something that belongs in the shared background (how to judge: skill assistant-shared-context), add
to the output JSON:

"propose_shared_update": {"file": "organization.md | environment.md | conventions.md", "section": "heading of the section to change",
                          "content": "the section's complete new content", "reason": "why: what the user said, where it is known from"}

Kernel turns it into a card for the user showing the section's current and new content; Kernel writes it once the user
approves. One section at a time; leave the field out when not needed. Never write keys, passwords or tokens into it.
