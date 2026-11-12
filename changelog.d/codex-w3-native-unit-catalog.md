### Fixed

- **Native startup inventories the running user manager search paths.** Scrubbed operator shells and native updater jobs now use the same systemd UnitPath instead of deriving conflicting paths from their XDG environments; unavailable or malformed observations refuse.
