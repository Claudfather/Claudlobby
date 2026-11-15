### Fixed — `bot handoff` says to repair a `.claude` redirected while it checks a save, and its hint names only the directories it refuses (#2935)

While the session has no handoff of its own, `bot handoff` checks the save in `.claude/session.md`. When `.claude` or the bot directory was redirected or not the user's own by then (it was changed while `bot handoff` ran), the answer's hint said to read `--check` until it reads `saved`, though `--check` cannot read `saved` until the directory is repaired. It now gives the hint a refused session handoff directory gets: repair the directory first, then read `--check`. One that is redirected before `bot handoff` starts is refused before anything is sent, as before.

That hint also said a directory on the way is refused when it cannot be searched or read. Only one above the session's own directory is: when the session's own directory cannot be searched or read, the readers read `.claude/session.md`. And it said `--check` reads `unknown` (`file_unverified`) until the repair, which is not so while `.claude` is redirected: `--check` is then refused, at its read of the effective configuration. The hint now says both as they are.

### Fixed — a stderr that cannot be written no longer turns an unreadable native-session receipt into a refusal (#2935)

The handoff readers say on stderr when the native-session receipt cannot be read, and then read `.claude/session.md`. With a stderr that could not be written, that note raised instead: `bot handoff` answered `unknown` (`unverified`) with the hint to repair a handoff directory that was fine, `bot handoff BOT --check` read `unknown` (`file_unverified`), and with a closed stderr both ended with no answer. Such a stderr now loses the note and nothing else, in every handoff reader. With no stderr at all (closed when Python started), the note went to stdout, ahead of the JSON answer, and is now dropped.

### Fixed — a refused unlinked report names the reason its fields were refused (#2935)

`fleet reports submit` answered every invalid field with `invalid report fields; inspect command help`. The message now carries the report's own reason, for example `invalid report fields: PR URL and role must occur together` for a `--pr` with no `--pr-role`, as the refusals of the linked report verbs do. A refused report records nothing and sends nothing, as before.
