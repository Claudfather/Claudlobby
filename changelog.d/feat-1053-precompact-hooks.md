### Added — `bot show` names the PreCompact hooks a bot's compose gives it, and `bot.conf` carries the Claudron session loop (#1053)

`claudlobby --json bot show BOT` gains `effective_configuration.precompact`, as do `bot list` and `bot capabilities`:

- `claudron_session_loop`: whether the compose adds Claudron's session-loop hooks, its `hook pre-compact` among them. It is the composer's own test (the loop resolves on and a vault path is set), not a read of the settings file.
- `claudna_precompact_reflect`: clauDNA's PreCompact prompt, as `bot.conf` composes it.
- `composed`: the `PreCompact` commands in the bot's composed `settings.local.json`, each named `claudron` or `other` and identified by SHA-256, never printed. `matches_declaration` is false where the file is older than the declaration, and a file that cannot be read is `absent` or `unreadable`, never an empty list. A plugin's hooks and the account's own settings are not in that file, so they are not listed.

Every bot's `bot.conf` gains `export CLAUDRON_SESSION_LOOP=1` or `=0` beside `CLAUDNA_PRECOMPACT_REFLECT`, from the same test, and the hook merge reads that test too, so the line and the composed hook cannot disagree. Nothing reads the line yet: the compaction executor is to read it before it sends a `/compact`.
