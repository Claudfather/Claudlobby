# claudlobby/ — placement guidance

The Python compositor. The module map lives in the root CLAUDE.md §"Python Package Structure";
this file is the boundary at the seam.

**What belongs here.** Composition and policy code: `fleet.yaml` parsing (owned here — parsed
nowhere else in the stack, by design), generation, validation, permission grants (#644),
status/observability, path resolution, migrations, scaffolding.

**What must never land here.**

- **Knowledge mechanics.** `paths.py` is the only module that may import `claudron.*` (the
  optional `[vault]` extra, composition-time fleet-overlay resolution only). No module reads or
  writes vault note content — bots reach knowledge through the CLI door at runtime.
- **Assertions about sibling surfaces not shipped at the pinned floor.** `claudron_compat.py` is
  the floor SSOT; the validator checks the door that exists (the CLI), never a hypothetical one
  (the parked MCP fragment stays unbuilt per decision C).
- **Skill or agent behavior** — prompts and procedures are composed *from* `library/`, never
  hardcoded in compositor logic.

**Placement test** (one line): does it transform `fleet.yaml` into a running bot, or police what
a bot may do? → here. Behavior → clauDNA / `library/`; knowledge → Claudron. Full algorithm:
Claudron repo, `documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.
