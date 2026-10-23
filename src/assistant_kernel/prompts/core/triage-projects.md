## This project can create new projects
When the user wants a new project or something like it (long-running, separate work that needs its own channels and
owner): use decision=start_task and add `"task_kind": "bootstrap"`. That starts a project-creation task: the project
charter is agreed with the user first, and Kernel creates the project once it is approved. goal reads "Create project
XX: one sentence on its purpose" (in the user's language). Check the project catalog first: a request an existing project can
handle needs no new project — route it there.
