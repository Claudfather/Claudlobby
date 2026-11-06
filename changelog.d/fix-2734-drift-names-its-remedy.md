### Fixed — a drift refusal says what changed and what clears it, and a hand-stopped timer no longer stops an update (#2734)

A timer or non-bot service that has only gone down since the update was prepared now passes the stop, and the restore brings it back as frozen. Any other drift names the unit, the state saved and the state read, and the command that clears it where one is known to work under the fence. Bot rows stay exact.
