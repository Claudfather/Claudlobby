# Cut over the shared clauDNA root

Operator-run after P3 consumer (6b). The per-bot root and initial seal already
shipped at order 3. This runbook confirms that seal; nothing automatically renames
or deletes the old root. Keep `CLAUDNA_SESSION_SUMMARY` **unset** for these commands:
a bot session then gates `headless`, with no summary spend.

1. Confirm composition is active on every bot: each writes under its own
   `$BOT_DIR/data/claudna`, and no switched bot opens a session under `~/.claudna`.
   Check `list --bot <b> --json --since 1h --root ~/.claudna` against restart times.
   Every bot must have restarted before touching its old sessions. If fleets share
   a bot name, wait until both have switched: `list --bot` matches name/id, not fleet.
2. Once clauDNA 0.28 opens a session on each bot, confirm its
   `<BOT_DIR>/data/claudna/entrypoint.json` exists. Read `E` from any such file:
   `E="$(jq -r .entrypoint <BOT_DIR>/data/claudna/entrypoint.json)"`.
   Use the recorded `python` interpreter too (`P="$(jq -r .python .../entrypoint.json)"`).
   Before 0.28, an operator may locate the plugin cache under that bot's
   `CLAUDE_CONFIG_DIR`: `plugins/cache/Claudfather/claudna/<version>/lib/claudna/session_store`.
   This is a manual fallback, never an export-consumer discovery contract.
3. For each switched bot, recheck all old sessions:
   `"$P" -S "$E" list --bot <b> --json --limit 100000 --include-private --root ~/.claudna`.
   Seal only its returned open session IDs:
   `"$P" -S "$E" seal <sid> --root ~/.claudna`.
   Never seal every open session: seal has no owner-pid check and would close live
   interactive sessions too. Do this after restart, not before a live bot closes.
4. At least an hour after the last restart, sweep dead idle stragglers:
   `CLAUDNA_UNCLOSED_AFTER_H=1 "$P" -S "$E" sweep --root ~/.claudna`.
   Repeat until `closed` is empty (at most five closures each time). Sweep checks
   that the process is gone and idle for the hour; it leaves live sessions alone.
5. Rename only when the service user runs **no interactive clauDNA sessions** using
   the shared default root, or after giving their interactive shell its own
   `CLAUDNA_STATE_DIR` through clauDNA's shell additions and verifying one session:
   `mv ~/.claudna ~/.claudna.retired-<date>`.
   Rename, never `chmod -R a-w`: hooks fail open on an unwritable root and would
   silently swallow later interactive sessions and entrypoint writes.
6. Keep the renamed root until `CLAUDNA_RETAIN_DAYS` (default 30 days) has passed
   since the **last seal**, then remove that specifically named retired directory.
   The compositor and export job never read it. Do not delete an unnamed root.
