### Changed — bots no longer stop on the auto-mode setup offer or on clauDNA's capture prompt (#1388, #941)

Every bot's composed `settings.local.json` now carries `skillOverrides` with
`auto-mode-setup` set to `"off"`. Once auto mode had blocked several actions,
Claude Code ended a turn on "Teach auto mode about your environment?", which
nobody at a bot's pane answers; an accepted draft would also have been written
to the `~/.claude/settings.json` that every bot running as that user shares.
`"off"` removes both the offer and the `/auto-mode-setup` command. The new
`skill_overrides:` mapping takes any skill and one of Claude Code's four
states, merged per skill, so a fleet that names one skill keeps the default
for the rest.

Every bot's `bot.conf` now carries `CLAUDNA_PRECOMPACT_REFLECT=0`, so clauDNA's
PreCompact hook no longer refuses every other compaction while it waits for a
`/claudna:capture` that nobody runs. A bot reads `bot.conf` at session start,
so this applies from each bot's next session. `host doctor --switches` lists it
as `claudna-precompact-reflect`.

A fleet turns either back on under `defaults:`, with
`skill_overrides: {auto-mode-setup: "on"}` or `claudna: {precompact_reflect: true}`,
and a bot does the same under its own entry.
