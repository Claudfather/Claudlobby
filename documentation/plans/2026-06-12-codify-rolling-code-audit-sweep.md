---
title: Codify the Rolling Code-Audit Sweep as a First-Class claudlobby Feature
type: plan
status: partially-completed
owner: astrid
tags: [sweep, code-audit, composer, timers, observability, tech-debt]
created: 2026-06-12
updated: 2026-07-06
---

# Codify the Rolling Code-Audit Sweep — Implementation Plan

## Implementation Status

*Updated 2026-07-06.* **Shipped in PR #408** (commit `e1f754a`, "feat(sweep): rolling code-audit sweep as opt-in claudlobby feature"): Phases 1–5 and nearly all of Phase 7 are complete and live — `SweepConfig` (`claudlobby/config.py`), the `_write_timer_units` compositor consolidation, `_validate_sweep` (`claudlobby/validator.py`), the `lib/code-audit-sweep.sh` selector, the `library/skills/code-audit-sweep/SKILL.md` real skill, cross-platform installers, the observability event vocabulary, and a 321-line test suite (`tests/test_code_audit_sweep.py`) all exist and match this plan's spec closely. Every Decision Fork (F1–F6) was resolved by adopting its Lean (a) option in the shipped code — no formal `[FORK-LOCK]` PR-comment ratification is visible in the git-visible repo, but the decisions were made and shipped, so the banner immediately below is stale/historical (kept for the record of what was proposed).

**Outstanding: Phase 6 (sunset)**, explicitly deferred by the implementing commit's own message ("coordinate via ari... overlaps alex's concurrent skill-removal work"). Four items remain:
1. Delete `library/skills/sweep/` and `library/skills/sweep-personal/`.
2. Remove `sweep` from `fleet.yaml.example`'s skill list (`fleet.yaml.example:96`).
3. Update `documentation/advanced-patterns.md`'s hand-rolled `audit-tracker.json` description (still documents the old cron/Python-tracker pattern in full, ~lines 668-830).
4. Update `documentation/runbooks/pi-setup-guide.md` to add the sweep installer step (also a Phase 7 gap — never touched for this feature).

Per-phase status markers with evidence are inline below. `status: partially-completed` (frontmatter) reflects the split: Phase 6 is a load-bearing "no shims" requirement, not a nice-to-have, so this is deliberately not `status: completed`.

> **For agentic workers / reviewers:** This is a `/forge`-style PLAN. The **Decision Forks** below are **OPEN** pending human ratification (`[FORK-LOCK F<N>]` comments on the PR). **Do not implement until the forks that gate a phase are locked.** Steps use checkbox (`- [ ]`) syntax. Sizing is **S/M/L per phase — no calendar estimates** (fleet convention).
>
> **Fleet-specific execution detail** (concrete repo list, exact sunset paths, cold-start seeding, briefing re-wire) lives in the cross-linked fleet planning doc: `<fleet>/shared/planning/active/2026-06-12-codify-rolling-code-audit-sweep.md` (in the fleet overlay vault). This committed doc is the **generic, reusable design** any claudlobby deployment can adopt.

**Goal:** Replace the hand-rolled, fragile rolling code-audit sweep with a first-class, **opt-in**, composer-integrated claudlobby feature whose staleness signal is **authoritative** (GitHub `auto-audit` issue timestamps), whose logging reuses the existing **events-JSONL** pattern, and whose nightly trigger is installed via the existing **composer → systemd/launchd timer** mechanism — then delete the hand-rolled tooling cleanly with no shims.

**Architecture:** A no-LLM bash selector (`lib/code-audit-sweep.sh`) queries GitHub per configured repo for `max(createdAt)` of `auto-audit`-labeled issues, picks the stalest, emits an `audit_selected` event, and hands the audit to the owner bot's tmux session through the **existing** `lib/bot-sweep-cron.sh` dispatch path (which already owns sanitize + busy-pane guard + send-keys + logging). The audit (clauDNA `/claudna:tech-debt --auto --output github`) files `auto-audit`-labeled issues — which **become the next run's staleness signal**, closing the loop through GitHub with **no local tracker and no select→run→log race**. The feature is opt-in via a `fleet.yaml` `sweep:` block parsed into `FleetConfig.sweep`; the compositor emits the env and a fleet timer (systemd `.timer` on Linux, launchd `.plist` on macOS) through the **same** `compose_fleet_timers` emission path used by `fleet-pulse`/`creds-check`; a thin `lib/install-code-audit-sweep-systemd.sh` (Linux) / launchd sibling (macOS) enrolls it.

**Tech stack:** Bash (`lib/`, sourcing `lib-common.sh`), Python compositor (`claudlobby/config.py`, `composer.py`, `validator.py`, `diff.py`), systemd-user timers / launchd LaunchAgents, `gh` CLI, clauDNA audit skills.

---

## Empirical validation (performed before authoring — root-cause is proven)

The core design claim — *"GitHub `auto-audit` issue timestamps are a correct, self-healing staleness signal"* — was validated live against a real fleet before writing this plan. A single `gh issue list --label auto-audit --state all --json createdAt` query per repo, reduced to `max_by(.createdAt)`, produced:

| Outcome | Result |
|---|---|
| Stalest repo correctly identified | A repo last audited **22 days** ago was ranked stalest and would be selected. |
| Freshly-audited repo correctly **excluded** | A repo whose `auto-audit` issues were filed **< 24 h earlier (0 days)** ranked freshest — **the exact case the JSON tracker mis-flagged as "20 days stale" and re-audited.** |
| Signal already populated | Audited repos returned dozens of labeled issues (no synthetic seeding required). |
| Cold-start set surfaced | Never-audited repos returned `NONE` → read as max-stale; handled by one-per-run draining (see Sunset § Cold-start). |

This is the mandatory behavior evidence (CLAUDE.md "Validating changes to how a bot behaves"). It proves the *signal*; Phase 5 adds the end-to-end harness that proves the *job*.

---

## Why this design (root-cause analysis)

**The bug being killed:** staleness is read **only** from a hand-written `audit-tracker.json`. Two defects make it lie:
1. **select → run → log race.** The tracker is written *after* the audit completes, with **no "in-progress" marker**. A repo stays max-stale across its own audit window, so a second trigger (or a crash mid-audit) re-selects it.
2. **Skipped writes.** Audits dispatched outside the `/sweep` flow (e.g. a manual manager→engineer dispatch) never reach the tracker at all; the derived briefing artifact silently froze.

**The fix is structural, not a patch:** make GitHub *the* ledger.
- **Selection reads GitHub:** `max(createdAt)` of `auto-audit`-labeled issues per repo.
- **The audit writes GitHub:** every audit files `auto-audit`-labeled issues.
- **The loop closes through GitHub:** the next run sees those issues. There is **no local tracker, therefore no race and no drift**, and **every** audit is captured regardless of how it was triggered — because the *output* (labeled issues) *is* the ledger.

This is the "convention over event" and "consolidate, don't fork" principles applied to data: one authoritative source, self-documenting via the issue label, no parallel state to skew.

---

## Decision Forks (OPEN — human ratification required)

Each fork has a recommended **Lean**. Forks are locked via `[FORK-LOCK F<N>]` PR comments by the ratifier before the gated phase begins (`decision-fork-lifecycle` protocol).

### F1 — Onboarding / config mechanism &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** How does an operator opt in and configure the sweep?
- **(a) `fleet.yaml` `sweep:` block parsed by the compositor** — **LEAN.** Opt-in = presence of the block; `claudlobby generate` reproduces env + timer; `claudlobby validate` checks it.
- (b) A dedicated `/sweep-config` scaffold skill that writes the block.
- (c) A `claudlobby new-sweep` interactive wizard.
- (d) `/forge` itself as the config vehicle (this plan) with no persistent config surface.

**Rationale for (a):** Every existing optional feature — `observability`, `plugins`, `autonomous_runner` — is a `fleet.yaml` block coerced into a dataclass and consumed by the composer (`config.py:76-93`, `:163-174`; `composer.py:378-403`). Reusing that path means zero new config surface, free validation, and reproducibility. (b)/(c) add a **parallel config path** that would diverge — a direct "consolidate, don't fork" violation. (c) can be a *thin future convenience* that writes the same block, not the source of truth. **Ratifier:** human.

### F2 — Repo-list source &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** Where does the list of repos to audit come from?
- **(a) Explicit `sweep.repos: [org/repo, …]`, defaulting to the owner bot's existing `scope.repos`** — **LEAN.**
- (b) Auto-discover from the owner bot's `projects/` checkouts.
- (c) Auto-discover all repos under the org via the GitHub API.

**Rationale for (a):** The owning bot already declares its repos in `scope.repos` (fleet.yaml) — reuse it (DRY) and allow an explicit `sweep.repos` override. (b) is unreliable: `projects/` holds incidental checkouts (tooling repos, data dirs) that are not audit targets. (c) audits repos no one owns and can't gate scope. Explicit-with-scope-default is deterministic and self-documenting. **Ratifier:** human.

### F3 — Keep a derived local cache? &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** Cache the GitHub staleness read locally?
- **(a) No cache — query GitHub live each run** — **LEAN.**
- (b) Keep a cache *derived from* GitHub (never authoritative), refreshed each run.

**Rationale for (a):** One `gh` call per repo per night is trivially cheap. The entire original bug was *a cache treated as source of truth*. A cache adds an invalidation surface for **zero** benefit at this cadence — YAGNI. If a future fleet audits hundreds of repos, revisit. **Ratifier:** human.

### F4 — Config scope: per-fleet vs per-bot &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** Is the sweep configured once per fleet, or per bot?
- **(a) Per-fleet (`FleetConfig.sweep`), one fleet-wide nightly job** — **LEAN.**
- (b) Per-bot (`BotConfig.sweep`), each bot sweeps its own scope.

**Rationale for (a):** The sweep is a fleet-level periodic job exactly like `fleet-pulse` and `creds-check`, which are prefix-scoped fleet timers, never per-bot (`composer.py:1396`). One job iterates the configured repos and dispatches to one owner. Per-bot multiplies timers and risks concurrent audits of overlapping scopes. **Ratifier:** human.

### F5 — Trigger ownership (who runs the audit) &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** The timer is fleet-level and runs no LLM. Which bot's Claude session executes the dispatched audit, and where do sweep events land?
- **(a) Configurable `sweep.owner_bot`; selector dispatches the audit into that bot's tmux session; events written to that bot's `data/events/`** — **LEAN.**
- (b) A fixed convention (e.g. always the fleet manager).

**Rationale for (a):** Audits need a real bot identity (its MCP GitHub server files the issues, its auth, its scope). Making the owner explicit keeps it legible and lets a fleet point the sweep at a dedicated worker. Events land in the owner's stream so they surface in that bot's existing observability/pulse-summary with no new plumbing. **Ratifier:** human.

### F6 — Trigger → audit handoff &nbsp;·&nbsp; *Status: leaning (a)*
**Question:** How does the no-LLM timer hand the audit to an LLM?
- **(a) `tmux send-keys` dispatch into the owner bot's live session** — **LEAN.**
- (b) Headless `claude -p "<audit command>"` spawned by the timer in the repo dir.

**Rationale for (a):** This is the fleet's established dispatch path (manager→worker), reusing the running bot's identity, MCP servers, and credentials. **Mandatory:** the selector does **not** hand-roll send-keys — it calls the existing `lib/bot-sweep-cron.sh <owner_bot> "<audit command>"`, which already owns `sanitize_tmux_input` (`lib-common.sh:240`), history-expansion safety, the **busy-pane check that prevents a double-dispatch** when a tick fires mid-audit, the `send-keys` + bare-`Enter` sequence, and dispatch logging. Re-implementing any of that in the selector is a consolidate-don't-fork violation. (b) spawns a context-less session that must re-establish auth/MCP and has no observability home. **Ratifier:** human.

> **Implementation note (not a fork — resolved by principle):** the opt-in timer is emitted through the **existing** `compose_fleet_timers` emission templates, not a sibling function. The per-timer service/timer/plist writer is extracted into a reusable helper called by both the `system_defaults` platform-timer loop **and** the `fleet.sweep` opt-in path — one emission implementation, two callers (consolidate, don't fork). The **installer** side gets the same treatment: the copy-and-enroll step is extracted into a shared `lib/install_fleet_timer.sh <name>` helper called by the fleet-pulse, creds-check, and sweep installers (Phase 3), collapsing three byte-identical scripts that vary only by `NAME=`.

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| **Create** | `lib/code-audit-sweep.sh` | No-LLM selector: resolve repos → `gh` staleness query → pick stalest (deterministic tiebreak) → emit `audit_selected` event → delegate dispatch to `lib/bot-sweep-cron.sh` (owns sanitize, busy-pane guard, send-keys, logging — **not** re-implemented here). Sources `lib-common.sh`. |
| **Create** | `lib/install-code-audit-sweep-systemd.sh` | Linux enrollment — a thin caller of the shared `lib/install_fleet_timer.sh code-audit-sweep` helper (Phase 3); its only feature-specific value is `NAME="$SERVICE_PREFIX.code-audit-sweep"`. **Not** a verbatim copy. |
| **Create** | `lib/install_fleet_timer.sh` | Shared systemd-timer enroll helper (`<name>` arg): copy generated `.service`/`.timer` → `~/.config/systemd/user/`, `daemon-reload`, `enable --now`, with the "run `claudlobby generate` first" guard. One enroll implementation, three callers. |
| **Modify** | `lib/install-fleet-pulse-systemd.sh`, `lib/install-creds-check-systemd.sh` | Collapse to thin callers of `install_fleet_timer.sh <name>` — today byte-identical except their `NAME=` line (installer-side mirror of the compositor `_write_timer_units` consolidation). |
| **Create** | `lib/install-code-audit-sweep.sh` | macOS enrollment — copy generated `.plist` → `~/Library/LaunchAgents/`, `bootout`/`bootstrap`. Mirrors `install-keepalive.sh:43-86` but copies the **composer-generated** plist (single source of truth). |
| **Create** | `library/skills/code-audit-sweep/SKILL.md` | The real audit skill (replaces both placeholder skills): run `/claudna:<type> --auto --output github`, ensure `auto-audit` label, emit `audit_completed` event. **No `<PLACEHOLDER>` tokens.** |
| **Modify** | `claudlobby/config.py` | Add `SweepConfig` dataclass (after `ObservabilityConfig` ~`:93`), `sweep: SweepConfig \| None` on `FleetConfig` (~`:265`), `_coerce_sweep()` (~`:458`), parse in `load_fleet()` (`:807-819`). |
| **Modify** | `claudlobby/composer.py` | Extract per-timer emission into a helper; call it for `fleet.sweep` under an opt-in gate; (if F5 needs per-bot env) emit `SWEEP_*` lines after the observability block (`:404`). |
| **Modify** | `claudlobby/validator.py` | Add `_validate_sweep(fleet, report)` at the fleet-validation call site (`:469-472`); hard-error on missing repos+scope, warn on bad schedule/repo format. |
| **Modify** | `claudlobby/diff.py` | Recognize the new timer so it is not reported as drift (`:73-119`). |
| **Modify** | `claudlobby/system_defaults.yaml` | (If chosen) timer *shape* skeleton only — **no fleet-specific repos/label** (those stay in `fleet.yaml`). |
| **Modify** | `documentation/fleet-yaml-schema.md` | Document the `fleet.sweep` block (mirror the `### fleet.plugins` section `:131-157`). |
| **Modify** | `documentation/install-patterns.md`, `documentation/runbooks/pi-setup-guide.md` | Add the sweep installer to the operator install steps (`install-patterns.md:41-42`). |
| **Create** | `tests/test_code_audit_sweep.sh`, compositor tests | Composition + behavior tests (Phase 5). |
| **Delete** | template skills, hand-rolled scripts, cron, fleet.yaml skill entries | Sunset (Phase 6) — see fleet doc for exact paths. |

---

## Phases

### Phase 0 — Ratify forks &nbsp;·&nbsp; **Gate** &nbsp;·&nbsp; **S**

**Status: COMPLETED** — no formal `[FORK-LOCK]` PR comments found for this plan in the git-visible repo, but commit `e1f754a` shipped every fork's Lean (a) option, so ratification happened de facto through implementation rather than through the ceremony below.

- [ ] Post forks F1–F6 to the PR; collect `[FORK-LOCK]` ratifications from the human.
- [ ] Do not start a phase whose gating fork is still `open`. (F1/F4 gate Phase 2; F5/F6 gate Phase 1's dispatch step.)

### Phase 1 — Selector script + real skill (the engine) &nbsp;·&nbsp; **M** &nbsp;·&nbsp; *gated by F2, F5, F6*

**Status: COMPLETED** — `lib/code-audit-sweep.sh` and `library/skills/code-audit-sweep/SKILL.md` both exist and match the spec (delegates dispatch to `lib/bot-sweep-cron.sh`; skill guarantees the `auto-audit` label explicitly, not by trusting delegation).

**Files:** Create `lib/code-audit-sweep.sh`; create `library/skills/code-audit-sweep/SKILL.md`.

- [ ] **Selector skeleton** — copy the job-script shape from `lib/fleet-pulse.sh:1-29`: `#!/usr/bin/env bash`, `set -euo pipefail`, source `lib-common.sh`, `FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"`, `install_error_trap ""`, `ts=$(ts_iso)`, `today=$(date +%Y-%m-%d)`.
- [ ] **Resolve repos** (F2 lean): read `SWEEP_REPOS` from the owner bot's `bot.conf` via `bot_conf_get` (`lib-common.sh:468`), falling back to the bot's `scope.repos`.
- [ ] **Staleness query** — for each `org/repo`, run with a timeout guard (`with_timeout` `lib-common.sh:95`):
  `gh issue list --repo "$repo" --label "$SWEEP_LABEL" --state all --limit 200 --json createdAt -q 'if length>0 then max_by(.createdAt).createdAt else "NONE" end'`.
  Map `NONE` → max staleness. Tiebreak never-audited repos **deterministically** (config order).
- [ ] **GitHub-failure handling** — if a `gh` call errors (auth/network), **skip that repo and emit a `sweep_repo_unreachable` event; never treat an error as "fresh" or "stale"** (no-fabrication guardrail). If *all* repos fail, exit non-zero (the error trap emits `script_error`).
- [ ] **Emit `audit_selected`** — copy the `emit_event` helper from `fleet-pulse.sh:47-54` with `source:"audit"`, write `{"repo":…,"staleness_days":…,"audit_type":…}` to the **owner bot's** `data/events/fleet-${today}.jsonl` (F5).
- [ ] **Dispatch** (F6 lean): build the audit command string and delegate to the existing `bot-sweep-cron.sh "$SWEEP_OWNER_BOT" "<audit command>"`, which owns `sanitize_tmux_input`, the **busy-pane double-dispatch guard** (skips the tick if the owner is mid-audit), `send-keys` + bare `Enter`, and dispatch logging. The selector only builds the string and rotates audit type over `SWEEP_AUDIT_TYPES` (default `tech-debt`) — it must **not** re-implement send-keys/sanitize/busy logic (consolidate, don't fork). Caveat to honor in Phase 4: `bot-sweep-cron.sh` exits 0 on **both** dispatch and busy-skip, so derive the `audit_dispatched` event from its actual outcome (distinct exit code or its log line) rather than emitting it unconditionally — extend the shared dispatcher, don't fork it.
- [ ] **Skill** — write `library/skills/code-audit-sweep/SKILL.md`: pull `main`, run `/claudna:<type> --auto --output github`, then **guarantee the `auto-audit` label by an explicit mechanism — not by trusting delegation.** Why this is load-bearing: `/claudna:publish` only applies labels present in each doc's frontmatter `tags:` (`gh issue create --label "<tags>"`), and the three audit skills' own output instructions name only `priority:*`/`enhancement` — **not** `auto-audit` (the "always apply `auto-audit`" rule lives in the inherited `_shared/output-guide.md`, which an `--auto` run may or may not honor). **Mechanism (cleanest, use this):** the skill instructs the audit to add `auto-audit` to the `tags:` of every doc it writes *before* `/claudna:publish` runs. **Acceptable fallback:** immediately after the audit, `gh issue list --repo <repo> --limit 50 --json number,createdAt` then `gh issue edit --add-label auto-audit` on each issue created this session. Then emit an `audit_completed` event (closes the observability loop). Zero placeholder tokens; use `{{BOT_NAME}}`/`{{CLAUDLOBBY_ROOT}}` Jinja placeholders only.
- [ ] **Test** the selector in isolation against a fixture repo set (Phase 5 wires the assertion).

### Phase 2 — Compositor integration (config → env + timer) &nbsp;·&nbsp; **M** &nbsp;·&nbsp; *gated by F1, F4*

**Status: COMPLETED** — `SweepConfig`/`_coerce_sweep`/`sweep_enabled()` in `claudlobby/config.py` (lines 96, 279, 281, 491, 917); `_write_timer_units` extracted in `composer.py:1464`, called from both the `system_defaults` loop and the opt-in `fleet.sweep` branch (`composer.py:1686,1710,1746`), gated on `fleet.sweep_enabled()`.

**Files:** Modify `claudlobby/config.py`, `composer.py`, `system_defaults.yaml`.

- [ ] **`SweepConfig` dataclass** (`config.py` after `:93`): fields `enabled: bool`, `owner_bot: str | None`, `repos: list[str]`, `label: str = "auto-audit"`, `schedule: str = "*-*-* 03:00:00"`, `audit_types: list[str]`. All `None`/empty-defaulted so absence ⇒ nothing emitted.
- [ ] **`_coerce_sweep(raw)`** (`config.py` ~`:458`) mirroring `_coerce_observability` (`:458-470`); add `sweep: SweepConfig | None = None` to `FleetConfig` (`:265`); parse in `load_fleet()` via `sweep=_coerce_sweep(fleet.get("sweep"))` (mirror `_coerce_plugins` `:814`).
- [ ] **Extract emission helper** — refactor the service/timer/plist writer out of `compose_fleet_timers` (`composer.py:1400-1509`) into `_write_timer_units(name, script, schedule, …)`. Call it from the existing `system_defaults` loop **and** from a new `if fleet.sweep and fleet.sweep.enabled:` branch that synthesizes the sweep timer from `fleet.sweep` (script = `${CLAUDLOBBY_ROOT}/lib/code-audit-sweep.sh`, `OnCalendar = fleet.sweep.schedule`). One emitter, two callers.
- [ ] **Gate** the sweep timer on `fleet.sweep.enabled` so a fleet with no `sweep:` block gets **no** unit files (mirror the `if sd.enabled and sd.timers:` gate, `commands/core.py:85-89`).
- [ ] (Only if F5 ⇒ per-bot env) emit `export SWEEP_REPOS/SWEEP_LABEL/SWEEP_AUDIT_TYPES` into the owner bot's `bot.conf` after the observability block (`composer.py:404`), each through `_shq` (`:260`).
- [ ] **Test:** `claudlobby --fleet <f> generate` with a `sweep:` block ⇒ assert `runtime/fleet/timers/<prefix>.code-audit-sweep.{service,timer,plist}` exist; without the block ⇒ assert they do **not**.

### Phase 3 — Installers (cross-platform enrollment) &nbsp;·&nbsp; **S**

**Status: COMPLETED** — `lib/install_fleet_timer.sh` (shared helper), `lib/install-code-audit-sweep-systemd.sh` (Linux), and `lib/install-code-audit-sweep.sh` (macOS) all exist; `lib/install-fleet-pulse-systemd.sh` and `lib/install-creds-check-systemd.sh` are now thin one-line callers of the shared helper — the "one enroll implementation, three callers" consolidation landed as specified.

**Files:** Create `lib/install_fleet_timer.sh` (shared enroll helper), `lib/install-code-audit-sweep-systemd.sh`, `lib/install-code-audit-sweep.sh`; refactor `lib/install-fleet-pulse-systemd.sh` + `lib/install-creds-check-systemd.sh` to call the helper.

- [ ] **Linux** — **extract a shared `lib/install_fleet_timer.sh <name>` helper** holding the copy-generated-`.service`/`.timer` → `~/.config/systemd/user/` + `daemon-reload` + `enable --now` + the "run `claudlobby generate` first" guard, parameterized on `NAME="$SERVICE_PREFIX.<name>"`. Make `install-code-audit-sweep-systemd.sh` a thin caller (`install_fleet_timer.sh code-audit-sweep`), and **refactor the two existing installers** (`install-fleet-pulse-systemd.sh`, `install-creds-check-systemd.sh`) — today byte-identical except their `NAME=` line — to call it too. One enroll implementation, three callers (the installer-side mirror of the Phase 2 `_write_timer_units` consolidation).
- [ ] **macOS** — model on `install-keepalive.sh:43-86` but copy the composer-generated `runtime/fleet/timers/<prefix>.code-audit-sweep.plist` (not an inline plist) → `~/Library/LaunchAgents/`, then `launchctl bootout`/`bootstrap`. OS-branch on `$_OS` (`lib-common.sh:55`).
- [ ] **Wire into docs** (Phase 7): the installer is a deliberate, separate operator step — generation never auto-enrolls (matches every existing fleet timer).

### Phase 4 — Logging / observability wiring &nbsp;·&nbsp; **S**

**Status: COMPLETED** — event vocabulary (`audit_selected`, `audit_dispatched`, `audit_deferred`, `sweep_repo_unreachable`, `audit_completed`, `audit_failed`) documented in `library/protocols/fleet-observability.md:70-75`; retention piggybacks on the existing 7-day `fleet-*.jsonl` reapers, no bespoke log file added.

- [ ] Confirm events land in `<owner-bot>/data/events/fleet-<date>.jsonl` with the canonical shape `{ts,bot,type,source:"audit",data}` (`fleet-observability` protocol).
- [ ] **Retention is free** — the existing 7-day reapers (`bot-vitals.sh:106`, `fleet-pulse.sh:76-84`) match `fleet-*.jsonl`; no rotation wiring needed. **Do not** add a bespoke `.log` in `data/` (unregistered ⇒ never rotated); if a human-readable rollup is wanted, use `$CLAUDLOBBY_ROOT/state/code-audit/` like `state/pulse/`.
- [ ] Event vocabulary: `audit_selected`, `audit_dispatched`, `sweep_repo_unreachable`, `audit_completed`, `audit_failed`. Document near the emission helper (convention-over-event: no timestamped comments).

### Phase 5 — Validation harness + tests &nbsp;·&nbsp; **MANDATORY** &nbsp;·&nbsp; **M**

**Status: COMPLETED** — `tests/test_code_audit_sweep.py` exists (321 lines); the implementing commit message claims 15 tests, all passing, including a hermetic end-to-end selector test (`gh`/tmux shimmed, no network). Not re-executed live during this doc audit — file presence and commit-message claim only.

- [ ] **Unit (composition):** config parse, env emission, timer-file generation, opt-out emits nothing, validator errors. Add to the compositor test suite (`pytest`).
- [ ] **Validator:** `_validate_sweep` (`validator.py:469-472`) — hard error when `enabled` and no `repos` and owner has no `scope.repos` (mirror `:309-314`); warn on bad `schedule`/`org/repo` format (mirror `:180-198`, `:303-307`); `closest_match` typo hints (`:27`).
- [ ] **Behavior (the gate that actually proves it):** extend `lib/validate-bot-change.sh` — stand up a throwaway bot + tmux session, point `SWEEP_REPOS` at a small real repo set, run `lib/code-audit-sweep.sh <fleet>`, and **assert**: (1) the stalest repo was selected via the live `gh` query; (2) an `audit_selected` event was appended to `data/events/`; (3) the dispatch string reached the owner pane. **Cite the observation verbatim in the PR body** (claimed evidence is not evidence).
- [ ] **`diff.py`:** assert the new timer does not show as drift (`:73-119`).

### Phase 6 — Sunset the hand-rolled tooling &nbsp;·&nbsp; **M** &nbsp;·&nbsp; *no shims, no parallel run*

**Status: PENDING** — explicitly deferred in the implementing commit's own message ("coordinate via ari... overlaps alex's concurrent skill-removal work"). Still on disk: `library/skills/sweep/SKILL.md` and `library/skills/sweep-personal/SKILL.md` (both untouched since May 15, pre-dating the sweep feature); `fleet.yaml.example:96` still lists `sweep` in a bot's `skills:` array; `documentation/advanced-patterns.md` (~lines 668-830) still documents the old hand-rolled `audit-tracker.json` pattern this phase is meant to delete. See "Implementation Status" near the top for the full outstanding checklist.

- [ ] **Delete the placeholder skills** `library/skills/sweep/` and `library/skills/sweep-personal/` and **remove their `fleet.yaml` skill entries** (exact files in the fleet doc).
- [ ] **Delete the hand-rolled engine** (gitignored, host-local): the selector, the Python tracker, the JSON state, the decoy `/sweep` trigger, and the run log (exact paths in the fleet doc).
- [ ] **Remove the cron trigger** — delete the hand-added crontab line; the composer-installed timer replaces it. No overlap window.
- [ ] **Re-wire, don't orphan, the briefing consumer** — whatever the morning briefing read from the old artifact must read from the events stream (or the audit section is dropped deliberately). Identify and fix in the same PR.
- [ ] **Regenerate** affected bots (`claudlobby generate`) so the composed `CLAUDE.md` no longer references the removed tooling — **never hand-edit generated output.**
- [ ] **Backfill: NONE.** GitHub already holds the `auto-audit` history (proven in Empirical Validation). The deleted JSON is not migrated — it shares no key with the GitHub query. **Cold-start:** never-audited repos read max-stale and drain one-per-run with the deterministic tiebreak; converges in N runs. *Optional 10-minute polish:* relabel the most recent real audit issue per never-labeled repo with `auto-audit` to seed a realistic baseline (a GitHub-side label op, not a state file). Recommend ship-without; seed only if the thundering-herd ordering matters to the operator.

### Phase 7 — Docs + schema &nbsp;·&nbsp; **S**

**Status: IN PROGRESS** — `fleet-yaml-schema.md:190-199`, `install-patterns.md:29,50`, and the `fleet-observability` protocol's event vocabulary are all documented. Gap: `documentation/runbooks/pi-setup-guide.md` was never updated for this feature (its last touch predates this plan entirely) — the one outstanding checklist item below.

- [ ] `fleet-yaml-schema.md`: document `fleet.sweep` (mirror `### fleet.plugins` `:131-157`).
- [ ] `install-patterns.md` / `pi-setup-guide.md`: add the sweep installer step.
- [ ] Document the event vocabulary in the `fleet-observability` protocol.
- [ ] `/simplify` + final self-review before the implementation PR (per implementer-workflow).

---

## Spec coverage (brief requirement → phase)

| Brief requirement | Satisfied by |
|---|---|
| 1. First-class, codified, reproducible (`library/`+`lib/`+`claudlobby/`) | Phases 1–3 |
| 2. Optional + onboardable + configurable | F1/F4 + Phase 2 opt-in gate |
| 3. Root-cause fix — authoritative GitHub staleness | Why-this-design + Phase 1; repo-list = F2 |
| 4. Proper logging — reuse events-JSONL + rotation, no hand-roll | Phase 4 |
| 5. Trigger via composer, cross-platform (systemd/launchd) | Phases 2–3 |
| 6. Clean sunset — no shims, with backfill assessment | Phase 6 |
| Process: forks with leans; S/M/L sizing; principles (no-shims, consolidate, convention-over-event) | Decision Forks + Phase 2 note + Phase 4 |

## Risks & open questions
- **`auto-audit` label provenance** — the label is **not guaranteed by delegation.** `/claudna:tech-debt`, `/security-audit`, and `/product-enhance` name only `priority:*`/`enhancement` in their own output instructions; the "always apply `auto-audit`" rule lives in the inherited `_shared/output-guide.md`, and `/claudna:publish` labels solely from each doc's frontmatter `tags:`. Phase 1's skill therefore **sets `auto-audit` explicitly** (in `tags:` before publish, with a post-hoc `gh issue edit --add-label` fallback — see Phase 1's skill step). Without that, an audit can file unlabeled issues, leaving the repo max-stale so it re-audits next cycle — the same silent-miss failure mode as the retired JSON tracker.
- **Concurrency** — a long audit must not overlap the next night's run. The audit's own filed issues make the repo "fresh," so the next selection naturally skips it; add a `with_lock` (`lib-common.sh:108`) guard on the selector only if double-fire is observed.
- **Per-type staleness** — v1 tracks staleness per repo (ratified: `max(createdAt)`), rotating audit type by run. Per-(repo,type) staleness (queryable via the `[type]` issue-title prefix) is a deliberate future enhancement, not v1.
