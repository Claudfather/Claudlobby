### Fixed — the two-source access-loss test no longer depends on event-loop timing

`test_two_source_node_contract` could fail on a healthy tree. Its access-loss subtest required that no overview ever painted `ok`, but a live peer's accepted partial paints on a real zero-delay timer, and when that timer beat the test's two event-loop turns, a paint made before the loss failed the test. The subtest now sets the order with a fake clock and runs both orders, so a partial retired by the loss fires on every run. No client code changed.
