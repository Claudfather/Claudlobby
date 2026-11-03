### Fixed — Keep exact ledger counts out of frequent team summaries

Overview reads now explicitly mark the ledger row count as not counted instead of scanning all history on each refresh. Exact counts remain in the explicit summary diagnostic. Bot, task, permission and freshness facts keep their existing semantics; this removes one measured latency cost without claiming all overview timeouts are resolved.
