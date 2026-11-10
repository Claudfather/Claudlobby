### Added — `task move`: a manager moves open work into another workstream (#1053)

`claudlobby --json task move TASK_ID --workstream ID --reason TEXT --request-id UUID` moves an open task into another open workstream of its fleet and project. It records one system row, `task_moved`, which names the task, the workstream it left, the one it joined, who moved it and why. Only the fleet's manager or a local person moves a task, and only an open one. A move refuses a target that is archived, closed by the manager, or closed because every task in it has ended or moved out. The batch rechecks the target under the plane's write lock, so a close that lands in between refuses the move. A move also refuses a reason whose row would pass the 16,384 bytes the plane stores whole: JSON writes most control characters as six bytes, so a reason within its 4,096 bytes can still be too long. The fleet's manager is granted the command.

Every read takes a task's workstream from its newest move, else from its admission. That covers `task show` and `task list`, each task verb's printout, the workstream registry (`workstream show` and `list`, the brief and the lookup door), admit's check of the workstream it joins, and `workstream close`. A close refuses or withdraws a task moved in, and leaves a task moved out alone.

A workstream whose last open task moves out reads closed, dated by the move: `done` if a task it still holds completed, else `abandoned`. That is how a task's own workstream reads once its task has moved out.

The move row's subject is the fleet itself, so a read seeks the fleet's own subject rows, not its whole system history.
