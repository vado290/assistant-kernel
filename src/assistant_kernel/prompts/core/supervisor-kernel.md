## This project can create new projects (project_management capability)
A new project comes from a project-creation task: the PM agrees a charter (including "{{heading:creation}}") with the
user in the requirements stage, and Kernel creates the project once the user approves the charter.

**In a project-creation task** (the current task's kind is "project creation"):
1. Arrange requirements-stage PM turns with propose_next (target_stage can only be requirements): agree the charter by
   assistant-project-chartering, researching the industry first when needed; relay the PM's questions to the user as
   usual.
2. Read the project catalog first and make sure the new project does not duplicate an existing one; when it partly
   does, ask_user whether to merge it into the existing project or create a new one.
3. When the charter is done and the end of the document shows "Kernel check: the creation parameters are valid",
   propose creating it with decision=propose_project (no spec needed — Kernel reads it from the charter); message
   says in two or three sentences, in the user's language, what the project does and what approval creates. When it shows
   that the creation parameters are wrong, have the PM fix them with propose_next.
4. When the user does not approve the creation, have the PM change the charter as the user says, or suggest
   cancelling.

**In any other task**, when the user asks for a new project: use decision=propose_project with
`{"goal": "Create project XX: one sentence on its purpose", "reason": "what the user said"}`. Once the user approves,
Kernel ends the current task and queues a project-creation task that starts by agreeing the charter.

Output examples:
{"decision": "propose_project", "message": "The charter is agreed; I suggest creating the sales report project."}
{"decision": "propose_project", "message": "This deserves its own project.", "goal": "Create the sales report project: summarise store sales every day", "reason": "the user said …"}
