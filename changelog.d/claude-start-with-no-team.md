### Changed — a computer can be started with no team (#3053)

A first install may now name no team files. Its render lists the computer's own host services and timers. Its committed configuration holds an empty team table, and its identities are `None`, the same value every later update holds while there is no team. The database, owner preparation and native placement are the same as a first install with a team. An ordinary update of such a computer reaches `done` as well. Every other check on the first-install scope stays: exact absolute paths, no duplicates, team files named `fleet.yaml`, a team file on disk still refused when the scope does not name it, and a fresh root. Nothing a person runs changes yet. This follows the workspace-first setup flow approved on 2026-11-21.

### Fixed — default host jobs run clean on a computer with no team (#3053)

`plane expire` reports that there is nothing to sweep and exits zero when the committed configuration lists no team; before, it refused. `host update runtime`, which the daily `claude-update` timer runs, no longer calls the Claude updater on a computer with no team, so it attempts no `npm` install and no longer fails every night where `npm` is absent. The skip sits in `run_host_update` after admission; `update-claude-code.sh` is unchanged. `disk-monitor` no longer stops partway with an unbound-variable error when there is no bots directory under bash 3.2, the bash macOS ships.

### Added — where a start with no team stands (#3053)

`started_without_team(root, installation)` and `start_state(root, installation)` in `claudlobby/plane/owner_source.py` read whether a computer is `fresh`, `starting`, `started` with no team, or `other`. A start counts as unfinished by the update record's own rule: not `done`, or `done` with health errors. Any other unfinished record is an ordinary update, so a started computer stays `started` while one is in flight or after it ends with health errors. Both functions only read.
