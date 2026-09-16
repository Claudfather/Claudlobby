---
title: "Claudlobby Consumes Claudron — the Receiving-Side Epic"
type: plan
status: superseded
owner: chris
tags: [ecosystem, claudron, mcp, protocols, knowledge, play:ecosystem]
created: 2026-07-07
updated: 2026-08-28
links: ["#251", "#266", "#508", "#509", "#528", "#532", "#561", "Claudfather/Claudron#14", "Claudfather/Claudron#17", "Claudfather/Claudron#33", "Claudfather/Claudron#60"]
ironclad: "cycle-1 complete (9 lenses); forks F1–F5 + F7 locked 2026-07-07, F8 locked 2026-07-18 (Decision C — clauDNA CLI-skill door; MCP fragment parked). CONVERGED. v5 (2026-07-18) re-scoped P2/P3/P4 per Decision C + full anchor re-validation (all behavioral claims hold; line drift tabled in the revision log)."
---

# Claudlobby Consumes Claudron — the Receiving-Side Epic

> **⛔ SUPERSEDED (2026-08-28 docs audit).** GitHub EPIC #509 (this plan's tracking issue) was
> closed 2026-07-24 as superseded by the cross-repo boundary-separation program
> (`Claudfather/Claudron` → `documentation/plans/2026-07-20-claudfather-boundary-separation.md`
> §10 + the `2026-07-20-boundary-rearchitecture/` phase docs). This doc is kept for its reasoning
> trail (Decision C, the fork ratifications, the anchor history) — it is **not** a live plan. What
> it asked for was built, mostly beyond its own scope:
>
> | Planned here | Built as | Evidence |
> |---|---|---|
> | P1 (1a/1b/1c/1e) | schema pointer, ladder split, `[vault]` pinned `@v0.4.0`, CLI wedge | `pyproject.toml:21`; #519/#517/#528 |
> | P2d (doctor readiness) | `doctor.check_claudron` + capability probes | `claudlobby/doctor.py`, `claudlobby/claudron_compat.py` |
> | P3 (the consumption door) | **L2** — composed session-loop hooks (`SessionStart`/`PreCompact`/`SessionEnd`) + narrow per-bot verb grants; stronger than the CLI-only wedge this plan specified | `config.py` `claudron_session_loop`; `composer.py` `_merge_claudron_hooks` / `CLAUDRON_LOOP_GRANTS` |
> | *(beyond this plan's scope)* | **L3** — the lessons corpus triaged and migrated to the vault | `documentation/plans/2026-07-23-l3-lessons-triage-ledger.md` (status: completed); `claudlobby/commands/lessons_migrate.py` |
> | #560/#564 groundwork | **L4** — conformance gates, CI-wired | `claudlobby/conformance.py`, `tests/test_boundary_invariants.py` |
> | — | boundary `CLAUDE.md` seams + root Ecosystem boundary section | root `CLAUDE.md`, `claudlobby/CLAUDE.md` (#664) |
>
> All five child issues (#510–#514), #251, and the #266 umbrella are closed. Decision C
> (`documentation/decisions/2026-07-18-claudron-consumption-door.md`) is **not** reversed by any
> of this — the program built on top of it, not against it.
>
> **Genuine residuals carried forward** (orphaned when #509 closed; tracked at **#1248**, "the
> five orphans of the closed #509 epic"): #560 (overlay-path/reserved-name conformance, open),
> #562 (the claudron-*installed* mount branch is untested, open), #564 (shared-tier set still
> hardcoded + double-write into the vault, open), **#745** (vault-hygiene guardrail — bots can
> write to the vault with no composed rule about what belongs there, open), **#746** (session-loop
> per-session cost never measured on Pi-class hardware, open). #561 (navigate-vs-query protocol)
> closed separately, via the `shared-documentation-vault` gated protocol variant.
>
> Current source of truth for what's shipped: `documentation/integrations/claudron-integration.md`.
> The Decision Forks / Ironclad findings below remain an accurate historical record and are left
> as originally written.

Companion to Claudron's six-epic roadmap (Claudfather/Claudron PR #13 → EPIC #14, children #15–#20). Claudron authors the two inbound PRs (the `claudron.json` MCP fragment and the `frontmatter-schema.md` SSOT pointer); **this epic plans everything Claudlobby-side around receiving them**: reception criteria, an interim CLI query wedge, compositor/validator/doctor readiness, protocol cutover, the approval-gated graduation to fleet default, the version-pin policy, and the librarian standing job.

> **For agentic workers / reviewers:** This is a `/forge`-style plan. **Decision Forks are ALL LOCKED** — the plan PR (#508) merged before the lock ceremony, so the fleet owner ratified F1–F5 + F7 in-session (2026-07-07); each fork's Status line below is the lock record, committed via the follow-up fork-locks PR (which stands as the `[FORK-LOCK]` artifact). Sizing is **S/M/L per phase — no calendar estimates** (fleet convention). This is **plan-only**; no implementation in this PR. Format note (plan-health disposition): this doc keeps the repo's committed single-doc plan shape (the goal-aware and fleet-update exemplars) rather than the plugin's one-doc-per-phase skeleton; section names map 1:1 (Goal≈Summary, Verified Current State≈Evidence, Phases≈Implementation Plan, Validation≈Verification Checklist) and the issue mapping below is the one-phase-one-issue ledger.

## Revision Log

### v5 (2026-07-18, Decision C re-scope + full re-validation)

**Two things after a week+ of heavy Claudlobby shipping (38 commits past v4):**

**1. Decision C ratified — the consumption door is clauDNA's CLI-skill, not a Claudlobby MCP fragment.** A `/weigh-development-paths` pass (7 dimensions, 3 options) picked **C: adopt clauDNA's `/claudron`/`/recall`/`/capture` CLI-skill door as the fleet-consumption path now; park the `library/mcp/claudron.json` fragment as a demand-gated option** (adopt only when Claudron E3 ships *and* per-tool permission scoping is wanted — the #644 tie-in). Full record + matrix: `documentation/decisions/2026-07-18-claudron-consumption-door.md`. Ratified by the fleet owner 2026-07-18 and **executed cross-repo the same day** — Claudfather/Claudron#60 marks their `03-mcp-server.md` `status: deferred` (E3 off the critical path). Rationale in brief: Claudron E3 has no active work (only `v0.2.0` tagged), clauDNA already ships the door CLI-based on every bot ("clauDNA ships no MCP servers — this engine IS the CLI… if MCP is configured, the same engine"), and the epic's own #561 already reframed bot↔hub as navigate-vs-query over `recall`/`lookup`. **Scope impact:** P2 sheds the fragment-mount work (2b/2e/MCP-half-of-2d → parked); P3 collapses to "point bots at `/recall`+`/capture` + #561"; **P4's F5 mission-approval gate is largely moot** (no new MCP server in the default template); the wiring children #560–564 and the #561 protocol are what C depends on and they stand. New **Fork F8** records the parked fragment. Forks F1–F5/F7 unchanged.

**2. Full anchor re-validation vs `main` @ f2863a8 — every behavioral claim HOLDS; nothing removed or reversed.** Line numbers drifted heavily (composer/config/validator ~+100–130 lines each); the corrected anchors are tabled here rather than hand-patched inline (they re-drift every week — treat the Python-file anchors as symbol-cited, re-verified at gate-fire per the anchor re-validation rule):

| Claim (v4 anchor) | Corrected @ f2863a8 |
|---|---|
| CLAUDRON_VAULT_PATH emission `composer.py:501-512` | `# Ecosystem` block `composer.py:604-615` (CLAUDRON_VAULT_PATH `:611`) |
| shared-docs mkdir scaffold `composer.py:1874-1883` | `composer.py:2258-2266` (same five dirs, unconditional) |
| protocol auto-include `composer.py:885-888` | `composer.py:1005-1008` |
| `collect_env_contracts` provided_by skip | **retargeted (#568): shared kernel `iter_operator_contract_vars` (`composer.py:1602`, called `:1620`) — P2a tests target this SSOT** |
| `claudron_vault_path` field `config.py:364`; defaults `:935-936` | field `config.py:403`; defaults `:1057-1058` |
| `_merge_mcp_lists` `config.py:509-518`; merge in `_coerce_bot` `:893-895` | def `config.py:563-572`; merge call `:1015-1017` |
| validator cross-check `validator.py:271-282` (key `:272`) | `validator.py:403-413` (keys on "claudron" `:404`) |
| warm-cache npx-only `commands/core.py:392-456` | `cmd_warm_cache` `commands/core.py:415` (npx filter `:440`) |
| start-bot bot.conf-last `:137-152` | `start-bot.sh:155-162` (now `set -a`-wrapped) |
| `fleet-yaml-schema.md` claudron_vault_path `:441-451`; 2c example `:448` | **badly stale → field `:124`, doc `:493-503`, example `:500`, warn-note `:597` (2c's correction target is now `:500`)** |
| shared-documentation.md pre-work INDEX + 5-cap `:14-16` | `:14-17` (5-doc cap line `:17`) |
| dispatch.md INDEX preflight `:119-136` | `:128-145` (scan `:132-133`, monitor `:139-145`) |
| doctor.py non-npx warn `:124-131` | `doctor.py:128` |

Confirmed still-true (Option-A-parked-relevant): `library/mcp/claudron.json` **does not exist** ✅; `apply_ecosystem_defaults` **not yet built** ✅; doctor has **no `check_claudron`/`--json`** ✅; `provided_by:"composer"` shipped (`library/mcp/README.md:31`) ✅; `[vault]` pinned `@v0.2.0` (`pyproject.toml:21`) ✅; INDEX.md text still present (1d not run) ✅. New neighbor to note: #644 added `permission_mode`/`dangerously_skip_permissions` emission at `composer.py:405-406` (separate region); the ecosystem block (`:604-615`) now sits between briefing (`:600`) and plugin-sync (`:618`) — 2c's `apply_ecosystem_defaults` must run before this emission point.

### v4 (2026-07-10, accuracy + companion-consistency pass)

Re-anchored against `main` after three days of shipping (no scope change; forks unchanged). **P1 substantially landed** — 1b (ladder split), 1c (`[vault]` pinned to `@v0.2.0` + `claudron_compat.py` + `claudron-integration.md`), 1e (**#528**, the interim CLI query wedge in `dispatch-task.sh`), and 2a (**#532**, `provided_by:"composer"`) all merged; their checkboxes are now `[x]`. Three "Verified Current State" claims were overtaken by their own fixes and are corrected below: the `[vault]` extra is **pinned** (not "unpinned HEAD"); the promotion ladder is **already the corrected split**; `SHARED_SUBDIRS` is **four tiers** (`planning/` landed with E1). The `PROJECT_MISSION.md` anchors naming the F5 approval shifted +2 (`:61/:65/:66` → `:63/:67/:68`) and are re-pinned. All other file:line evidence anchors re-verified against `main` on 2026-07-10. Added the **Claudron vault-structure contract** (Claudfather/Claudron PR #33) to Companion Plans — the structure-defining counterpart ratified 2026-07-09 (Claudron owns the folder-structure SSOT; this epic consumes it), with its Claudlobby children #560–#564.

### v3 (2026-07-07, fork-locks PR)

All six open forks **locked by the fleet owner on their leans** — F1 (SHA pin → tag), F2 (`provided_by: "composer"`), F3 (host-level pipx/uv via `setup-system --with-claudron`), F4 (retire INDEX.md — retirement still executes only after the clauDNA handoff + live-fleet sweep), F5 (conditional auto-mount + generic `mcp_exclude`; the lock discharges mission :63/:67/:68 — P4 still implements only after the soak bundle passes), F7 (adopt the mechanical CLI wedge). No lean reversed → the Claudfather/Claudron#17 contract feedback stands as posted; Claudron authors their fragment PR against it (confirmation posted on #17). **Plan status: CONVERGED** (zero open Blockers, all forks locked). Also recorded: **PR #490 merged 2026-07-07** — P2's coordination clause is resolved; composer/config line anchors cited below have shifted (the anchor re-validation rule covers re-verification at each gate-fire). Filed artifacts: EPIC #509, children #510–#514, plan PR #508.

### v2 — post-ironclad cycle 1

Nine lenses (first-principles, adversarial, cost-benefit, precedent, plan-health, align-to-mission, engineering, ceo, devex); every file:line evidence claim survived audit. The findings that reshaped the plan:

1. **#251 closure race (the one Blocker).** Claudron E3's acceptance literally reads "#251 closes" at their MCP release while this plan holds #251 open until P4d. The filing-time Claudfather/Claudron#17 comment now carries this divergence (their fragment PR must *reference*, not `Closes`, #251), and the 2b review checklist checks their PR body for it.
2. **Gates re-phrased capability-first.** Claudron's release numbers are ordinal, not epic-bound (their own cycle-2 finding): every gate is now "the first tagged release shipping <capability> (0.X.0 in default order)", and the compat table keys on capabilities.
3. **New Fork F7 — interim CLI query wedge + query transport.** Claudron v0.1 already ships CLI `lookup`; CLI and MCP are two doors on one engine. The plan previously left the entire adoption story to protocol prose (the exact failure class INDEX.md died of) and left fleet-side evidence waiting on an *evidence-gated option* (their G1). F7 adopts a mechanical, deterministic preflight injection in the dispatch helpers, available from the P1 pin, off by default.
4. **Dead-text cleanup pulled forward (P1d).** The claudron-independent halves of the protocol rewrites — killing INDEX.md instructions that point at files which have never existed — need nothing from Claudron and no longer wait for it.
5. **F4 gains a clauDNA coordination gate.** "Zero adoption" is true of this repo but the fleet-default clauDNA plugin ships `/claudna:index` ("the SOLE writer of INDEX.md files", auto-run by `/claudna:learn`) and is expanding it (clauDNA#36). Retiring the convention without a clauDNA handoff would be exactly the unilateral cross-repo move this plan forbids itself toward Claudron.
6. **Auto-mount hardened.** F5(a) now conditions on vault-resolves AND `claudron-mcp` resolvable (a `.claudron` bridge file resolves a vault even when claudron was never installed — mounting on vault alone composes a fleet of broken servers).
7. **P4 soak rebuilt to measure outcome, not just motion** — minimum-corpus precondition, numeric compliance floor, an impact criterion (the saved-me-tally analog), named instruments (including fleet RSS — the mission's Pi-baseline RAM constraint was previously unmeasured), and an explicit soak-fail branch.
8. **Effective-config normalization named** (`apply_ecosystem_defaults`) so composer/validator/doctor/permissions/env-contracts all see the same post-auto-mount fleet; F2's doctor semantics specified per-bot and in-memory.
9. F6 (librarian dispatch target) demoted to a plan decision — one live option; fork ceremony reserved for the contested (F2 contract shape, F3 install story, F4 ADR reversal, F5 mission gate, F7 transport).

## Goal

Make "query Claudron before tasks, write findings after" (PROJECT_MISSION.md:11, sprint focus #4) real in composed bots — without Claudlobby ever storing the knowledge corpus, forking the schema, or taking a hosted dependency. When Claudron's release train delivers each plug, the matching Claudlobby socket is ready: fragment reviewed and landed, compositor defaults sane, doctor checks live, protocols rewritten, librarian job composed. When the train is late or Gate G1 re-orders it, **nothing here dangles** — every phase gates on a shipped capability, not a calendar, and P1 pays debts that are real today regardless.

**Success floor (mirror of Claudron's D10):** P1+P2+P3 landed = this epic succeeded — the socket works and the behavior is real on opt-in fleets. P4 (default-ness) and P5 (librarian) are compounding upside on Claudron's evidence-gated tail and may legitimately trail or never trigger.

## Relationship to Claudron's roadmap

| Claudlobby phase | Gate (capability-phrased) | Claudron epic |
|---|---|---|
| P1 — SSOT reception, ladder fix, pin, dead-text cleanup, interim query wedge | none (SSOT-pointer PR rides the first tag shipping E1 — 0.2.0 in default order; everything else is unconditional) | E1 (#15) |
| P2 — ~~Fragment reception~~ → **re-scoped (Decision C):** vault-wiring readiness (compat/doctor); the MCP-fragment half is **parked (Fork F8)** | fragment-mount half → demand-gated (F8); wiring → now, folds into #560–564 | ~~E3~~ (parked) |
| P3 — ~~Protocol cutover (MCP overlay)~~ → **re-scoped (Decision C):** point bots at clauDNA `/recall`+`/capture` + the #561 navigate-vs-query protocol | none MCP-gated — clauDNA door ships today; folds into #561 | — |
| P4 — Graduation to fleet default | **F5 gate largely moot (Decision C — no new MCP server in the template);** graduation = vault wiring on when a vault resolves | — |
| P5 — Librarian standing job | first tagged release shipping `claudron review --json` (E5; 0.5.0 in default order) | E5 (#19) |
| Deferred — events.jsonl fleet observability | first fleet with ≥2 actively-writing bots (Claudron's own F8 milestone) | — |

Claudron's Gate G1 sits between their 0.2.0 and everything after; E3–E6 are evidence-gated options, not commitments (their D9/D10). If G1 re-orders E3/E4, version numbers follow ship order — the capability phrasing above tracks that. If the tail stalls entirely: P1 (including the dead-text cleanup and the F7-locked CLI query wedge) is unconditional and already valuable; P2–P5 wait on capabilities that haven't shipped; #251 stays open with correct fallback protocol text; no Claudlobby code path breaks. **Anchor re-validation rule:** each gated phase, on gate-fire, re-verifies its cited anchors (and the #490-class merge adjacency) if more than ~4 weeks have passed since plan date — this doc is a point-in-time snapshot of a repo that ships daily.

## Verified Current State

All anchors verified against this checkout on 2026-07-07 (`main` @ 5b51a0a) and re-audited by the ironclad panel (every claim below held exactly).

**The socket is built; the plug is missing.**
- `.claudron` bridge parser: `paths.py:42-58` (shell-sourceable key=value; gitignored at `.gitignore:64`). Vault-based fleet resolution `_resolve_vault_fleet` (`paths.py:104-130`): claudron's `Vault.fleets` API when installed, manual `vault/<fleet>/fleet.yaml` check otherwise — **note:** the manual fallback resolves a vault even when claudron is *not installed*, which is why F5's auto-mount must gate on binary presence. Detection precedence with fallback to `local/<fleet>/`: `paths.py:380-427`.
- History note (precedent lens): the vault layer was built in-repo (#206/#220-#222), extracted to Claudfather/Claudron (#297), and re-integrated as the optional `[vault]` extra (#300) — the arc that founded this plan's boundary invariants and introduced F1's unpinned `git+HEAD` line.
- Per-bot `CLAUDRON_VAULT_PATH` emission into `bot.conf`: `composer.py:501-512` (sprint #6, **done** — #254 closed via #273). Field is per-bot with fleet-wide `defaults:` fallback (`config.py:364`, `:935-936`; documented `fleet-yaml-schema.md:441-451` — its per-bot sub-path example is corrected in P2c to match the fleet-root + query-time-scoping model).
- Bidirectional vault-path ↔ claudron-MCP cross-check: `validator.py:271-282`, keying on MCP entry name `claudron` (`:272`) — **warns about an MCP server that cannot be mounted yet** (no `library/mcp/claudron.json` exists; a fleet.yaml `mcp: [claudron]` entry fails fragment loading today).
- `defaults.mcp` **already merges into every bot** (applied in `_coerce_bot`, `config.py:893-895`, via `_merge_mcp_lists` at `:509-518`) — fleet-wide mounting is one YAML line once the fragment exists. The merge is union-by-name, **defaults-first-wins**: no per-bot exclusion exists, and a bot re-declaring `claudron` to customize it would be silently shadowed — both facts shape F5.
- Shared-docs scaffold at generate time: `compose_fleet` mkdirs `planning/active`, `planning/completed`, `decisions`, `knowledge`, `runbooks` under `paths.shared_docs` (`composer.py:1874-1883`). In vault mode `paths.shared_docs = fleet_dir/shared` **is inside the vault** (`paths.py:308-312`) — a second writer of tenant-owned structure beside Claudron's `cmd_fleet_add` (which scaffolds the same five fleet paths via `scaffold_shared_tree`, `Claudron/claudron/cli.py:726-748`; vault-level `SHARED_SUBDIRS` is now **four tiers** — `knowledge`/`decisions`/`runbooks`/`planning`, `vault.py:48-56` — `planning/` landed with E1). P2c retires the vault-mode mkdir instead of freezing it; the double-write is tracked as sibling issue **#564**.
- Protocol auto-include precedent: `shared-documentation` is appended to every bot's protocols when shared docs resolve (`composer.py:885-888`) — the pattern the claudron protocol rides.
- `[vault]` extra is **pinned to tag `@v0.2.0`** (`pyproject.toml:21`, guarded by `test_claudron_compat.py`; the compat floor is machine-readable in `claudron_compat.py`) — F1/step-1c shipped. Claudron v0.1 already ships a working CLI `lookup` over the vault index — the fact F7 builds on.
- Bot session PATH: `start-bot.sh:49` exports `$HOME/.local/bin` (the default `pipx`/`uv tool` target) into every bot session, and env-tier sourcing puts `bot.conf` last (`:137-152`) — F3(a)'s console-script story verified sound.

**The conventions this epic rewrites.**
- `dispatch.md:128-137` preflight tells managers to scan `shared/planning/active/INDEX.md` and `shared/knowledge/<repo>/INDEX.md`; `:139-145` has the manager monitor INDEX.md on a cycle. `shared-documentation.md` pre-work checks scan the same INDEX files with a hard 5-doc read cap (`:14-16`), and its promotion ladder (`:47-54`) is **already the corrected split** (`memory/` → `shared/` → vault `_shared/` → packs; `library/` a separate track — step 1b landed). `templates/claude.md.j2:117` is a **fourth consumer** ("Each subdirectory has an INDEX.md — scan it before creating new files"), composed into every bot's Shared Documentation section. `precedent-check/SKILL.md:17-21` scans `shared/decisions/` and `shared/planning/active/` **directly — it never reads INDEX.md** (the ADR's status note overcounted it; do not propagate that error into the supersession note).
- The INDEX.md convention is **ratified with zero adoption in this repo** — its own decision doc says so (`documentation/decisions/index-md-convention.md:12`): no INDEX.md exists in this checkout, no `/index` producer ships *here*. **But the fleet-default clauDNA plugin (auto-installed on every bot) ships `/claudna:index`** — self-declared "SOLE writer of INDEX.md files", auto-run by `/claudna:learn` after knowledge writes, referenced by `/reflect` and `/publish`, with clauDNA#36 (open) extending it — so composed bots may have *produced* INDEX.md files in live fleet overlays. F4's retirement is therefore a cross-repo coordination, not a local deletion, and a live-fleet `find local/*/shared/ -iname INDEX.md` is part of its lock evidence. History (precedent lens): the convention shipped as the consumer phases (PRs #150/#151) of a plan whose in-repo producer phase never landed — consumers-before-producer from birth.
- Claudron E5 corrects the promotion ladder: claudlobby's `library/` rung is *cross-deployment building blocks via OSS PR*, not a knowledge tier; the knowledge ladder with a vault is bot `memory/` → fleet `<fleet>/shared/` → vault `_shared/` → packs (their E6 — beyond their success floor, so the rung is written conditionally).

**Reception machinery that exists / is missing.**
- `doctor.py` has the `Check`/`DoctorReport` pattern and consolidated env/npx/reconcile checks — **zero claudron checks**, and **no `--json` flag** (the subparser registers none; `status --json` is the pattern to mirror). `doctor.py:124-131` already warns on unresolvable non-npx MCP commands — baseline coverage before P2d.
- The MCP fragment contract (`library/mcp/README.md`) defines `_env_contract` (tier `fleet|bot`, scope `shared|instance`), `_permissions_contract`, `_global_binary`. Two wrinkles for a claudron fragment:
  1. `_env_contract` drives `.env` scaffolding and doctor's env presence check — but `CLAUDRON_VAULT_PATH` is **composer-emitted into bot.conf**, not operator-supplied. As Claudron E3 currently specs the fragment (their `03-mcp-server.md:97-98`), the default `tier: fleet` would scaffold an empty `export CLAUDRON_VAULT_PATH=` stub into the fleet `.env` (→ doctor **warn** "empty"); `tier: bot` would land in per-bot `.env` files doctor never merges (→ hard **fail** "missing"). Either way a false alarm. Two sibling composer-emitted vars (`CLAUDNA_VERSION`, `CLAUDOSSEUM_TENANT_ID`, `composer.py:501-512`) would hit the identical wrinkle in future fragments — the F2 premise is generic, not claudron-specific.
  2. `_global_binary` and `claudlobby warm-cache` (`commands/core.py:392-456`) are **npx-only** (warm-cache filters on `command == "npx"`, so a bare `claudron-mcp` command is silently and safely skipped); `check-npx-cache.sh` likewise. Claudron's server is a Python console script — none of the existing cold-start machinery covers it, and none of it misfires on it either.
- Fleet-job machinery for the librarian exists end to end: `defaults.jobs` name-merge with the composed-but-dormant `enroll: false` pattern (`fleet-yaml-schema.md:202-212`, `claudlobby/system.yaml:111-115`), and `code-audit-sweep.sh` as the shape to mirror — with one documented wart the mirror must not fork: `bot-sweep-cron.sh` exits 0 on both a real dispatch (implicit, after `:64`) and a busy-skip (`:58`) — only failure differs (`exit 1`, `:67`) (the 2026-06-12 plan said "extend the shared dispatcher, don't fork it"; code-audit worked around it by grepping its own log). launchd caveat from the goal-aware plan: sub-daily cron `schedule:` degrades on macOS; weekly `OnCalendar` with weekday+HH:MM survives.

**Merge adjacency.** PR #490 (`feat/projects-tier`, goal-aware plan P2) touched the same four files with hunks **disjoint from this epic's regions** (composer insertion adjacent to but not overlapping the ecosystem block; `validator.py:271-282` untouched). **Resolved: #490 merged 2026-07-07 (a16c2f7)** — the coordination clause is discharged; composer/config line numbers cited in this doc have shifted, and the anchor re-validation rule covers re-verification at each gate-fire. The goal-aware plan's Fork F2 names "vault sync carries the registry (future Claudron wiring)" as accepted residual risk — that future work is **not** pulled into this epic.

## Architecture

```
        Claudron release train                Claudlobby receiving side
        ─────────────────────                 ─────────────────────────
 E1/E2  SCHEMA.md (SSOT),      ──PR──▶  P1  frontmatter-schema.md → conformance profile
 (0.2.0 session loop                   P1  shared-documentation.md ladder corrected
  default                              P1  [vault] pin: SHA now → tag/range at their release
  order)                               P1  dead INDEX.md text dies (F4; clauDNA handoff)
                                       P1  interim CLI query wedge (F7): dispatch-task.sh
                                            injects `claudron lookup` hits — deterministic,
                                            off by default, dogfood fleet on
   ═══ Gate G1 (theirs) ═══
 E3     claudron-mcp           ──PR──▶  P2  library/mcp/claudron.json lands (review checklist)
 (0.3.0 five tools, write lock,        P2  fragment contract ext: provided_by:"composer" (F2)
  default events.jsonl                 P2  apply_ecosystem_defaults: auto-default + auto-include
  order)                               P2  doctor: check_claudron + --json; status vault panel
                                       P2  install story: claudron-mcp on PATH (F3) + quickstart
                                       P3  protocols gain the MCP overlay: claudron_lookup
                                            preflight / claudron_write capture (fallback kept)
                                            │ soak (dogfood fleet, corpus-preconditioned,
                                            │       floor + impact criteria, fail-branch)
                                       P4  graduation: vault+binary-conditional default (F5)
                                            — the mission :63/:67/:68 approval artifact
 E5     review --json          ──────▶  P5  lib/claudron-review-sweep.sh + dormant weekly job
 (0.5.0 default order)                       librarian bot drains the queue
 (later) first ≥2-writer fleet ──────▶  deferred: events.jsonl → fleet-pulse integration
```

**Transport principle (stated once):** interactive bot sessions get the **MCP server** (typed tools, `mcp__claudron__*` permission gating, in-context discovery); no-LLM host jobs and dispatch helpers get the **CLI** (deterministic, no session needed). Don't wire the CLI into bot protocol text or the MCP into cron jobs.

Boundary invariants (both missions): Claudlobby never stores the corpus — the vault is tenant-owned; `runtime/` and `.env` never enter it. Claudron's `SCHEMA.md` is the tri-repo SSOT — deltas are filed as feedback, never forked locally. Contract gaps discovered here go to Claudfather/Claudron#17 as comments, not unilateral redesigns. Read-side trust mirrors write-side hygiene: bots treat `maturity: draft` lookups as unverified input.

## Phases

### Phase 1 (M): SSOT reception, ladder correction, pin, dead-text cleanup, interim query wedge — unconditional

**Dependencies:** none (1a's inbound PR arrives with Claudron's E1 release; 1b–1e are valuable before it does; F4 and F7 are **locked** — 1d still waits on its execution precondition, the clauDNA handoff being filed). **Blocks:** P2 (1c pin policy), P3 (1b/1d text base).

**Steps:**
- [ ] 1a. **Receive Claudron's `frontmatter-schema.md` SSOT-pointer PR** (Claudron-authored). Review checklist: (i) `library/resources/frontmatter-schema.md` becomes a *conformance profile* pointing at Claudron `SCHEMA.md` as SSOT — keeps the local type-aware status tables as the profile, adds the documented equivalence mapping (`current≈active`, `ratified` ≈ locked decision, etc.); (ii) the `maturity` trust axis (`draft|verified|canonical`) is introduced as *Claudron-defined, optional here*; (iii) no local field silently dropped (the E1 superset guarantee — `expires`, `source_url`, `slug`, `last_verified`, `supersedes` all carried); (iv) composed output still renders (generate diff); (v) **taxonomy-shape check:** the ratified `SHARED_SUBDIRS`/fleet-dir shape matches the composer's five-dir list exactly — file any delta on Claudron#17 before P2c acts on it. *Vocabulary note:* the ≈-mapping is transitional, not a soft fork — if living with a permanent dialect proves worse than a one-time migration of local docs to SSOT vocabulary, that migration is filed as feedback on Claudron#15, decided there.
- [x] 1b. **(landed)** **Correct the promotion ladder** in `library/protocols/shared-documentation.md:43-54`: split the conflated rung — knowledge promotes bot `memory/` → fleet `shared/` → (when a vault is wired) vault `_shared/` → (when Claudron packs exist — their E6) packs; **reusable building blocks** (skills, protocols, guardrails) promote to `library/` via OSS PR as a *separate track*, which is what today's rung 3 actually meant. Claudron-less fleets keep the two-rung knowledge ladder + the library/ track. Also fix `:41` ("librarian cron runs `/index --stale` weekly") — reference the future `claudron review` queue, marked "when a Claudron release with `review --json` is wired". Every future-rung mention carries its condition — no new dead text.
- [x] 1c. **(landed — pinned @v0.2.0)** **Pin the `[vault]` extra + write the version policy** (F1). Immediate: pin `pyproject.toml:21` to Claudron's current main SHA. At their first release: move to the tag (or `claudron>=0.2,<0.3` if the PyPI publish lands as planned). Policy: the extra tracks the **compositor's API consumption** (currently `claudron.vault.detect`); bump per Claudron release after claudlobby's vault-mode tests pass against it. The compat floor (claudlobby feature ↔ minimum claudron capability/version) lives **machine-readable in one place** — a small constant module (e.g. `claudlobby/claudron_compat.py`) that doctor reads — and `documentation/integrations/claudron-integration.md` (new; placement per the `notion-integration.md` precedent) renders/references it. The MCP *server* install (host-level, F3) is deliberately **not** coupled to the extra — bots don't run in claudlobby's venv.
- [ ] 1d. **Dead-text cleanup** (claudron-independent; F4 **locked** — execution contingent only on the clauDNA handoff item being filed): (i) live-fleet evidence sweep — `find local/*/shared/ -iname INDEX.md` across the fleet hosts; existing files, if any, become a data-disposition note in the supersession record; (ii) rewrite `dispatch.md:128-145` preflight + manager-monitoring and `shared-documentation.md:14-16` (the INDEX text is still present — 1d not yet run) pre-work checks to **direct directory listing + frontmatter scan** of `shared/planning/active/` and `shared/knowledge/<repo>/` (the claudron-less permanent fallback — no INDEX.md, no producer needed); (iii) rewrite `templates/claude.md.j2:117` (the fourth consumer — template edits are in scope, templates own top-level structure); (iv) mark `documentation/decisions/index-md-convention.md` `status: superseded` with a note pointing at `claudron_lookup` + this plan (and *not* repeating the ADR's precedent-check overcount); (v) repo-wide grep sweep so nothing else instructs reading or writing INDEX.md.
- [x] 1e. **(landed — #528)** **Interim CLI query wedge** (F7 **locked** — go): (i) short spike — v0.1 `claudron lookup` against a fleet-shaped vault (scoping args, JSON output shape); (ii) `dispatch-task.sh` gains an env-knobbed preflight (`CLAUDRON_QUERY_BEFORE=1` in bot.conf/fleet defaults, off by default): when set and `claudron` CLI + vault resolve, shell `claudron lookup` with the task text and prepend hits to the dispatched prompt — deterministic query-before with zero protocol prose; (iii) **designate + wire the dogfood fleet** (owner: fleet owner — the Pi fleet): create/clone its vault, write the `.claudron` bridge, flip the knob on; (iv) record lookup-volume counters for Claudron's G1 evidence (their own metric: "lookup/MCP calls per bot task in a Claudlobby fleet").

**Validation:** `claudlobby --fleet <fleet> generate && claudlobby diff` show only intended text drift; `pip install -e '.[vault]'` resolves the pinned SHA; pytest vault-mode tests green; post-1d, a composed bot's CLAUDE.md contains zero INDEX.md references (grep); post-1e, a dispatched task on the dogfood fleet visibly carries prepended lookup context (dispatch-log inspection — cite the observation).

**Standalone value:** the tri-repo schema drift stops; installs stop depending on a moving HEAD; the ladder doc stops giving wrong instructions; every composed bot's dead preflight text dies **now**, not when an external train arrives; and (under F7) the fleet starts generating real query-volume evidence that feeds Claudron's G1 — before their E3 even starts.

### Phase 2 (M): Fragment reception + compositor/validator/doctor readiness — gate: first Claudron release shipping the MCP server

> **⛔ RE-SCOPED by Decision C (2026-07-18) — `documentation/decisions/2026-07-18-claudron-consumption-door.md`.** The **MCP-fragment half is parked (Fork F8):** 2b (review/land `library/mcp/claudron.json`), 2e (`claudron[mcp]` pipx install), and the MCP-specific half of 2d (`check_claudron` for a mounted server) do **not** happen unless F8's demand gate fires. **What survives now:** 2a (shipped, #532), and the **option-agnostic wiring** — vault-reachability doctor coverage, the `claudron_compat.py` floor, and the `apply_ecosystem_defaults` vault auto-default (2c) — which **fold into the conformance children #560–564**. 2a's test target is retargeted to `iter_operator_contract_vars` (the #568 shared kernel). The steps below are retained as the *parked-fragment* reference for if/when F8 is un-parked.

**Dependencies:** P1c; ~~the gate capability (E3 shipped)~~ **(parked — Decision C).** ~~Coordination: PR #490~~ (merged 2026-07-07 — resolved). On any un-park, run the anchor re-validation rule. **Blocks:** ~~P3, P4~~ (P3/P4 no longer depend on the fragment).

> **Re-sequencing (ratifier-approved, 2026-07-08):** step **2a's claudlobby-internal half is pulled ahead of the gate** — the `provided_by: "composer"` contract extension needs nothing from Claudron's code, F2 is locked, and building it first means Claudron's fragment PR validates against machinery that already exists with tests. The Claudron#17 feedback comment (also 2a) was posted at epic filing. The reception steps (2b) and the doctor/composer/install work (2c–2f) remain gated as written.

**Steps:**
- [x] 2a. **(landed — #532)** **Extend the fragment metadata contract** (`library/mcp/README.md`) per F2: an `_env_contract` entry may declare `"provided_by": "composer"` — semantics: `scaffold_env_files` and `check_env_vars` **skip it entirely**; the per-bot pairing check lives in `check_claudron` (2d) with the same conditionality as the validator (absent + no vault resolvable → warn, never a hard fail; resolved **in-memory** via the same config logic the composer uses — never by parsing generated `bot.conf` files, which are absent pre-generate and stale post-edit). Unit tests: red-green on `collect_env_contracts` filtering. **File the contract feedback on Claudfather/Claudron#17 at epic filing time** (before their fragment PR is authored), carrying: fragment key must be `claudron` (`validator.py:272` keys on it); command is the bare `claudron-mcp` console script (F3); `_env_contract.CLAUDRON_VAULT_PATH.provided_by: "composer"` (with the tier-specific false-alarm behavior spelled out); `_permissions_contract.tools` = the five `mcp__claudron__*` names; **no `_global_binary`** (npx-only mechanism); **and the #251 closure divergence** — their PR must reference, not close, #251 (closure is claudlobby-owned at P4d). *(Comment posted at epic filing, 2026-07-07; F2/F3 subsequently locked on their leans and confirmed on #17 — Claudron authors PR3 against the confirmed shape.)*
- [ ] 2b. **Review + land Claudron's `library/mcp/claudron.json` fragment PR** (Claudron-authored, into #251) against the 2a checklist, plus their `claudron-query-before-write-after.md` protocol doc (per #251 item 3). Additional checks: **PR body does not carry `Closes #251`**; re-verify the shipped tool names/response contracts against this plan's assumptions (re-anchor, don't assume); taxonomy-shape check from 1a(v) still holds. Verify: fragment loads (`claudlobby validate` with a test bot mounting `claudron`), permissions render as `mcp__claudron__*` allow patterns, `.mcp.json` env expansion carries `${CLAUDRON_VAULT_PATH}`.
- [ ] 2c. **Compositor: one normalization point.** New `apply_ecosystem_defaults(fleet, paths)` run once after `load_fleet`, before any consumer — composer, `_resolve_mcp_permissions`, `collect_env_contracts`/`scaffold_env_files`, validator, and doctor all see the same effective fleet. In it: (i) vault auto-default — when a bot mounts the `claudron` MCP and has no `claudron_vault_path` (bot or defaults), default to `paths.vault_root` when the `.claudron` bridge resolves one; explicit config always wins; emit nothing when no vault resolves. The emitted bot.conf line carries a provenance comment (`# auto-defaulted from .claudron bridge`). Model note: the default is the **vault root** — scoping is query-time (tool args / CLI flags), per Claudron's design; correct `fleet-yaml-schema.md:448`'s per-bot sub-path example to match. (ii) Protocol auto-include for bots that mount the fragment (the `composer.py:885-888` pattern) — the claudron protocol is a **separate auto-included doc**; base protocols carry only the fallback text (answers the variant-mechanism question: no dual-branch prose taxing claudron-less bots). (iii) Validator: the `:278-282` warning fires only when the auto-default also can't resolve, and its message names **both remedies** (set `claudron_vault_path`, or create the `.claudron` bridge / run `claudron init`); inverse warning kept. (iv) **Vault-structure handover:** in vault mode, `compose_fleet` **skips** its shared-docs mkdir when the vault fleet dir is already initialized (Claudron owns vault structure — removal, not freeze-and-watch); the mkdir stays for claudron-less overlays only. Unit tests: composer/validator/doctor agree on the effective MCP list + vault path across {auto-default, explicit-config, no-vault} cases.
- [ ] 2d. **Doctor: `check_claudron`** (conditional — silent for fleets with no claudron surface): when any bot mounts the fragment (post-normalization view) → `claudron-mcp` resolvable on PATH, `claudron --version` within the compat constants (1c), per-bot vault path exists, `claudron status --json` healthy (when CLI present); when `[vault]` extra installed → import + version match; when vault set but nothing mounts it → mirror of the validator warning. **Doctor `--json` is net-new and in scope** (mirror `status --json`). Plus: `claudlobby status` gains a small vault panel (notes count, last write, lookup/write counters via `claudron status --json`) — the operator's "is the memory working" surface.
- [ ] 2e. **Install story** per F3: `setup-system` gains an **explicit opt-in flag** (e.g. `--with-claudron`) installing `claudron[mcp]` via `pipx`/`uv tool` — including installing pipx/uv itself if absent (it is not a current prereq; `setup-system` uses bare pip today). One host-level install serves all bots (the WS-2 cold-start-dogpile lesson from #474). `documentation/integrations/claudron-integration.md` charters the **operator quickstart** — the end-to-end first-time walkthrough (install → vault create/clone → `.claudron` bridge → one fleet.yaml line → generate → restart → doctor → first query) — plus the late-adoption single re-run command; linked from `getting-started.md` and `integrations.md`. `environment-variables.md` documents `CLAUDRON_VAULT_PATH` (+ `CLAUDRON_QUERY_BEFORE` if F7 locked).
- [ ] 2f. **Resource budget check:** `lib/bench-cold-start.sh` before/after mounting the fragment on a test bot, **plus a per-bot and fleet RSS delta** (the mission's Pi-5 RAM constraint; `lib/fleet-memory-check.sh` is the instrument). Set the numeric budgets here — cold-start delta ≤ 1s and RSS delta within the fleet-memory reserve floor on Pi-class hardware — and record both in the PR body; P4's soak criteria reference these numbers.

**Validation:** house-mandatory empirical loop — `lib/validate-bot-change.sh` gains a claudron scenario with a **skip-guard** (skips cleanly when claudron/vault absent on the dev machine); mechanical asserts here (fragment composes, env lands, permissions render, doctor matrix: no-claudron-anywhere → all silent / mounted+CLI-missing → fail / mounted+healthy → pass / **vault-present+binary-absent → mount skipped with loud warning**); a spun-up test bot lists the five tools in-session (observed, cited).

**Standalone value:** an operator can mount Claudron on any bot with one fleet.yaml line and get correct env, permissions, doctor coverage, a quickstart that owns their first hour, and a measured cold-start + RSS cost — opt-in, zero effect on claudron-less fleets.

### Phase 3 (M): Protocol cutover — the MCP overlay — gate: P2 landed + dogfood fleet vault-wired

> **⛔ RE-SCOPED by Decision C (2026-07-18).** No `mcp__claudron__*` protocol cutover — bots consume the vault through **clauDNA's `/recall` (query-before) + `/capture` (write-after)** over the `claudron` CLI, already installed fleet-wide. Claudlobby's protocol work collapses to the **#561 navigate-vs-query protocol** (navigate the filesystem for config; query `recall`/`lookup` for knowledge; never hardcode the hub path) + the read-side trust note (draft = unverified) + the vault-hygiene guardrail (3e still stands). 3a/3b's `claudron_lookup`/`claudron_write` *tool* text is superseded by the clauDNA verbs; the routing-contract nuances (`suggest_*`, `retry_later`, draft-trust) migrate into clauDNA's `/capture` guidance, not a Claudlobby overlay. **This work is not MCP-gated and can proceed once the dogfood vault is wired.**

**Dependencies:** ~~P2 fragment~~ → the clauDNA door (ships today) + #561; 1d's fallback text as the base. **Blocks:** P4 soak (needs bots actually querying/writing — via clauDNA verbs + the 1e wedge).

**Steps:**
- [ ] 3a. **`dispatch.md` preflight overlay**: vault-wired fleets — `claudron_lookup` scoped to the target repo/fleet before dispatch, include hits in the dispatch prompt (door-consistent with 1e's mechanical injection if F7 locked: the protocol documents what the helper does mechanically, and covers the manager-judgment cases the helper can't). Claudron-less fallback text (from 1d) stays. Manager monitoring: `claudron review` forward-ref stays conditional.
- [ ] 3b. **`shared-documentation.md` write-after overlay**: post-work capture via `claudron_write`, documenting the full routing contract — `{action: created|updated|suggest_update|suggest_supersede}` (a `suggest_*` response means *follow the suggestion, don't force-create*; the engine now enforces one-file-per-topic) **and `retry_later`** (lock contention: retry once after backoff; on second failure, record the finding to bot memory and move on — never drop silently, never hammer). The 5-doc read cap becomes a lookup `limit` guidance. **Read-side trust:** treat `maturity: draft` hits as unverified input; prefer `verified`/`canonical` for decisions. Lifecycle section aligned to supersession-over-deletion (`expires` = review trigger, `superseded_by` = terminal pointer).
- [ ] 3c. **`precedent-check` skill**: its direct directory scans (`shared/decisions/`, `shared/planning/active/`) gain a `claudron_lookup` first path when the vault is wired; filesystem fallback kept.
- [ ] 3d. *(moved to P1d — dead-text cleanup no longer waits for Claudron.)*
- [ ] 3e. **New guardrail `library/guardrails/vault-hygiene.md`**: never write secrets, tokens, `runtime/` paths, or operator PII into vault notes; bot writes default to `maturity: draft`; respect `suggest_update`/`suggest_supersede`/`retry_later` routing; the vault is tenant-owned — treat it as the operator's private repo. Rationale notes the read-side mirror (draft ≠ trusted).
- [ ] 3f. **Composition + convergence check:** vault-wired bots' CLAUDE.md carries the claudron protocol + guardrail; claudron-less bots carry the fallback text only; **and clauDNA's capture skills and this protocol text name the same destination for the same finding** (post-E3, clauDNA's `/remember`/`/learn` prefer the Claudron engine — verify no double-write or divergent-destination instructions ship together).

**Validation:** split explicitly — *mechanical* (harness): protocol/guardrail composition per bot class asserts in `validate-bot-change.sh`; *observed* (manual Deliver→Observe run, cited in the PR body): a dispatched task on a vault-wired bot shows a `claudron_lookup` call before work and a `claudron_write` after (transcript/`tool_call` events via bot-vitals), and the written note passes `claudron validate`. LLM tool-choice is probabilistic — the observed run is the house empirical loop, not a CI gate.

**Standalone value:** the fleet's knowledge conventions describe machinery that exists, for both fleet classes; bots get typed, permission-gated tools with honest failure-mode guidance.

### Phase 4 (M): Graduation to fleet default — gates: soak evidence + mission approval

> **⛔ RE-SCOPED by Decision C (2026-07-18).** Graduation was "default-mount the MCP fragment," which tripped the **F5 mission-approval gate** (PROJECT_MISSION.md :63/:67/:68 — "new MCP server in the default template"). **Under Decision C there is no new MCP server in the template → the F5 approval artifact is largely moot.** What "graduation" means now: the **vault wiring is on by default when a vault resolves** (the `.claudron` bridge → `CLAUDRON_VAULT_PATH` → clauDNA door + the 1e wedge), an operator-config default, not a mounted server. The soak (below) still validates the *behavior* (bots query/write against the vault); it's just no longer gating an MCP-server-in-template approval. F5's generic `mcp_exclude` mechanism survives only if F8 is ever un-parked.

**Dependencies:** P3 landed. **Soak precondition:** the dogfood vault holds a minimum corpus (≥25 notes spanning ≥2 repos — lookups against an empty vault prove nothing). **Soak evidence** (≥2 weeks, dogfood fleet; instruments named): (i) *compliance floor* — `claudron_lookup` calls on ≥50% of dispatched tasks (from `claudron status` counters / events vs dispatch-log count); (ii) *impact* — ≥3 observed instances where a preflight hit was included in a dispatch and visibly used in the worker's output (transcript/events — the saved-me-tally analog; impact, not just activity); (iii) *latency* — dispatch→first-activity P95 from `data/events/*.jsonl` within 10% of the pre-mount baseline window; (iv) *resources* — cold-start and fleet RSS deltas within the 2f budgets (fleet-memory-check); (v) *hygiene* — a weekly sweep of vault commits (secret patterns, `runtime/` paths, PII) returns zero hits — named checker, run by the fleet owner or a dispatched bot task; (vi) *liveness caveat* — flat counters trigger a doctor run before being read as non-adoption (MCP server crashes fire `PostToolUseFailure`, which bot-vitals does not capture — a dead server and an unpersuaded bot look identical in counters alone). **Soak-fail branch:** stay opt-in; if the floor failed for mechanism reasons, mechanical injection (F7's door) becomes the default query path and prose demotes to guidance; re-run the soak. No silent indefinite soak.
**Approval:** this phase is the PROJECT_MISSION.md approval artifact for **:68** (new MCP server in the default template), and the F5 lock also explicitly discharges **:63** (the host-level `claudron[mcp]` install becomes a de-facto conditional prereq for vault-resolving fleets) and **:67** (MCP-config provisioning changes) — **granted: F5 locked (a) by the fleet owner 2026-07-07, presented with the n=1 honesty line** (soak evidence is the author's own fleet; graduating serves future vault-wired operators). The lock ratifies the mechanism; the soak bundle above still gates the rollout — a failed soak lands in the fail branch, not in graduation.

**Steps:**
- [ ] 4a. Implement the F5-locked mechanism (lean: auto-mount when the fleet resolves a vault **and** `claudron-mcp` is resolvable at compose time (`shutil.which`); one loud validator warning + skipped mount otherwise; per-bot opt-out via a **generic** `mcp_exclude: [claudron]` list honored by the merge/auto-mount path — one reusable rule, not a claudron-only boolean). Unit tests: red-green on exclusion, binary-absent skip, and the defaults-first-wins shadowing note.
- [ ] 4b. Update `fleet.yaml.example`, the `new-bot` wizard (its MCP list derives from `library/mcp/*.json` — mark claudron "auto-mounted (vault detected)" so operators aren't asked to hand-configure a default), and the seed fleet; `fleet-yaml-schema.md` documents the mechanism + `mcp_exclude` in the merge-rules section.
- [ ] 4c. Extend the harness scenario to assert the default path (fresh fleet with a vault + binary → bot queries/writes with no explicit `mcp:` entry; vault without binary → skip + warning).
- [ ] 4d. Close out #251 (all four of its items landed or superseded: fragment ✓ P2, protocol ✓ P2/P3, default mount ✓ here, bot.conf wiring ✓ pre-existing) — closure is claudlobby-owned, here and only here. Publish the soak counters (dedup hits, routed updates, lock contention) as a comment on Claudfather/Claudron#17/#14 — likely the ecosystem's first ≥2-writer field data on their F8 write-chokepoint bet.

**Validation:** the 4c scenario observed end-to-end and cited; `claudlobby validate` on a vaultless fleet shows zero claudron warnings; the F5 lock record (Decision Forks) names :63/:67/:68 — done.

**Standalone value:** a new operator with a vault gets ecosystem-citizen bots out of the box; an operator without one sees no change — the mission's "opt-in to remain local-first" honored in the default itself.

### Phase 5 (M): Librarian standing job — gate: first Claudron release shipping `claudron review --json`

**Dependencies:** the gate capability (E5 PR3; 0.5.0 in default order); P2 (doctor/install story). Independent of P4. On gate-fire: re-anchor the `review --json` output shape against the shipped tag. **Blocks:** none.

**Plan decision (was Fork F6, demoted cycle-1 — one live option):** the sweep dispatches to an explicit `claudron.librarian_bot` named in fleet.yaml, validator-checked against the bot list; unset → the job composes dormant and skips with a note, never errors. Reusing `sweep.owner_bot` would couple two unrelated jobs' ownership; hardcoding the manager would make the least-interruptible session the default target.

**Steps:**
- [ ] 5a. **`lib/claudron-review-sweep.sh`** mirroring `code-audit-sweep.sh`: no-LLM; resolves the fleet vault (bridge or bot.conf), runs `claudron review --json`; empty queue → exit 0 silently; non-empty → dispatch a summarized worklist to the librarian bot's live session via the shared dispatcher, emit `librarian_dispatched`/`librarian_skipped` events. **Busy-skip wart handled explicitly:** `bot-sweep-cron.sh` exits 0 on both dispatch and busy-skip — per the 2026-06-12 precedent ("extend the shared dispatcher, don't fork it"), and now that N=2 consumers need the distinction, give `bot-sweep-cron.sh` a distinct busy-skip exit code rather than copying code-audit's log-grep workaround. The dispatched prompt instructs working the queue with the claudron CLI: resolve expired/stale per supersession-over-deletion, apply `promote` only per the vault's curation rules, never delete.
- [ ] 5b. **Config:** fleet.yaml `claudron.librarian_bot` → composed into the owner's `bot.conf` (`LIBRARIAN_BOT`, the `sweep:` block pattern); validator: must name a fleet bot.
- [ ] 5c. **Job:** `system.yaml defaults.jobs.claudron-review`, `enroll: false` (dormant default — opt-in like `code-audit-sweep`), weekly `OnCalendar` weekday+HH:MM (launchd-safe), `type: oneshot`.
- [ ] 5d. **Validation harness:** scenario (with skip-guard) — seeded stale note in a throwaway vault → sweep run → dispatch observed in the librarian's session → queue item resolved and `claudron review` empty on re-run.
- [ ] 5e. Docs: `claudron-integration.md` librarian section; cross-link Claudron E5's acceptance criterion (their "drained weekly by a claudlobby bot" line — this job is that bot).

**Validation:** 5d observed and cited; both init systems' units compose (`claudlobby generate` + timer manifest shows the dormant entry).

**Standalone value:** the vault gets a standing curator the day Claudron ships the queue — the fleet-native answer to "a queue without a worker is a dashboard" (their E5's own risk line). If scope must shrink, this phase is the cut line (mission-tangential by strict reading; boundary-correct division of labor — curation semantics stay Claudron-side).

### Deferred: events.jsonl fleet observability

`.claudron/events.jsonl` (dedup hits, routed updates, lock contention) is Claudron-owned instrumentation, summarized by `claudron status` — which P2d's doctor check and status panel already surface. A fleet-pulse integration (alerting on lock-contention/dedup anomalies) is **deferred with a named trigger**: the first fleet with ≥2 actively-writing bots (the same milestone Claudron names for validating the F8 write-chokepoint bet). Building a second reader of their gitignored internal file before multi-writer traffic exists would be speculative plumbing against an uncommitted format.

## Decision Forks

### Fork F1: `[vault]` pin strategy
- **Context:** `pyproject.toml` pulled claudron from unpinned git HEAD at plan time (introduced by #300 when the vault layer was re-integrated as an extra); Claudron had no tags/releases then. **Shipped since (F1 lock):** pinned to tag `@v0.2.0` (`pyproject.toml:21`).
- **Options:** **(a)** pin to current main SHA now, move to the version tag (or PyPI range) at their first release, bump per Claudron release after claudlobby's vault-mode tests pass; (b) wait for their first release and pin then (unpinned until an external event); (c) stay unpinned (status quo).
- **Lean:** **(a)** — determinism now, one tiny PR; the machine-readable compat constants (1c) make every later bump mechanical. (b) leaves installs nondeterministic through Claudron's highest-churn window; (c) is the bug.
- **Ratifier:** fleet owner. **Status:** **locked (a)** — fleet owner, 2026-07-07 (ratified in-session; recorded in the fork-locks PR).

### Fork F2: `_env_contract` handling for composer-emitted vars
- **Context:** Claudron E3 specs the fragment with `_env_contract` mapping `${CLAUDRON_VAULT_PATH}` — but that var is composed into `bot.conf`, not operator-supplied; as-spec'd it false-alarms doctor (warn or fail depending on tier) and scaffolds a misleading `.env` stub. Two sibling composer-emitted vars (`CLAUDNA_VERSION`, `CLAUDOSSEUM_TENANT_ID`) make the wrinkle generic.
- **Options:** (a) fragment omits the var from `_env_contract` — zero code; 2d's `check_claudron` independently covers the pairing, so the *functional* loss is only fragment self-documentation; named trigger to adopt (b) when a second composed-var fragment actually appears; **(b)** extend our fragment contract with `"provided_by": "composer"` — scaffolding and `check_env_vars` skip it entirely; the per-bot check lives in `check_claudron`, resolved in-memory (never by parsing generated files); README documents it with `CLAUDRON_VAULT_PATH` as the worked example; (c) teach doctor to merge `bot.conf` for all env checks (heavier, broader semantics change than needed).
- **Lean:** **(b)** — the cost is one metadata field in a contract we own; the claudosseum fragment is a concrete (not speculative) second instance; (a) remains a respectable fallback the ratifier declined in favor of (b) — the Claudron#17 feedback comment stated the lean; the lock is confirmed there.
- **Ratifier:** fleet owner. **Status:** **locked (b)** — fleet owner, 2026-07-07; lock confirmed on Claudron#17 so their fragment PR is authored with `provided_by: "composer"`.

### Fork F3: MCP server command + install story
- **Context:** `claudron-mcp` is a Python console script; nothing like npx exists for it, and `_global_binary`/warm-cache/check-npx-cache are npx-only. Bots need the command resolvable on their session PATH — verified: `start-bot.sh:49` already puts `$HOME/.local/bin` (the pipx/uv-tool target) on every bot session PATH.
- **Options:** **(a)** host-level `pipx`/`uv tool` install of `claudron[mcp]` via an explicit `setup-system --with-claudron` opt-in (one install serves all bots; deterministic cold-start; doctor-checkable; the step installs pipx/uv itself if absent); (b) `uvx --from 'claudron[mcp]' claudron-mcp` in the fragment (npx-analog ephemerality, but adds a `uv` prereq and re-creates the N-bots cold-start dogpile #474 just fixed); (c) absolute-path knob in fleet.yaml (violates the no-hardcoded-paths lib rule).
- **Lean:** **(a)**. Fragment command is `claudron-mcp`, bare.
- **Ratifier:** fleet owner. **Status:** **locked (a)** — fleet owner, 2026-07-07 (ratified in-session; recorded in the fork-locks PR).

### Fork F4: INDEX.md convention disposition
- **Context:** Ratified 2026-05-11 as the consumer phases of a plan whose in-repo producer never shipped; zero adoption in this repo (its own doc admits it). Consumers: `dispatch.md`, `shared-documentation.md`, **`templates/claude.md.j2:117`** (composed into every bot), and — the cross-repo fact — the fleet-default clauDNA plugin ships `/claudna:index` as the convention's live producer (auto-run by `/claudna:learn`; clauDNA#36 extends it). `precedent-check` does *not* read INDEX.md (the ADR's status note overcounted).
- **Options:** **(a)** retire — decision doc → `superseded` pointing at `claudron_lookup` + this plan; claudron-less fallback becomes plain directory+frontmatter scans (no producer needed); **conditional on**: (i) a clauDNA handoff filed (comment/dedup on clauDNA#36 + a deprecation-path issue in their #106/#107 style: `/claudna:index` and the INDEX.md write path in learn/reflect/publish go vault-aware or retire with the convention — coordinated with clauDNA's companion epic), and (ii) the live-fleet INDEX.md sweep (P1d-i) resolving any existing runtime files; (b) keep scoped to claudron-less fleets and finally ship an `/index` producer here (build new machinery for a convention with zero demonstrated demand); (c) status quo (dead text keeps instructing bots to read files that don't exist).
- **Lean:** **(a)**. Reversing a ratified decision needs its ratifier's explicit sign-off — hence a fork, and this would be the repo's first decision-doc supersession (`superseded` is the ratified terminal status; the fleet owner superseding the original owner is a novel but reasonable authority call, surfaced here rather than assumed).
- **Ratifier:** fleet owner (original ADR owner: clog — fleet owner supersedes). **Status:** **locked (a)** — fleet owner, 2026-07-07, exercising supersession authority over the original ADR. Retirement **executes** only after both named preconditions: the clauDNA handoff filed and the live-fleet INDEX.md sweep resolved (P1d).

### Fork F5: Graduation mechanism (the mission :63/:67/:68 gate)
- **Context:** #251 asks for "default new bots to having Claudron mounted (opt-out via fleet.yaml override)"; `defaults.mcp` already merges into every bot but is defaults-first-wins with no per-bot exclusion (a bot re-declaring `claudron` is silently shadowed); the mission gates default-template MCP servers on approval; and a `.claudron` bridge resolves a vault even where claudron was never installed, so vault-presence alone must not trigger the mount.
- **Options:** **(a)** conditional auto-mount: fleet resolves a vault **and** `claudron-mcp` resolves at compose time → every bot gets the fragment unless excluded via a **generic per-bot `mcp_exclude` list** (new, reusable for any defaults-mounted server); vault-or-binary absent → loud warning, no mount, zero behavior change (local-first clean disable by construction); (b) documentation-only: ship a `defaults.mcp: [claudron]` recipe in `fleet.yaml.example` + new-bot wizard (zero code; all-or-nothing per fleet; vaultless copiers trip the validator warning; can ship early at P3-time as an interim while (a) waits for demonstrated need — the staged path); (c) template-literal reading: only `new-bot` scaffolding adds it (existing bots never change; weakest fulfillment of #251).
- **Lean:** **(a)**, optionally staged through (b) — the ratifier should weigh that today's beneficiary population is approximately one known vault-wired fleet, and n=1 soak evidence may argue for holding (a) until a second operator demands it. The `[FORK-LOCK F5]` comment discharges mission :63, :67, and :68 by name.
- **Ratifier:** fleet owner. **Status:** **locked (a)** — fleet owner, 2026-07-07, presented with the n=1 honesty line and the staged alternative. **This lock is the mission approval, discharging PROJECT_MISSION.md :63, :67, and :68 by name.** P4 still implements only after the soak bundle passes — the lock ratifies the mechanism, the soak gates the rollout.

### ~~Fork F6~~ — demoted to a P5 plan decision (cycle-1; one live option — see Phase 5).

### Fork F7: Query-before transport + interim CLI wedge
- **Context:** Claudron v0.1 already ships CLI `lookup`; CLI and MCP are two doors on the same engine, and the query-before *behavior* (mission :11) is transport-agnostic — only the MCP mount is gated on their E3. Meanwhile the plan's adoption story otherwise rides protocol prose, the mechanism class whose failure mode (instructed-but-never-done) this same plan buries in P1d; Claudron's own E3 acceptance bets on tools discovered in-context, *not* convention text. A deterministic option exists: the dispatch path is mechanical — `dispatch-task.sh` can shell `claudron lookup` and prepend hits to the dispatched prompt, guaranteed rather than hoped-for, mirroring how Claudron's personal wedge injects recall via hooks rather than prose.
- **Options:** **(a)** adopt the mechanical wedge now (P1e): env-knobbed CLI `lookup` injection in `dispatch-task.sh`, off by default, dogfood fleet on — deterministic query-before from the P1 pin onward; MCP tools (P3) layer on for in-session use; write-after stays MCP/write-lock-gated (no pre-lock concurrency exposure); generates the fleet query-volume evidence Claudron's G1 wants; (b) decline — protocol prose + MCP only; mechanical injection reserved as the named soak-fail remediation (P4's fail branch); (c) mechanical injection only, never protocol text (rejected: managers also need judgment-call queries the helper can't anticipate, and in-session tools need discovery text).
- **Lean:** **(a)** at S effort (a spike + one script knob) — it converts the plan's biggest external risk (fleet evidence waiting on an evidence-gated option) into early evidence either way, and gives P4's soak a deterministic floor. Costs stated honestly: a third query door to document, Bash-level permissions instead of `mcp__claudron__*` gating for the helper path, and a v0.1-lookup-vs-fleet-vault spike that may find gaps (filed on #17 as feedback, per the boundary).
- **Ratifier:** fleet owner. **Status:** **locked (a)** — fleet owner, 2026-07-07 (ratified in-session; recorded in the fork-locks PR). P1e is go.

### Fork F8: Consumption door — clauDNA CLI-skill vs Claudlobby MCP fragment *(this plan's F8; distinct from Claudron's F8 field-research milestone in the Deferred row)*
- **Context:** The epic's P2/P3 assumed bots consume the vault via a Claudlobby-authored MCP fragment mounting Claudron's E3 server. But E3 has no active work (only `v0.2.0` tagged), and clauDNA ships the query/write door **CLI-based on every bot today** (`/claudna:claudron` lookup/status, `/recall`, `/capture` — "clauDNA ships no MCP servers; this engine IS the CLI; if MCP is configured, the same engine"). A `/weigh-development-paths` pass evaluated three options across 7 dimensions.
- **Options:** **(A)** Claudlobby MCP fragment (`library/mcp/claudron.json` + E3 mount + protocol cutover); **(B)** clauDNA CLI-skill door + Claudlobby wiring only; **(C)** hybrid — B now, A parked as demand-gated.
- **Lean:** **(C).** B wins on elegance/DRY/separation/existing-patterns (one engine, one door, matches the ecosystem boundary "Claudlobby does not define skills"); A's only unique value (in-session tool discovery + `mcp__claudron__*` per-tool permission gating) is gated on an unshipped server, so C parks A rather than deletes it — pull it forward only when **E3 ships AND the fleet wants per-tool vault-access permission scoping** (the #644 tie-in) or a non-clauDNA MCP consumer appears.
- **Ratifier:** fleet owner. **Status:** **locked (C)** — fleet owner, 2026-07-18. Full matrix: `documentation/decisions/2026-07-18-claudron-consumption-door.md`. Executed cross-repo — Claudfather/Claudron#60 marks their `03-mcp-server.md` `status: deferred`. Consequence: P2's fragment half + P3's MCP overlay are parked; the wiring (#560–564) and #561 protocol are the active path.

## Risks

| Risk | Sev | Impact | Mitigation |
|---|---|---|---|
| Claudron G1 re-orders the tail or it stalls (their own base-rate risk: solo maintainer) | high | P2–P5 wait indefinitely | Capability-phrased gates; P1 (incl. dead-text cleanup + F7 wedge) is unconditional and valuable alone; #251 stays open with correct fallback text — nothing dangles |
| Claudron's fragment PR carries `Closes #251` (their E3 acceptance says "#251 closes") | high | the epic's tracking spine breaks mid-flight from another repo's merge button | the divergence ships in the filing-time #17 comment (2a); the 2b review checklist checks their PR body; closure is claudlobby-owned at P4d |
| Adoption rides prose and quietly doesn't happen (the INDEX.md failure class) | high | machinery becomes permanent maintenance surface while the behavior stays theater | F7 mechanical wedge (deterministic floor); P4 numeric compliance floor + impact criterion + explicit soak-fail branch — the gate is falsifiable |
| clauDNA keeps producing INDEX.md while our protocols retire it | med | composed bots get contradictory instructions (protocols vs installed skills) | F4 lock conditional on the clauDNA handoff; 3f convergence check; live-fleet sweep before supersession |
| Fragment PR authored against the un-extended contract | med | churn on Claudron's side, re-review | F2/F3 feedback posted on #17 at epic filing time, before their PR exists; locks since confirmed there — the authoring shape is settled |
| Cold-start / RSS regression per bot on Pi-class hardware | med | violates the resource-conscious principle exactly at fleet-wide graduation | F3(a) host-level install; 2f numeric cold-start **and RSS** budgets; soak criterion (iv); mount stays opt-in until P4 |
| Bots write secrets/PII into a tenant vault | med | operator data leak into their own repo — still a trust break | 3e guardrail composed into every vault-wired bot; soak criterion (v) with a named weekly checker |
| Doctor/validator noise for claudron-less fleets | low | alarm fatigue, "ecosystem tax" perception | every check conditional on a claudron surface; P2 validation asserts the all-silent case |
| Write-path behavior under real fleet load unvalidated (Claudron F8) | med | protocols instruct a write pattern whose concurrency story is per-writer-proven only | inherited honestly: claims scoped per-writer; `retry_later` guidance in 3b; deferred observability trigger is the ≥2-writer milestone; P4d publishes the first field data upstream |
| Vault taxonomy drift between two structure writers | low | composer and `claudron fleet add` disagree inside the tenant's vault | P2c(iv) removes the composer's vault-mode mkdir (handover, not freeze); 1a(v)/2b taxonomy reception checks; deltas filed on #17 |
| Plan anchors rot across month-scale gates (repo ships daily; #490 already drifted the counts) | med | phases implement against stale line numbers/assumptions | standing anchor re-validation rule at every gate-fire (>~4-week staleness window); re-anchor steps in 2b/3/P5 |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with | Gate |
|---|---|---|---|---|
| P1 | M | — (1d: clauDNA handoff; forks locked) | P2–P5 planning | none |
| P2 | M | P1c; #490 coordination clause | P5 prep | first Claudron release shipping the MCP server |
| P3 | M | P2, P1d | — | dogfood fleet vault-wired |
| P4 | M | P3 + soak bundle | P5 | soak passes (F5 locked — mission :63/:67/:68 discharged) |
| P5 | M | P2 | P3, P4 | first Claudron release shipping `review --json` |

P1 Blocks: P2 (1c), P3 (1b/1d). P2 Blocks: P3, P4. P3 Blocks: P4. P4 Blocks: none. P5 Blocks: none. Critical path: P1 → P2 → P3 → P4; P5 forks off P2. The success floor (P1–P3) sits entirely on the near side of Claudron's evidence-gated tail.

## Companion Plans

- **Claudron roadmap** (consumed contract): `Claudfather/Claudron` `documentation/plans/2026-07-07-claudron-roadmap/00-overview.md` (+ `01-schema`, `03-mcp-server`, `05-lifecycle`) — PR #13, EPIC #14, E3 #17.
- **Claudron vault-structure contract** (structure-defining counterpart — Claudron owns the folder-structure SSOT this epic conforms to; ratified 2026-07-09): `Claudfather/Claudron` `documentation/plans/2026-07-09-vault-structure-contract/00-overview.md` (P1 `VAULT-STRUCTURE.md` · P2 `validate`/`init` enforcement · P3 conditional fleet-scoped consumption) — PR #33; its Claudlobby-side children are #560–#564 (overlay conformance, navigate-vs-query protocol, mount wiring, pilot-fleet dogfood, tier-set drift). Its forks are numbered F1–F7 too but cover *structure* decisions (vault residence, flat layout, hub-name…), distinct from this plan's *consumption* forks.
- **Goal-aware fleet portfolio** (merge adjacency): `documentation/plans/2026-07-06-goal-aware-fleet-portfolio.md` (branch `plan/goal-aware-fleet-portfolio`; its P2 is open PR #490). Its F2 residual-risk line ("vault sync carries the registry — future Claudron wiring") is future work owned there, not here.
- **clauDNA companion epic** (cross-repo coordination for F4 and the capture-path convergence): planned by clauDNA's own forge session (the Claudron roadmap's Prompt B); the F4 clauDNA handoff item lands in their backlog and is cross-linked from this epic.
- Prior-art exemplars this plan mirrors: `2026-06-12-codify-rolling-code-audit-sweep.md` (P5's shape + the busy-skip wart), `2026-06-09-system-defaults-tier.md` (config-tier introduction pattern).

## Issue mapping (dedup into the existing backlog)

One phase = one tracking issue. **All filed 2026-07-07.** Fork ratifications: recorded in this doc's Status lines via the fork-locks PR (#508 merged before the lock ceremony).

| Artifact | Status |
|---|---|
| **EPIC** | **#509** — links Claudron#14/#17, umbrella #266, this doc; lists P1–P5 + the floor |
| P1 | **#510** |
| P2 | **#511** |
| P3 | **#512** |
| P4 | **#513** |
| P5 | **#514** |
| #251 | **no new issue for its scope** — comment posted with the plan link + item mapping (fragment → P2, protocol → P2/P3, default → P4, bot.conf wiring → done); closes at P4d, claudlobby-owned |
| #266 | comment posted linking the EPIC (the Claudron limb of the compound play) |
| Claudron#17 | comment posted: F2/F3 contract feedback + fragment key/command/`provided_by`/no-`_global_binary` + **the #251 reference-don't-close divergence**; lock confirmation follow-up posted after ratification |
| clauDNA backlog | handoff item per F4(a)(i) — filed via/with clauDNA's companion epic session; **1d execution waits on it** (F4 itself is locked) |

Labels: `planning` (created for this epic, matching the sibling house style), `product-vision`, `play:ecosystem`; `two-hop` on the cross-repo children (P2, P4). CEO-lens note, dispositioned: P5's child files now with its gate in the body (house DoD files the set; the gate line keeps the backlog honest).

## What NOT To Do

- **No local schema fork.** `SCHEMA.md` deltas are feedback on Claudron#15/#17, never edits to our conformance profile that diverge from SSOT. The status-vocabulary ≈-mapping is transitional, not a dialect to grow.
- **No INDEX.md producer.** F4's option (b) exists to be rejected explicitly, not built by default.
- **No vault structure invention.** Claudron `init`/`fleet add` owns vault layout; P2c(iv) removes the composer's vault-mode mkdir rather than freezing it.
- **No transport blending.** Bots get MCP tools; host jobs and dispatch helpers get the CLI. Don't wire the CLI into bot protocol text or the MCP into cron jobs.
- **No MCP tool-surface redesign.** The five-tool v0.1 surface is Claudron's approval-gated contract; gaps → comments on Claudron#17.
- **No hosted dependency, no corpus storage.** The vault is a tenant-owned local git repo; `runtime/`, `.env`, and operator PII never enter it (bright line + 3e guardrail).
- **No pre-gate implementation.** P2–P5 do not start on plan approval — they start on their named capability gates.
- **Don't close #251 early — and don't let anyone else close it.** It closes in P4d; Claudron's fragment PR references it (checked in 2b).

## Ironclad Cycle 1 — Findings Disposition

| Finding (lens) | Disposition |
|---|---|
| **Blocker:** #251 closure race with Claudron E3 acceptance (adversarial) | Folded — 2a comment carries the divergence; 2b checks their PR body; risk row added |
| Version-number gates vs ordinal releases (align/adversarial/engineering) | Folded — capability-phrased gates throughout; compat table keys on capabilities |
| Auto-mount on vault alone → fleet of broken mounts (devex/adversarial) | Folded — F5(a) requires binary at compose time; loud warn + skip; validation matrix case added |
| Soak measures mechanism, not outcome; criteria untestable (first-principles/ceo/plan-health) | Folded — corpus precondition, numeric floor, impact criterion, named instruments incl. RSS, liveness caveat, soak-fail branch |
| Deterministic/interim query wedge unweighed (adversarial/ceo) | Folded — new Fork F7 + P1e; dogfood-fleet designation owned there |
| clauDNA ships the INDEX.md producer (precedent) | Folded — F4 lock conditional on clauDNA handoff; consumer inventory corrected (j2 template in, precedent-check out); live-fleet sweep added |
| Effective-config injection point unspecified (engineering) | Folded — `apply_ecosystem_defaults` named; agreement unit tests |
| F2 doctor semantics underspecified (engineering/devex) | Folded — skip-in-env-checks + per-bot in-memory check in `check_claudron`; doctor `--json` flagged net-new |
| Dead-text cleanup needlessly Claudron-gated (cost-benefit) | Folded — moved to P1d (P1 → M); P3 is the pure MCP overlay |
| Companion Plans / skeleton (plan-health) | Companion Plans added; single-doc shape kept **deliberately** (repo exemplar convention; filed via direct PR, not the publish pipeline) — dispositioned, not adopted |
| Success floor unstated (cost-benefit) | Folded — D10-mirror line in Goal |
| Mission :63/:67 adjacent approvals (align/adversarial) | Folded — F5 lock discharges all three by name |
| packs-rung dead text; #490 hard gate; vault-mkdir freeze; retry_later; read-side trust; harness split; unit tests; compat SSOT; validator message; provenance comment; setup-system opt-in shape + pipx prereq; quickstart ownership; wizard marker; latency instrument; mcp_error blind spot; busy-skip wart; SHARED_SUBDIRS wording; vault-path model Q; n=1 honesty; F1/F6 ceremony; risk severities; sequencing asymmetries; P3 issue home; soak counters upstream; status vault panel; transport principle; claudron history; vocabulary-dialect Q | All folded where they touch the plan text above (F6 demoted; F1 kept — dependency-semantics locks are cheap and this one changes a shipped file's install behavior) |

## Context

Area: ecosystem integration (compositor, protocols, lib jobs, docs) · Effort: M+M+M+M+M across five gated phases (success floor = P1–P3) · Risk: medium (external release train; mitigated by capability-gating and the unconditional P1) · Priority: high (mission sprint #4; the ecosystem-citizen play's memory limb).
