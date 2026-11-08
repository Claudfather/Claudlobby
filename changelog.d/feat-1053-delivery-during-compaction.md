### Added — a delivery to a bot waits for its compaction to end, and presses no extra Enter into it (#1053)

While the host's compaction slot holds a lease naming a bot, a Claude Code compaction is in flight there. Its pane then draws "Compacting conversation…", which the busy check does not match, so until now a compacting bot read as idle.

- Every delivery through the CLI's messaging transport (`message send` and `reply`, `assignment deliver`, `fleet reports submit`, and the notices that assignment reports and task acts send) waits for that lease to end, reading the slot every 2 seconds and for 60 seconds at most, and then sends as to any busy bot. A delivery to any other bot does not wait. One that waited says so in one line on stderr.
- The CLI's repair Enter (the one it presses when a message is still held in the box after the receipt wait) presses nothing while the lease lives; its look reads `compacting`.
- `bot_is_busy` reads such a bot busy, so the briefing trigger, the manager check-in and the bot sweep pass it over as they pass over a bot in a turn.

All three ask the slot with one reader, which calls the slot's own `status()`: the new `lib-common.sh` helper `compaction_lease_left` runs it for `bot_is_busy` and the repair, and the transport runs the same text itself, with no shell. A release without the slot, or a slot that cannot be read, reads as no compaction in flight, so nothing waits. Nothing takes a lease yet: the compaction executor is to take one just before it types `/compact`, so until it does no delivery ever waits.
