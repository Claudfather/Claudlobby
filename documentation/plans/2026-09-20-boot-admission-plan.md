---
title: Boot admission and supervision consolidation — plan (epic)
type: plan
status: draft
date: 2026-09-20
spec: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
issue: "#1573"
---

# Boot admission and supervision consolidation — plan

## Summary

Two PRs deliver the spec: PR A builds the one truth (`BootPolicy` into `bot.conf`), the renderer boundary (`SupervisionSpec` and its round-trip test), the supervisor adapter with its ratchet, and the three amplifier fixes (`MCP_TIMEOUT`, a wall-clock readiness ceiling that names why, plugin updates once per boot). PR B moves the boot path onto it: the priority admission gate in `start-bot.sh`, the platform-neutral boot-progress marker, keepalive and the spin/install scripts on the adapter, the systemd stagger and its rung reader retired, and the reboot proof. Mission line: "long-running is the precondition for the rest" — a fleet that cannot survive a host reboot without a storm cannot hold a goal.

## Architecture

Three layers, one owner each, one test at each boundary (spec §5): the core process (`start-bot.sh`, `keepalive.sh`, the lib doors; policy from `bot.conf`, state from markers), the adapter (`lib/supervisor.sh`, five verbs, two spellings), the renderer (`SupervisionSpec`, two writers, idioms only). The gate is the single bring-up mechanism on a host; the marker is the single boot-progress signal; the ratchet keeps every future supervisor call inside the adapter.

## Decision Forks

Design forks are locked in the spec (§11). These are implementation forks.

- **F1 — Adapter placement.** Context: the ratchet needs a greppable boundary. Options: (a) a new `lib/supervisor.sh` sourced by `lib-common.sh` at source time; (b) a delimited section inside `lib-common.sh`. Lean: (a) — a file is a boundary a test can name; a section is a convention. Ratifier: operator. Status: locked (a), 2026-09-20, ratified with the spec's adapter section.
- **F2 — Gate state location.** Options: (a) `$CLAUDLOBBY_ROOT/state/boot/` (host-wide because the root is host-wide; `state/` already holds the plane db and fleet state); (b) `$TMUX_TMPDIR`; (c) a per-user dir. Lean: (a). Ratifier: operator (2026-09-20, with the design walk). Status: locked (a) — the only host-wide directory the runtime already owns.
- **F3 — Readiness ceiling coupled to the MCP timeout.** Context: a poller that comes up at 150 s under a 180 s MCP timeout would be logged `TIMEOUT` by a 90 s ceiling and alerted on. Options: (a) derive `ready_timeout_s = max(90, mcp_timeout_ms // 1000 + 20)` inside `BootPolicy`; (b) two independent numbers. Lean: (a) — one truth, no way to set them inconsistently. Ratifier: operator (2026-09-20, with the design walk). Status: locked (a) (this document; the spec's table row for `ready_timeout_s` reads "derived" once folded).
- **F4 — Precedence between the composed value and an env override.** Context: `start-bot.sh` reads `${RC_READY_TIMEOUT_S:-90}` and `bot.conf` is sourced under `set -a` inside it. Options: (a) the composed `bot.conf` value wins; the env var stays as a fallback when the key is absent (old `bot.conf`); (b) env wins. Lean: (a) — the truth is the composed file; an operator changes `system.yaml` and regenerates. Ratifier: operator (2026-09-20, with the design walk). Status: locked (a).
- **F5 — Ratchet allowlist granularity.** Options: (a) `(script, count)` pairs; (b) per-line anchors. Lean: (a) — shrinks freely, no churn on unrelated edits. Ratifier: operator (2026-09-20, with the design walk). Status: locked (a).
- **F6 — Round-trip parser for the systemd unit.** Options: (a) a small line parser tolerant of repeated keys (`Environment=` appears twice), sections by `[Name]`; (b) `configparser` (rejects duplicate keys). Lean: (a). Ratifier: operator (2026-09-20, with the design walk). Status: locked (a).
- **F7 — Harness scenario vs a rehearsal script for the gate.** Options: (a) a new check block in `lib/validate-bot-change.sh` (runs on Linux CI and in the macOS harness run); (b) a `rehearse-boot-admission.sh` sibling. Lean: (a) — the harness is the runtime gate every `lib/` change already pays for. Ratifier: operator (2026-09-20, with the design walk). Status: locked (a).
- **F8 — `service_is_starting` signature.** Context: it takes a service name and returns 1 off Linux; the marker lives under the bot dir. Options: (a) `service_is_starting <service> [bot_dir]`, marker rung first when a bot dir is given; (b) a separate `bot_is_booting <bot_dir>` and keepalive calls both. Lean: (a) — one question, one door; keepalive already calls it with `BOT_DIR` in scope. Ratifier: operator (2026-09-20, with the design walk). Status: locked (a).

## Companion Plans

- PR A: `documentation/plans/2026-09-20-boot-admission-pr-a-truth-and-adapter.md`
- PR B: `documentation/plans/2026-09-20-boot-admission-pr-b-the-gate.md`
- Follow-ups under the ratchet (not planned here): migrate `setup-fleet`, `reconcile-fleet.sh`, the timer installers and the rehearsal scripts onto the adapter, one small PR each.

## Risks

| risk | impact | mitigation |
|---|---|---|
| The gate strands a bring-up (unwritable state dir, a stuck holder) | a bot never starts | every failure path proceeds and discloses (`boot_admission_timeout`, `boot_admission_unavailable`); holders are reaped on pid death; the wait cap is 20 min |
| Keepalive kickstarts a queued bot on launchd (livelock) | the storm returns in a new shape | the marker rung in `service_is_starting` lands in the same task as the gate, and the harness scenario asserts a keepalive tick leaves a queued bot alone |
| Removing `ExecStartPre` breaks the `Type=simple` + `RemainAfterExit` triple that makes SubState a boot signal on Linux | keepalive's Linux dead-session watchdog goes blind | the triple test in `tests/test_composer.py` stays; only the stagger line goes; the marker becomes the first rung on both OSes |
| The ratchet allowlist rots or is bypassed by a rename | a new direct call slips in | counts only shrink; the test greps for the binaries, not for wrappers; a rename of the adapter file fails the test by construction |
| `MCP_TIMEOUT` set too high hides a broken MCP server for minutes | slower failure on a genuinely broken server | 180 s is a startup bound only; the readiness ceiling names the state at timeout; keepalive's heal ladder is unchanged |
| The self-start instrument on the Pi loses its rung-based "not yet due" | a measurement page reads wrong after a Pi reboot | PR B makes it read the markers, with its disclosure path kept for hosts on old units |
| The plugin-once stamp survives a boot epoch that cannot be resolved | plugin updates skipped forever on such a host | unresolvable epoch falls back to per-start updates, logged |

## Complexity and Sequencing

| phase | size | depends on | parallel with |
|---|---|---|---|
| PR A — the truth, the renderer boundary, the adapter and ratchet, the three riders | L | none | — |
| PR B — the gate, the marker, the migration, the stagger retired, the reboot proof | L | PR A merged and deployed | its own plan may be hardened while A builds |

Critical path: A's Task 1 (`BootPolicy`) → A's Task 2 (`bot.conf` keys) → A's gauntlet → A's deploy → B's Task 1 (gate library) → B's Task 2 (wiring + marker + harness) → B's gauntlet → B's deploy → the planned reboot.

## Context

area: supervision / boot path · effort: L + L · risk: high (the boot path of every bot on every host) · priority: P1 (bites at every reboot; the last one took the estate's inbound down for half an hour)

Linear: neither.
