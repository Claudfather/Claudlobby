### Changed — Give task results room while keeping messages close

Tasks opens with the existing message composer collapsed into a compact disclosure that shows kept drafts and unconfirmed requests. Selecting a bot, manager, task feedback or nudge reveals the same form. View changes and refreshes preserve drafts, task targets and saved request IDs without sending or checking receipts.

### Added — Approve selected team actions together

Add a local `host owner allow-actions` approval for explicitly selected messages, nudges and feedback, with one action confirmation, atomic grant persistence and unchanged generations for matching grants. Permission decisions retain their separate approval door.

Show the selected team's action permissions and a guarded local approval example in Action access help, with a guide for operators continuing after first-team launch. Older hosts retain their individual local approval doors; checking access grants no authority.

### Fixed — Keep exact ledger counts out of frequent team summaries

Overview reads now explicitly mark the ledger row count as not counted instead of scanning all history on each refresh. Routine summary and health reads also omit full-table counts; exact counts require the native `/api/summary?counts=true` diagnostic. Bot, task, permission and freshness facts keep their existing semantics; this removes one measured latency cost without claiming all overview timeouts are resolved.

### Fixed — Avoid scanning metric history for overview host samples

Overview host-sample discovery now seeks distinct subjects through the existing index instead of walking every retained metric row. Historical subjects and empty results keep their existing meaning; query deadlines and freshness rules are unchanged. This removes one measured overview cost without claiming to resolve every timeout.

### Added — Browse older recorded tasks

Tasks can load older recorded work in bounded pages, preserving each connected host's own continuation and task ownership. Loaded history stays visible during activity updates until refreshed, and incomplete or unsupported source coverage stays explicit.
