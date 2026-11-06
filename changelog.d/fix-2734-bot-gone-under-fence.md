### Fixed — a bot whose session dies while an update's fence is up no longer wedges the update (#2734)

A bot the update froze running whose session has since gone, with its unit still active, failed its checkpoint on every pass, and nothing could finish or replace the update. The stop now marks such a bot from the observer's steady-state read, takes no checkpoint and stops it, and the restore starts it as frozen. A session that cannot be read, and every bot on macOS, still refuse as before.
