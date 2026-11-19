### Docs — the plane's refusal of a live task with no workstream is for a missing or empty id (#2853)

The entry for the refusal said a task whose `workstream_id` is "missing or blank" is refused. The check refuses a `workstream_id` that is missing or empty. A string of spaces is not refused: the contract tests that a task names a workstream, and takes any non-empty string there, as it does for its other workstream fields.
