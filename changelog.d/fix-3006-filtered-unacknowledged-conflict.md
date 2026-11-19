### Fixed — a filtered `--unacknowledged` report read answers a moved read position with `conflict` (#3006)

`fleet reports list --unacknowledged` with `--bot`, `--status`, `--since` or `--workstream` refused its
next page as `invalid_argument` once the viewer's read position had moved between two pages, the answer
a corrupt token or a changed filter gets, where the unfiltered read answers `conflict`. It now answers
`conflict` (exit 4, "report read position advanced after this page") when the read position is the only
difference between the cursor and the read, so a caller knows to start again from the first page. A
changed viewer, filter or release and a damaged token still answer `invalid_argument`.
