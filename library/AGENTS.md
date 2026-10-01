# library/ — placement guidance

Composable sources of truth the compositor assembles into bots. Each category's README states its
format; this file is the boundary at the seam — what belongs here, what must never land here.

**What belongs here.** Composition inputs: `expertise/` (roles), `skills/` (fleet-operations
commands), `mcp/` (wire-config fragments, `${ENV_VAR}` placeholders only), `guardrails/`,
`protocols/` (workflow patterns), `principles/`, `resources/` (non-secret environment facts),
`post_actions/`.

**What must never land here.**

- **The durable knowledge corpus.** "We learned this the hard way" content is Claudron vault
  material — write it through the door (`/claudna:capture`), where it is typed, deduped, and
  recallable fleet-wide. `lessons/` is the legacy corpus and freezes once the vault migration
  lands (boundary plan L3); do not add to it.
- **Engineering-workflow skills** — clauDNA's. Skills here operate the *fleet* (dispatch,
  restart, pulse), never the code the fleet works on.
- **Real tokens, IDs, PII, or fleet-specific paths** — those go in `local/` or `.env` (root
  CLAUDE.md, "The bright line").

**Placement test** (one line): does it *wire or operate* the fleet? → stays. Is it *true about
the world*? → vault. Is it *how to engineer*? → clauDNA. Full algorithm: Claudron repo,
`documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.
