### Changed — consolidate update recovery and effective configuration foundations

Add a source-independent current-update record and Git cutover primitives. A
pending render and its publication progress now fit within that operation,
without creating another permanent plan or activation journal. The committed
configuration reader is independent of whether every consumer started, and
native quiet checks share one implementation. These are internal foundations;
the common updater and host migration are still being integrated.

### Changed — prepare a first fleet through the same Git updater

Replace the bundle bootstrap and its publishing workflow with an explicit trusted
Git source and hash-locked Python environment. First-fleet preview retains the
same update request that launch continues; concurrent setup cannot replace its
configuration or advance a different request. Retry checks verify the installed
dependencies without silently repairing them. Initial recorder and registry setup
share the update coordinator, and new native consumers use the existing supervision
adapter. The CLI guide and CI installation rehearsal follow this path; browser
first-team integration, existing-host migration and native acceptance remain pending.
