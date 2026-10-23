## Code delivery (this project's real product is a git repository)
- In the build stage change code, write tests and run them directly in the **task workspace** below; do not write
  candidate files (write_candidate_file is not available in this project).
- **Do not run git commit / merge / push / checkout / reset**: Kernel commits the workspace's changes to the task branch
  at the end of every turn; when you submit the implementation report (outcome = ready_for_validation), Kernel binds the
  report to the latest commit, and validation runs the project's test command on that commit.
- Change only the files this task needs; nothing outside the workspace. Database structure changes only add new
  migration files, never edit existing ones.
- Once the user approves the release, Kernel fast-forwards the main branch to that commit. Deployment, if any, is
  separate — never deploy yourself.
- In the review stage read the changes in the same workspace (read-only commands such as `git diff` are fine) and
  Kernel's test results; do not change files.
