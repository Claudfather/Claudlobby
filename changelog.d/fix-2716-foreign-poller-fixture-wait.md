### Fixed — the harness's session-scoped readiness scenario waits for its foreign poller before the boot (#2716)

The `validate-bot-change.sh` scenario for #1530 starts a production-shaped
poller tree (`claude` → `bun … start` → `bun … server.ts`) and then boots the
bot, to check that another session's live poller is not read as ready. It gave
the tree a fixed 100 polls of 0.05 s to write `bot.pid`, and booted whatever it
found. On CI the tree once took longer: its poller's pid came after the boot's
own subreaper's, so the boot met an empty slot. Three checks failed, and the
check that another session's poller is not read as ready passed with no poller
there to read.

The scenario now waits up to 30 s for `bot.pid` to name a live process. A tree
that exits first is reported at once, and one still starting at 30 s is
reported with the processes it is running. Either way, the precondition and
every check after it in the scenario fail as refused, naming why, instead of
being scored against an empty slot. `tests/test_validate_harness_fixture_wait.py`
runs the scenario's setup, lifted from the harness, with a stand-in tree that
holds the slot late, exits first, or never holds it.
