### Fixed — `bot handoff` says to repair a handoff directory it refuses (#2927)

When `bot handoff` verifies a save and a directory on the way to the session's own handoff is redirected, not the user's own, or cannot be searched or read, it answers `unknown` (`unverified`) as before, and its hint now says to repair that directory first. `--check` reads `unknown` (`file_unverified`) until the directory is repaired, so the old hint, to read `--check` until it reads `saved`, named a wait with no end.

### Fixed — `bot handoff` records its handoff script's result when its own stderr cannot be written (#2927)

`bot handoff` passes on what its handoff script said on stderr just before it records the script's result. A stderr it could not write to raised there, so the result went unrecorded: with a broken stderr, `bot handoff` answered that the bot's native state could not be established, with the effect read as unattempted after the request had been sent, and with a closed one it ended with no answer at all. Such a stderr now loses those lines and nothing else. With no stderr at all (closed when Python started), the lines went to stdout, ahead of the JSON answer, and are now dropped.

### Fixed — a refused linked report names the reason its fields were refused (#2933)

`assignment progress`, `block`, `return`, `complete` and `fail` answered every invalid field with one message, `invalid linked report fields`, so nothing said which field was wrong or why. The message now carries the report's own reason, for example `invalid linked report fields: PR URL and role must occur together` for a `--pr` with no `--pr-role`. A refused report records nothing, as before.
