# Claudron v0.9.0 CLI captures

Captured 2026-10-15 from the released v0.9.0 CLI on a private scratch vault
created and captured into by the released v0.6.1 CLI. The format-2 clone has
not run a migration. Commands: `claudron status --json --vault <vault>` (exit 0)
and `claudron doctor --json --vault <vault>` (exit 1). The latter reports the
actual `m003` pending migration and D001. Only absolute scratch-vault and home
paths are replaced with `/example/vault` and `/example/home`; the envelope
shape and findings are unchanged. No production vault or credentials were used.

`doctor-format3.json` is the same v0.9.0 command (exit 0) after the actual
`m003` migration was applied and committed in that private canary. The same two path substitutions apply; the format-3 answer
has no pending migrations or findings.
