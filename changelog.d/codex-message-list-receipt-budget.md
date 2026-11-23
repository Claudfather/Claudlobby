### Fixed — a slow Plane ingest daemon delays a message list only once per page

`message list` gives each page half a second to check queued receipts. When
that time runs out, unresolved receipts read `unavailable`. Receipts committed
while the page is being read still show their recorded delivery verdict.
