### Fixed — a scheduled job that meets an update's fence skips its tick instead of failing (#2734)

A timer job that fires while a host update has its fence up now logs one skipped line and exits 0, so it starts no failure alert and takes no lock the updater must wait for. A resident unit stays a failure, now exit 75, and a keepalive sweep caught by the fence pauses quietly. Maintenance, a busy lock and an unreadable record still exit 6.
