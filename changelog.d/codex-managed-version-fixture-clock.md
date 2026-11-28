### Fixed — Keep the simulated Mac cleanup test deterministic

The fixture advances its clock from the first simulated Darwin refusal so runner delays cannot exhaust the test's confirmation window. The real child-process timeout and reaping tests still use wall time.
