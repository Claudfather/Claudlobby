### Added — reads group by workstream: the brief's open work, and `fleet reports list --workstream ID` (#1053)

The brief's text view shows each open task under its workstream, each group in its oldest task's place, and the tasks in no workstream under a group of their own. In JSON, each `work.items[]` names its `workstream_id`.

`claudlobby --json fleet reports list --workstream ID` lists only the reports that name that workstream, as each report's task stood when the report was made. It combines with `--bot`, `--status`, `--since` and `--unacknowledged`, and each report item now names its `workstream_id`. Like the other filters, it supplies no acknowledgement cursor. An ID the fleet never recorded, and that no report in the read names, is refused with `not_found` (exit 3): an empty page would read as "no reports" for what is most likely a typo.
