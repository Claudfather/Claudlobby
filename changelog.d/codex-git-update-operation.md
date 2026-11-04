### Changed — consolidate update recovery and effective configuration foundations

Add a source-independent current-update record and Git cutover primitives. A
pending render and its publication progress now fit within that operation,
without creating another permanent plan or activation journal. The committed
configuration reader is independent of whether every consumer started, and
native quiet checks share one implementation. These are internal foundations;
the common updater and host migration are still being integrated.
