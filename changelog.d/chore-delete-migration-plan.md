### Removed — the migration planner module that nothing imports

`claudlobby/migration_plan.py` built the preview for `host activate` and `migration plan`. Both routes are retired, and no code has imported the module since the Git updater replaced them. The module and `tests/test_migration_plan.py` are gone. The three fixtures other test files took from that test file are now in `tests/release_fixtures.py`.
