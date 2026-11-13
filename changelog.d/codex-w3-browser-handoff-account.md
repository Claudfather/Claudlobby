### Fixed — Hand off browser setup and bind the approved Claude profile

Guided browser setup now drains its foreground owner and exits after retaining
the original Launch, releasing runtime readers before the updater cuts over.
The terminal prints original-operation status and reconnect commands without
repeating setup. Local-project approval can bind an explicit Claude account
directory, keeping provider startup and native session provenance on the same
profile while preserving existing repository and default-account behavior.
