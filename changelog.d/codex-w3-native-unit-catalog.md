### Fixed — inventory the running user manager search paths (#2889)

- **Native startup inventories the running user manager search paths.** Scrubbed operator shells and native updater jobs now use the same systemd UnitPath instead of deriving conflicting paths from their XDG environments; unavailable or malformed observations refuse.

### Fixed — verify paired services when restoring new timers (#2889)

New systemd timers verify their declared service alongside the timer during restoration. First-fleet startup can finish this ownership check without losing the paired service from its selected inventory; missing, foreign or mismatched bindings still refuse.
