### Added — the updater's job records how its run ended (#3081)

Each dispatch of the independent updater's job saves `job_clock` with `pending_at` and drops any earlier `job_outcome`; entering the update adds `entered_at`. A run that has shown it is the job then saves `job_outcome` (`attempt`, `at`, and `result`: `finished`, `unfinished`, or `refused` when it was refused before entering) in one write with its dispatch `finished`. A run refused before entering used to leave its dispatch pending with nothing to say it stopped. The notice that the update did not finish is now sent even when that save fails, and the run's own error is still the one raised.

### Added — `update-status` reports where the update is (#3081)

The `update` object of `host-update-status-v1` adds `stage`, `units`, `runner`, `pending_seconds`, `entered_seconds`, `outcome`, `waiting`, `runtime_busy` and `unhealthy_units`, each read from the update record and the computer's clock alone, never from a look at the native job, and with no update lock. They are codes, counts and seconds: no record text and no unit names beyond `health_errors`. `failed` also covers an `unfinished` or `refused` outcome of the current dispatch before `done`. Every existing field, `update`, `update-continue`, `host update status` and `continue` answer as before.
