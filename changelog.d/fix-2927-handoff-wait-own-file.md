### Fixed — `bot handoff`'s wait ends only on a save its readers read (#2927)

While the bot's current session has its own handoff file, `bot handoff`'s wait for the save watches that file alone, because `bot handoff BOT --check` and the door's own verification read it alone. A write only to `.claude/session.md` beside it no longer ends the wait early with an `unknown` (`unverified`) answer while `--check` reads `pending`: the wait times out, and `--check` reads `pending` until the session saves its own file. Until the session has a file of its own, the wait watches both files, its own first.
