# lib/ — placement guidance

Bash lifecycle scripts: start, supervise, dispatch, report, maintain. Authoring rules (bash 3.2
caveats, quoting, `set -euo pipefail`) live in the root CLAUDE.md §"Adding or modifying lib/
scripts"; this file is the boundary at the seam.

**What belongs here.** Host- and fleet-lifecycle mechanics: supervision, timers, dispatch,
reporting, health checks, migrations, setup.

**What must never land here.**

- **Direct vault access.** Knowledge is consumed through the `claudron` CLI door only, in the
  `dispatch-task.sh` wedge shape: titles + pointers, never note bodies — the worker reads files
  itself. Resolution comes from the contract env (`CLAUDRON_VAULT_PATH`); no script opens files
  under a vault's note tiers (`_shared/`, `projects/`, `<fleet>/shared/`).
- **Agent-facing behavior.** A procedure a *bot session* should follow is a skill (engineering →
  clauDNA; fleet command → `library/skills/`), not a bash script that prompts it.
- **Assertions about unshipped sibling surfaces.** Scripts guard on what the pinned floor ships
  (`claudron_compat.py`), and degrade fail-open when a sibling is absent.

**Placement test** (one line): operates the *host or fleet* → here; steers an *agent session* →
a skill; touches *knowledge* → through the door. Full algorithm: Claudron repo,
`documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.
