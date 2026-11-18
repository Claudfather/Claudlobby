# claudlobby/_runtime_scripts/ — placement guidance, authoring rules, script reference

Bash lifecycle scripts: start, supervise, dispatch, report, maintain. This file is the boundary
at the seam, the authoring rules, and where the full reference of each script with a row is.
The root `CLAUDE.md` keeps a one-line index of the scripts, for routing. A script without a row
in that reference documents itself in its header.

**What belongs here.** Host- and fleet-lifecycle mechanics: supervision, timers, dispatch,
reporting, health checks, migrations, setup.

**What must never land here.**

- **Direct vault access.** Knowledge is consumed through the `claudron` CLI door only, in the
  title and path pointers only, never note bodies — the worker reads files
  itself. Resolution comes from the contract env (`CLAUDRON_VAULT_PATH`); no script opens files
  under a vault's note tiers (the set Claudron owns in `VAULT-STRUCTURE.md §Directory contract` —
  point at it, don't re-list it here).
- **Agent-facing behavior.** A procedure a *bot session* should follow is a skill (engineering →
  clauDNA; fleet command → `library/skills/`), not a bash script that prompts it.
- **Assertions about unshipped sibling surfaces.** Scripts guard on what the pinned floor ships
  (`claudron_compat.py`), and degrade fail-open when a sibling is absent.

**Placement test** (one line): operates the *host or fleet* → here; steers an *agent session* →
a skill; touches *knowledge* → through the door. Full algorithm: Claudron repo,
`documentation/decisions/005-claudfather-boundary-separation.md` §10.3.

## Authoring rules

1. Source `lib-common.sh` for shared helpers (OS detection, bot.conf loading, safe mktemp)
2. Always use `set -euo pipefail` — use `|| true` for intentional failures
3. Quote all variables. Use `printf '%s'` instead of `echo` for values that may contain tokens
4. Test on both Linux and macOS where applicable (use `lib-common.sh` OS detection helpers)
5. Never hardcode fleet names, user home dirs, or Homebrew paths — use env vars and detection
6. No apostrophes in comments inside `$( )` — bash 3.2 (macOS `/bin/bash`, the shebang target) does not strip comments while scanning a command substitution, so a stray apostrophe corrupts quoting for the rest of the file (gate: `tests/test_bash_parse.py`, which covers `claudlobby/_runtime_scripts/` and every `library/**/*.sh`)

## Script reference

The table that stood here is `documentation/runtime-script-reference.md` in the Claudlobby
repository, moved there word for word: a row for each script it covers and for each public-CLI
command that replaced an older script. Neither Claude Code nor Codex loads that file on its own,
and an installed runtime does not ship it. Before changing a script, read its row there. Each row
is one line that opens with the name of its script or command, so a grep from the repository
root prints it:

```bash
grep -n '^| `keepalive.sh`' documentation/runtime-script-reference.md
```

When a row grows past its contract, put the history in the script's own header instead: a grep
prints the whole row.
