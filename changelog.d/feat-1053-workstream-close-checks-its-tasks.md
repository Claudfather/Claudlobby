### Changed — a manager's `workstream close` checks the workstream's tasks (#1053)

`workstream close` now refuses while any task in the workstream holds a live assignment (assigned, active or blocked), and names each such task with its state: the manager ends it first. It also refuses, naming it, a task the task reducer cannot resolve, since such a task cannot be told queued from assigned. Nothing is recorded when it refuses.

Otherwise the close withdraws the workstream's queued tasks in its own batch: one `cancelled` row per task, the same row `task withdraw` writes, carrying the close's request and a reason that names the workstream. So a closed workstream never holds an open task. The result's `withdrawn_task_ids` lists them, and the text view names them on one line. A close of a workstream with no open task writes its one row, as before.

The batch rechecks the workstream's open tasks under the plane's write lock, so a task assigned, ended or joined between the close's read and its write refuses the close, and nothing is written. The close also holds each queued task's lock, the one every task verb takes, through its write, so no task verb can act on a task the close is withdrawing.
