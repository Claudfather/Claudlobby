### Removed — the five one-shot layout converters behind `claudlobby migration` (#2959)

`claudlobby migration env`, `data`, `cron`, `memory` and `lessons` are removed. Each moved one pre-Claudlobby layout into the current one, once; nothing called them but their own dispatch table and tests, and no guide told an operator to run one. Both first-class hosts were checked before the removal and neither has a legacy bot-fleet directory. Each of the five is now refused as an unknown command: argparse's usage error, exit status 2.

`claudlobby migration status`, `migration workstreams` and `migration workstream-backfill` are unchanged. A failed `migration workstreams` still returns `backup_path`, `diagnostics` and `failure_count` in its result, now always empty, since only the retired converters filled them.

The lessons converter held the only machine record of which note in `library/lessons/` was captured into the vault and which stayed in context. That record is now the **Triage record** table in `library/lessons/README.md`, and `tests/test_lessons_frozen.py` holds the directory to it, as the converter's tests did: a note with no verdict, or a verdict with no note, fails.

Also removed with them: `_migration_preamble` and `_parse_rename_map` in `commands/_helpers.py`, which only the converters used.
