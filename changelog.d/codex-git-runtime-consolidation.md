### Changed — prepare Git runtime paths and consolidate context-save verification (#2695)

Add explicit source-checkout and stable-launcher paths for the upcoming Git update
flow. Configuration publication now reuses staged bytes and leaves unchanged
outputs in place. Planned restart and activation share request-bound context-save
verification, retaining unresolved requests rather than sending them again.
The existing installation workflow remains in effect until the common updater
and migration path are ready.
