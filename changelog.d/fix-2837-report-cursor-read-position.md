### Fixed — acknowledging reports no longer voids another report listing's next page (#2837)

`fleet reports list` put the viewer's read position into every cursor it
returned, so when the same viewer ran `fleet reports ack` between two pages of a
`--since`, `--bot`, `--status` or `--workstream` listing, or of an unfiltered
one, the next page was refused as `invalid_argument`. Those listings return the
same reports wherever the read position is. Their cursors now leave it out, and
the next page is the one an unbroken read returns. An `--unacknowledged` read,
which starts at the read position, still keeps it in its cursor and still
refuses a page once it moves.
