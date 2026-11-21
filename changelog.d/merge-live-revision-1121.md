### Added — overview timeouts can report a bounded active query phase

Backport the request-local overview diagnostic onto this source floor: an existing handled server-budget timeout can carry an allowlisted static phase or statement ID, bounded request and active-phase elapsed times, and a bounded SQLite progress callback count. Closing owner admission still wins. No SQL, parameters, rows or host identifiers are included, and queries, deadlines and requests are unchanged. This backend-only change requires its reviewed target to be loaded; displaying the detail also requires the separately reviewed hosted UI publication.

### Fixed — share overview task candidate selection across teams

Select each team's bounded first task page in one request-local admission-order traversal. Keep canonical task reduction, empty-team orphan audits and public board pagination unchanged, without changing the database schema or query budget.
