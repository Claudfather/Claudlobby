### Fixed — Avoid scanning metric history for overview host samples

Overview host-sample discovery now seeks distinct subjects through the existing index instead of walking every retained metric row. Historical subjects and empty results keep their existing meaning; query deadlines and freshness rules are unchanged. This removes one measured overview cost without claiming to resolve every timeout.
