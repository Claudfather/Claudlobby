### Fixed — `workstream show` reads a task's own workstream (#2865)

`task admit` opens a workstream for every task, with the ID `ws_` and the task ID's 32 hex digits, and
`workstream list`, the brief and the lookup door show it. `workstream show` refused that ID as
`invalid_argument`, "workstream ID must use ws- followed by a slug". It now reads it, as it reads a
`ws-<slug>` ID, and the refusal of an ID that is neither form names both. `workstream open --id` and the
verbs that change a workstream (`progress`, `renew`, `block`, `unblock` and `close`) still take only
`ws-<slug>`.
