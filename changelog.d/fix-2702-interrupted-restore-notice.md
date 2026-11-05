### Fixed — An update that cannot finish tells the alert chat, and an interrupted stage records why (#2702)

When an update's application stage timed out or could not start, the update stopped in its phase, waiting for `host update continue`, with nothing in its record to say why. Nobody was told either: the timers that would page someone are among the units the update stops. An interrupted stage now records its cause in the update record as `stage_error`, as a refused stage already did, so `host update status` shows it until the same stage succeeds.

The update's own job now sends one notice when a run leaves the update unfinished, whatever the cause: a stage interrupted or refused, or units that came back with a health error. It goes to Telegram through the installation's own alert door, `resolve_alert_target` and `tg-post.sh`, the chat and sender a host job's FLEET ALERT uses. No unit the update stops is on that path. The notice names the request, its phase and the cause, and says to read `host update status`, then run `host update continue` from a shell outside any bot session. A run that finishes sends nothing.

An update job keeps the coordinator it started with. So the update that installs this change runs without it, and every job run after that has it, a `host update continue` of that same update included.
