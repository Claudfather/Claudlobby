### Fixed — the swap thrash check says when no python3 runs it, and a damaged state no longer stops it (#2856)

The per-minute host probe skipped the swap thrash check without a word when its PATH had no
`python3`. It now says so once on its journal (`memory-thrash: skipped: no python3 on this job's
PATH (…)`), and again after a `python3` that came back is gone again: a marker in
`state/memory-thrash/` keeps it said once. And `memory-thrash.py check` failed without a verdict
on a `state.json` whose parts were not the shape it writes (a reading with no counter, a time
that is a word, a NaN), every minute until the file changed, so the host had no swap thrash
alert and nothing paged it. It now reads such a state as none, says so once on the journal and
starts again from the reading in hand, as on a new boot.
