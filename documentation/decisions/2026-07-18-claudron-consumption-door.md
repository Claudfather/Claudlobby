---
title: "Claudron consumption door — clauDNA CLI-skill vs Claudlobby MCP fragment"
type: decision
status: ratified
owner: chris
created: 2026-07-18
tags: [claudron, ecosystem, mcp, clauna, decision, play:ecosystem]
links: ["#509", "#511", "#512", "#513", "#560", "#561", "Claudfather/Claudron#17", "Claudfather/Claudron#60"]
supersedes_scope: "reshapes P2/P3/P4 of documentation/plans/2026-07-07-claudron-consumption.md"
---

# Claudron consumption door — decision

## Junction

How do Claudlobby fleet bots consume the Claudron knowledge hub for **query-before / write-after**?

- **A — Claudlobby MCP fragment.** Author `library/mcp/claudron.json`, mount Claudron's E3 MCP server, cut protocols over to `mcp__claudron__*` tools. (The original epic P2/P3.)
- **B — clauDNA skill door + Claudlobby wiring.** Bots consume via clauDNA's already-shipped `/claudna:claudron` (lookup/status), `/claudna:recall`, `/claudna:capture` over the `claudron` CLI; Claudlobby owns only the *wiring* — vault provisioning, `CLAUDRON_VAULT_PATH`, the structure contract (#560–564), the shipped interim CLI wedge (1e/#528), and a thin navigate-vs-query protocol (#561).
- **C — Hybrid.** Ship B as the default door now; formally **defer** the MCP fragment as a demand-gated option (adopt only when Claudron E3 ships **and** the fleet wants per-tool permission scoping of vault access).

## Context (why this came up now)

- **Claudron E3 (the MCP server A needs) has not shipped and has no active work** — Claudfather/Claudron#17 is still a plan; their open PRs are a vault scanner and PyPI CI. A is gated on a server nobody is building.
- **clauDNA already ships the door, CLI-based.** `skills/claudron/SKILL.md` states it plainly: *"clauDNA ships no MCP servers — this engine IS the CLI. If Claudron's MCP tools are configured, they are the same engine with equivalent semantics; the CLI is the contract floor."* `claudna@Claudfather` is installed on every bot by default, so the door exists fleet-wide today over `claudron>=0.2`.
- **The epic was already drifting toward B.** Its own newer issue #561 frames bot↔hub as *"navigate the filesystem for config, query `recall`/`lookup` for knowledge"* — the CLI/skill verbs, not MCP tools. P3's step 3f already worried about clauDNA overlap.

## Comparison matrix (7 dimensions)

| Dimension | A — MCP fragment | B — clauDNA skill door | C — Hybrid (B now, A deferred) |
|---|---|---|---|
| Elegance | Low — many parts (fragment, pipx install, `check_claudron`, cold-start budgets) for a door that already exists; blocked on an unshipped server | High — door exists on every bot; Claudlobby collapses to wiring + a pointer | High — lean default, one labeled "someday" note |
| Existing Patterns | Moderate — rides `library/mcp/` machinery, but claudron's Python console script doesn't fit the npx `_global_binary`/warm-cache path → net-new install plumbing | High — clauDNA-skills-on-every-bot is *the* default pattern; #561 is the natural shape | High — same as B |
| Extension | Low — new abstractions (`apply_ecosystem_defaults`, `check_claudron`, pipx path, `doctor --json`) | High — extends clauDNA skills + the 1e wedge + vault wiring already in flight | High — B's extension, deferred hook only |
| DRY | Poor — a second door to the same engine clauDNA already doors ("the same engine") | Excellent — one engine, one door; 1e + `/recall` + `/capture` cover it | Excellent — while the fragment stays off-by-default and labeled "same engine, optional" |
| Separation of Concerns | Mixed — puts a knowledge-access surface in the orchestration layer; frictions "Claudlobby does not define skills" | Excellent — clauDNA owns the door, Claudron owns the engine/corpus, Claudlobby owns wiring | Excellent — same as B |
| Future-Proofing | Moderate-low — binds Claudlobby to Claudron's MCP tool surface (rename-costly); gated on E3 | High — clauDNA's door is forward-compatible ("if MCP is configured, the same engine"); hub-rename stays cheap | Highest — keeps the one thing B lacks (`mcp__claudron__*` per-tool permission gating + in-context discovery) available without building it now; aligns with the #644 permissions investment |
| Plan Alignment | Was the original P2/P3, increasingly out of step with #561 and 3f | Strong with the new issues (#560–564, #561); cost = re-scoping #511/#512 | Best — re-scope P2/P3 to real unblocked work now, park A as a demand-gated fork (the epic's existing deferral pattern) |

## Decision: **C — adopt B as the shipping path; defer the MCP fragment as demand-gated**

The clauDNA CLI-skill door is the fleet-consumption path, on the record. The `library/mcp/claudron.json` fragment (option A) is **parked**, adopted only on a named trigger. The conformance wiring (#560–564) and the navigate-vs-query protocol (#561) are what C actually depends on and they stand.

**Ratification:** fleet owner, 2026-07-18. Executed cross-repo the same day — **Claudfather/Claudron#60** marks `03-mcp-server.md` `status: deferred` (⛔ demand-gated), takes E3 off the critical path, and re-opens the post-gate "next epic" slot to E4 (indexer/search). This Claudlobby-side decision doc is the receiving-side counterpart.

## Tradeoffs (named)

- **Not picking A outright** loses in-session typed-tool discovery and `mcp__claudron__*` per-tool permission gating — both gated on an unshipped server anyway, so **zero near-term loss**, and C preserves the option.
- **Picking pure B** (deleting A) risks re-deriving the fragment if E3 later ships with compelling per-tool gating. C's one-paragraph deferral avoids that.

## Flip condition (the demand gate on the parked fragment)

Pull the MCP fragment forward off the parked fork **iff** Claudron ships E3 **and** the fleet needs per-tool permission scoping of vault access (the natural tie-in to the #644 permissions model) — or a non-clauDNA MCP consumer appears.

## Impact on the epic (documentation/plans/2026-07-07-claudron-consumption.md)

- **P2 (#511)** — park the fragment-mount work (2b fragment review, 2e `claudron[mcp]` pipx install, the MCP half of 2d). Option-agnostic wiring (vault reachability, compat floor) folds into #560–564.
- **P3 (#512)** — collapse the `mcp__claudron__*` protocol cutover into "point bots at `/recall` + `/capture` + the #561 navigate-vs-query protocol."
- **P4 (#513)** — no new MCP server in the default template → the **F5 mission-approval gate (PROJECT_MISSION.md :63/:67/:68) is largely moot**; graduation becomes "vault wiring on by default when a vault resolves."
- **#560–564, #561** — unaffected (option-agnostic wiring + the bot protocol); these are the active near-term set.
- **#514 (P5 librarian)** and **1d (INDEX.md cleanup)** — unaffected by A-vs-B.
