---
title: "[plan] Telegram bridge poller-hijack fix — heal rollout, prefer-live-holder, deaf-poller detection"
type: plan
status: draft
owner: astrid
created: 2026-07-10
updated: 2026-07-10
tags: [telegram, bridge, no_bridge, keepalive, observability, plugin, upstream, root-cause]
repos: claudlobby
---

# Telegram bridge poller-hijack fix

## Summary

Root cause is CONFIRMED by controlled replication (17/17 checks, 3 identical runs): the telegram plugin's boot-time reap (`server.ts` v0.0.6 L60-69) is last-writer-wins — any *second* claude-family process that starts in a bot's env and launches the telegram MCP will SIGTERM the bot's **live** poller, take the single `getUpdates` slot, and abandon it on exit (ownership-checked `bot.pid` unlink). The victim session never reconnects: permanent dark bridge until the next restart. A third flavor exists where the poller stops polling after persistent 409s but stays alive holding `bot.pid` — structurally healthy, functionally deaf, invisible to current detection.

This plan layers three defenses, each empirically validated against the replication harness: **recovery** (enable the already-built, harness-proven #453 keepalive heal ladder — its planned Fork F6b Tier-2 rollout, for which this incident is the gate evidence), **prevention** (an upstream prefer-live-holder patch to the plugin, with the rightful-owner reap moved to the supervised boot path), and **detection/attribution** (heartbeat-staleness detection for deaf holders, plus a boot-audit breadcrumb that names the transient processes definitively).

## Evidence

Current state, verified 2026-07-10 against the installed plugin and `main`:

**Plugin** (`~/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6/server.ts`; upstream `anthropics/claude-plugins-official`, `external_plugins/telegram/server.ts`; only 0.0.6 installed):

- L60-69 (module top level): boot reap. Reads `bot.pid`, probes holder with `process.kill(stale, 0)`, writes `replacing stale poller pid=` to stderr, `process.kill(stale, 'SIGTERM')` — **no distinction between an orphaned zombie and the live poller of the current session**. Dead holder (ESRCH) is swallowed by the bare `catch {}` (L68) and the file is claimed (L69).
- L69: `bot.pid` is a bare decimal pid, written **once at boot, never refreshed** — no heartbeat signal exists for freshness checks.
- L993-1032: polling loop (`bot.start()`, grammy default long-poll). 409-Conflict branch at L1016-1022: after `attempt >= 8` it logs "…Exiting." and `return`s — **but only exits the async IIFE, not the process**. No `process.exit()`, no `bot.pid` unlink: the MCP stdin keeps the process alive, deaf, holding the slot (the third flavor; the loop's own header comment L989-992 describes this exact failure).
- L636-647 `shutdown()`: ownership-checked unlink (L641: only if `bot.pid` still holds our pid), `bot.stop()` + 2s force-exit. Wired to stdin end/close (L648-649), SIGTERM/SIGINT/SIGHUP (L650-652).
- L654-664 orphan watchdog: 5s interval, self-exits on reparent or destroyed/ended stdin. Catches a poller whose claude *died* — not one whose claude is alive.

**Fleet detection** (`lib/lib-common.sh`, SSOT since #454):

- `bridge_state()` L366-448: gates on handle → token → `bot.pid` exists (L381-382) → pid alive (L384-390) → comm `bun` + args `server.ts` (L395-402) → env ownership via `/proc/<pid>/environ` (L408-412) → live `claude` ancestor via PPID-chain walk (L428-444) → `up`. **Every gate is existence/identity/lineage — nothing observes whether polling actually works.** A 409-deaf poller with a live claude parent passes all gates and reads `up`: it would never be healed.
- `bridge_down_state()` L464-480: wraps with `data/.spawn` grace (default 300s) and collapses to actionable verdicts.

**Heal ladder** (`lib/keepalive.sh`, #453 Phase 5, merged #577 — built, harness-validated, **dormant**):

- `_bridge_heal` L129-162: gated by `OBSERVABILITY_BRIDGE_HEAL` (default `0`, L130); fires only from the `IDLE` pane branch (L312 — a working bot is never bounced); `no_bridge` → `_bridge_heal_bounce` under a per-bot lock; `no_token` → never bounces.
- `_bridge_heal_bounce` L168-195: persisted attempt counter (`data/.bridge-heal`), cap `BRIDGE_HEAL_MAX_ATTEMPTS` (default 3, L170), increments before bouncing, calls the shared `restart_bot_service` ladder (L95-118), emits `BRIDGE_HEAL` events; at cap emits a one-shot `emit_failure_alert … bridge_down` escalation; resets on recovery (L155-159).
- `documentation/guides/observability.md:125-126` documents both vars and states: ships OFF, "enable via a bot/fleet `env:` entry once the gate clears (issue #453 Fork F6b)". **This plan is that gate clearing.**

**Composition constraints** (why the rollout step is shaped the way it is):

- Bridge-heal vars are plain env passthrough, not structured `observability:` fields (`config.py:81-97` has only 4 fields; `composer.py:419-448` emits only those). Per-bot `env:` reaches `bot.conf` via `composer.py:530-533`.
- **`defaults.env` is never merged** — `config.py:925` reads per-bot `env:` only (in-code warning at `validator.py:251-253`). Fleet-wide env goes through the `.env` tiers sourced at `lib/start-bot.sh:137-152` and mirrored for scripts in `lib/lib-common.sh:184-197`: `~/.env` → `local/<fleet>/.env` → `<bot>/.env` → `bot.conf` (a root-level `$CLAUDLOBBY_ROOT/.env` still sources with a deprecation warning). The schema documents this exact pattern for the fleet-pulse escalation vars (`documentation/fleet-yaml-schema.md` §"Fleet-pulse escalation (environment overrides)", ~L331); the bridge-heal vars are missing from that section (doc gap, fixed in Phase 2).
- **Plugin version pinning does not exist**: plugins are strictly `name@marketplace` (`validator.py:476-479` regex rejects any version component); install/update pass the name verbatim (`start-bot.sh:204-211`, `reload-fleet.sh:74`). Custom fork **marketplaces** are supported (`fleet.plugins.marketplaces`, `config.py:376-390`, emitted at `composer.py:519-528`), and the telegram channel is per-bot config (`config.py:329` default `plugin:telegram@claude-plugins-official`, composed at `composer.py:339-352`; hardcoded fallback `start-bot.sh:162`) — so an interim fork adoption needs **zero new claudlobby code**.
- `KillMode=process` is deliberate (`composer.py:604-608` — cgroup cleanup would kill the tmux server) and leaks the old session tree at restart: old claude + its poller survive a service stop (observed in production journal). This interacts dangerously with prefer-live-holder (see Phase 6).

**Incident + replication** (fleet-local; genericized here):

- 2026-07-10: two worker bots dark for hours (`bridge_down` every ~5 min pulse). Timeline reconstructed from event ledgers, MCP jsonl logs, journal, and plugin `.in_use` markers: transient claude-family processes (~80% confidence: headless/subagent runs inheriting bot env) launched the plugin, murdered the live pollers, and abandoned the slot on exit. One hijack-and-abandon lifecycle captured live mid-incident.
- Replication harness (bot-local `data/scripts/repro-bridge-hijack.sh`, promoted in Phase 1): Phase 1 mechanism 7/7 with a fake token and private tmux/state namespace; heal validation (dark slot → `BRIDGE_HEAL` bounce via real `keepalive.sh` + recorder stub); prefer-live-holder prototype (patched copy defers, victim survives, dead-holder reap preserved); opt-in real-slot tier proved genuine 409 single-consumer semantics.
- Full detail: fleet-local findings doc `local/<fleet>/shared/planning/active/2026-07-10-telegram-bridge-dark-poller-root-cause.md`.

## Architecture

Target state — authority for the single `getUpdates` slot becomes explicit and layered:

```
prevention   plugin (upstream): newcomer defers to a live+fresh holder; reaps only
             dead/stale holders; releases the slot properly on persistent 409;
             logs every boot/reap/defer decision to poller-audit.log
authority    supervised boot path (start-bot.sh): the ONE place that may displace
             a live holder — the rightful owner reclaims its slot at service start
detection    bridge_state (SSOT): existence/identity/lineage gates + heartbeat
             freshness — a deaf holder reads no_bridge, not up
recovery     keepalive heal ladder (#453, enabled): no_bridge on an idle bot →
             bounded bounce → escalate at budget; covers every dark flavor
             regardless of cause, including ones this plan didn't foresee
attribution  poller-audit.log breadcrumbs name every process that ever touches
             the slot — the transient-identity question answers itself
```

Design principle: the plugin becomes *polite* (never kills a live peer), the supervisor becomes *authoritative* (only the supervised boot displaces a holder), and the fleet layer stops trusting existence as health.

## Implementation Plan

### Dependencies

None — Phase 1 and Phase 2 start immediately and independently.

### Blocks

Completes the #453 epic (Fork F6b Tier-2 rollout). Unblocks retiring the recurring manual "bridge dark again" firefights and the transient-identity investigation (rider R1).

### Steps

Phases in dependency order. Every phase carries its own validation against the promoted harness — the harness IS the dummy-bot test; no phase ships on claimed behavior.

---

#### Phase 1 (S) — Promote the hijack repro into the validation harness

**Summary.** Promote the bot-local `repro-bridge-hijack.sh` into the repo as the executable proof of the hijack mechanism and the regression gate for every later phase. Follow the established pattern: a scenario in `lib/validate-bot-change.sh` (the #453 heal scenario already lives there at L273-378) plus a pytest subprocess wrapper (`tests/test_validate_harness.py` precedent), with per-run `TMUX_TMPDIR` isolation (#586).

**Steps.**
1. Add a `bridge-hijack` scenario to `lib/validate-bot-change.sh` (or a sibling `lib/validate-bridge-hijack.sh` if size argues for it — engineering judgment via `/weigh-development-paths` in-phase): live poller A (real plugin, fake token `8888888:AAA…`, private `TELEGRAM_STATE_DIR`) → newcomer B same state dir → assert `replacing stale poller pid=A` + A dies gracefully + `bot.pid`=B → B stdin-EOF → assert slot abandoned, no poller returns.
2. Generalize the hardcoded plugin path: resolve the newest installed `~/.claude/plugins/cache/claude-plugins-official/telegram/<version>/` (the current script pins `0.0.6`).
3. Keep the patch-anchor assertion (`assert src.count(old) == 1 … refusing to patch`) so plugin drift fails loudly instead of silently testing nothing.
4. Skip semantics for CI: the scenario requires `bun`, `tmux`, and the installed plugin — skip cleanly (with a printed reason) when absent, mirroring `tests/test_validate_harness.py` `skipif`. CI runs pytest only; the wrapper is the enforcement point on hosts that have the deps.
5. Retire the bot-local script in favor of the promoted one (single source; no fork of the harness left behind).

**Validation.** Run the promoted scenario 3× consecutively — identical pass counts; run existing `validate-bot-change.sh` scenarios — no regression; `pytest tests/test_validate_harness.py` (or the new wrapper) green locally.

**What NOT to do.** Don't stub the poller here — the point of this scenario is the *real* plugin binary exhibiting the real reap; recorder stubs stay for the restart ladder only.

---

#### Phase 2 (S) — Enable the bridge heal fleet-wide (executes #453 Fork F6b Tier-2)

**Summary.** Turn on the dormant, harness-validated keepalive heal for the fleet. Converts a hijack from permanent-dark into dark-for-≤-a-few-keepalive-ticks. Zero new code; one config line plus a doc fix. Ships first because it covers **all** dark flavors — including the observed restart-time 30s-MCP-connect-timeout flavor that prevention can't reach.

**Steps.**
1. Add `OBSERVABILITY_BRIDGE_HEAL=1` to the fleet-tier `local/<fleet>/.env` (the documented pattern for script-read vars — NOT `defaults.env`, which `config.py:925` silently drops). Keep `BRIDGE_HEAL_MAX_ATTEMPTS` at the built-in default 3 (Fork F4).
2. Verify propagation: keepalive reads the `.env` tiers per tick via `lib-common.sh:184-197` — confirm with a manual `lib/keepalive.sh <bot-dir>` run that the gate at `keepalive.sh:130` passes. No bot restarts required.
3. Fix the doc gap: add `OBSERVABILITY_BRIDGE_HEAL` / `BRIDGE_HEAL_MAX_ATTEMPTS` to the schema's env-override documentation, following the "Fleet-pulse escalation (environment overrides)" section's pattern (`documentation/fleet-yaml-schema.md` ~L331), cross-linking `guides/observability.md:125-126`.
4. Controlled live drill on ONE idle bot: SIGKILL its poller (`bot.pid` holder) — SIGKILL, not SIGTERM, so the graceful unlink doesn't run and the pid file goes stale like a real crash… then also run the abandoned-slot variant (SIGTERM a transient we start ourselves) — and observe: `no_bridge` classification, `BRIDGE_HEAL … bounce attempt 1/3` event, `BRIDGE_READY` after the bounce, ladder reset (`poller recovered`) on the next tick.
5. Watch the burn-in window: `claudlobby events` for `BRIDGE_HEAL` / `bridge_down` across the fleet; any heal that fires for a cause other than an induced drill gets a root-cause note before Phase 4 proceeds.

**Validation.** Harness heal scenario (exists since #577) green; live drill evidence (event lines) pasted into the PR/report. Known limitation to document, not fix: heal fires only from the IDLE pane branch (`keepalive.sh:312`) — a busy bot with a dark bridge stays dark until idle; fleet-pulse still nudges the manager meanwhile.

**What NOT to do.** Don't put the flag in `fleet.yaml` `defaults.env` (dropped silently); don't set per-bot `env:` on 16 bots (drift-prone); don't touch `OBSERVABILITY_BRIDGE_DOWN_GRACE` (detection and heal share it deliberately).

---

#### Phase 3 (M) — Upstream plugin patch: prefer-live-holder + slot hygiene + boot audit

**Summary.** Fix the class at the source, upstream in `anthropics/claude-plugins-official` `external_plugins/telegram/server.ts`. Four tightly-coupled changes in one file, one PR (with the issue filed first; split only if maintainers ask):

**Steps.**
1. **Prefer-live-holder** (replaces the L60-69 murder): after the `process.kill(stale, 0)` liveness probe succeeds, check holder freshness (Fork F6: `bot.pid` mtime age < threshold). Fresh → log `deferring to live holder pid=…` and **do not kill, do not claim**; stale or dead → reap/claim as today. Defer behavior: minimal form exits 0; preferred form stays up serving *outbound-only* MCP tools (sendMessage needs no slot) and skips `bot.start()` — maintainer's choice, both satisfy the fleet need.
2. **Heartbeat**: touch `bot.pid` (mtime; content unchanged — bare pid stays, so old readers never break) inside the existing 5s orphan-watchdog interval (L658-664), gated on a `pollerActive` flag that flips false when the polling IIFE returns. Heartbeat means "poll loop still running", not "process exists" — that distinction is what makes the 409-deaf holder reapable.
3. **409 exhaustion releases the slot**: the L1016-1022 branch calls `shutdown()` instead of bare `return` — the process exits, `bot.pid` unlinks, existing fleet detection sees `no_bridge`, heal bounces. The log line finally tells the truth ("Exiting.").
4. **Boot-audit breadcrumb**: append one line per boot and per reap/defer decision to `$STATE_DIR/poller-audit.log`: ts, own pid/ppid, parent argv (best-effort `/proc/<ppid>/cmdline`, try/catch, linux-only), decision (`claimed|reaped pid=N|deferred pid=N`). First hijack attempt after this log exists names the transient definitively (closes the ~80%-subagents attribution question).
5. Prototype every change against a patched **copy** first (harness Phase-2b technique — patch-anchor assert, run scenario suite), then file upstream: issue describing the confirmed mechanism + PR. Track review latency for the Fork F2 fallback trigger.

**Validation.** Extended harness scenario against the patched copy: hijack attempt → `deferring` logged, victim survives, `bot.pid` untouched; dead-holder → still claimed (orphan-reap regression guard); 409-exhaustion (forced via a stub 409 endpoint or grammy error injection — engineering judgment) → process exits AND `bot.pid` gone; audit log contains boot/defer/reap lines. All checks green 3× consecutively.

**What NOT to do.** Don't change the `bot.pid` content format to JSON (breaks `bridge_state` L384-390 numeric parse and any old-plugin coexistence); don't heartbeat from a code path that beats while polling is dead (the watchdog interval must be `pollerActive`-gated); don't add a lock file (deferral IS the lock — a lock file adds stale-lock failure modes).

---

#### Phase 4 (S) — Adopt the fixed plugin in the fleet

**Summary.** Get the patched plugin running fleet-wide, through whichever adoption path Fork F2 lands on, and prove it against the harness and a live drill. **Blocked by Phase 6** — deploying prefer-live-holder without the supervised-boot reap regresses restarts into split-brain (see Phase 6 summary).

**Steps.**
1. Path A (upstream merged): determine the channel-plugin update path empirically — channel plugins load via `--channels plugin:telegram@claude-plugins-official` (`composer.py:339-352`), and the telegram plugin is not in `FLEET_PLUGINS_REQUIRED` by default, so neither `start-bot.sh:204-211` nor `reload-fleet.sh:74` obviously updates it. Observe what refreshes `~/.claude/plugins/cache/...telegram/` (binary update? `claude plugin update telegram@claude-plugins-official` by hand?) and codify the answer in the runbook. Confirm the new version lands (`ls` the cache, grep server.ts for the defer line).
2. Path B (upstream stalled past the F2 trigger): publish the patched plugin in a fork marketplace repo; register via `fleet.plugins.marketplaces` and point `channels` at `plugin:telegram@<fork-marketplace>` in fleet.yaml; `claudlobby generate`; verify composed `--channels` flag and `FLEET_PLUGINS_MARKETPLACES` in `bot.conf`.
3. Re-run the FULL harness against the *installed* plugin (not a patched copy) — the Phase 1 scenario's "hijack occurs" assertions flip to "defer occurs": update the scenario to branch on plugin capability (probe server.ts for the defer marker), so the harness stays truthful across both plugin generations.
4. Live drill on one bot: launch a second claude-family process in the bot's env that starts the telegram MCP; assert via `poller-audit.log` + `bot.pid` that it deferred and the victim survived; assert the bot still replies on Telegram afterward.
5. Rollout order: one bot → observe a day of `poller-audit.log` + events → fleet-wide.

**Validation.** Harness green against installed plugin; drill evidence in PR/report; zero `BRIDGE_HEAL` firings attributable to hijack post-rollout (heal stays as backstop, its silence becomes the success metric).

**What NOT to do.** Don't hot-patch the live plugin cache as the rollout mechanism (any update overwrites it silently — it's fine for prototyping, it is not deployment); don't skip the capability probe in the harness (a pinned-old-plugin host would otherwise fail spuriously or, worse, pass vacuously).

---

#### Phase 5 (M) — Deaf-holder detection: heartbeat freshness in bridge_state

**Summary.** Teach the fleet SSOT predicate that existence is not health. `bridge_state()` gains a staleness gate: if the plugin generation emits heartbeats and `bot.pid` mtime is older than threshold, classify `no_bridge` → keepalive heal bounces it. Catches every deaf-holder mode with a stopped heartbeat — 409-exhaustion residue, poll-loop returns, frozen processes — including causes not yet foreseen. Depends on Phases 3+4 (heartbeat must exist fleet-wide first).

**Steps.**
1. Add the gate to `bridge_state()` after the lineage check (lib-common.sh L428-444): `stat -c %Y` on `bot.pid`, compare against `BRIDGE_HEARTBEAT_STALE_S` (default ~120s — 24× the 5s heartbeat cadence; generous against load-induced delay).
2. **Version-gate it**: only enforce when `BRIDGE_HEARTBEAT_EXPECTED=1` is set (fleet-tier `.env`, set as the last step of Phase 4). Without the gate, every old-plugin bridge (mtime = boot time, ages forever) would read stale → a false `no_bridge` storm → heal bounces the whole healthy fleet. This is the single most dangerous edge in the plan; the flag makes the rollout order enforceable.
3. Extend `tests/test_bridge_state.py` (the "unit-level companion" per its own docstring): fresh-heartbeat → `up`; stale + flag on → `no_bridge`; stale + flag off → `up`; missing file semantics unchanged.
4. Extend the harness: 409-deaf simulation (poller alive, heartbeat stopped) → `bridge_state` says `no_bridge` → heal scenario bounces it.
5. Update `guides/observability.md` and the schema script-read-vars section with the new flag + threshold.

**Validation.** Unit tests green; harness deaf-holder scenario green; one live observation window with the flag on — zero false `no_bridge` across healthy bots (check `claudlobby events`).

**What NOT to do.** Don't enable the staleness gate before the heartbeat plugin is fleet-wide (false-positive storm, see step 2); don't shrink the threshold below ~12× heartbeat cadence; don't add a second detection path outside `bridge_state` (it is the SSOT by design — #454).

---

#### Phase 6 (M) — Supervised-boot slot reclaim (restart-overlap teardown)

**Summary.** Prefer-live-holder has one dangerous interaction: `KillMode=process` (deliberate — `composer.py:604-608`) leaks the old session tree at restart, so the OLD session's poller is alive with a live claude parent and a fresh heartbeat. A naive prefer-live-holder newcomer in the NEW session would *defer to the zombie* — new session bridgeless, telegram traffic flowing to the unsupervised old claude: split brain, undetectable by lineage checks. Today the plugin's murder-reap accidentally papers over this. The fix moves displacement authority to the one place it's legitimate: the supervised boot path reclaims the slot before launching the new session.

**Steps.**
1. In `lib/start-bot.sh`, before launching claude (near the bring-up verify machinery from #457): if `TELEGRAM_STATE_DIR/bot.pid` exists and the holder's `/proc/<pid>/environ` matches THIS bot (reuse the `bridge_state` ownership check — extract a shared helper in `lib-common.sh` rather than duplicating), SIGTERM the holder, wait briefly for the graceful unlink, SIGKILL + unlink on timeout. Log a `SLOT_RECLAIM` breadcrumb to startup.log + events.
2. Scope discipline: this phase reclaims the *slot* only. The broader leaked-tree question (old claude process itself) stays with the existing KillMode tradeoff — rider R2, not this PR.
3. Guard: never fire when `bot.pid` holds the *current* session's poller (can't happen pre-launch, but assert pid ≠ anything spawned by this invocation — cheap insurance ordered before the claude launch).
4. Harness scenario: fake "old session" (poller + stand-in claude parent holding it) → run the start path → assert old poller dead, slot empty, then new poller claims cleanly; with a patched (deferring) plugin — assert the new session does NOT defer to a zombie because the reclaim ran first.

**Validation.** Harness scenario green with both plugin generations; live restart drill on one bot: `systemctl --user restart <bot>` mid-session → `SLOT_RECLAIM` logged when a holdover existed, `BRIDGE_READY` for the new session, exactly one poller for the state dir afterward (`pgrep` census).

**What NOT to do.** Don't change `KillMode` (the comment is right — cgroup cleanup kills the tmux server); don't sweep by name (`pkill -f server.ts` would hit other bots) — environ-match only; don't put the reclaim in the plugin (transients run the plugin too — that's how we got here; only the supervised path has the authority).

---

## Decision Forks

### Fork F1: Fix strategy
- **Context:** Heal (recovery) and the upstream patch (prevention) are independent defenses.
- **Options:** **(a)** heal-only — zero new code, but sessions keep getting murdered+bounced, losing context each time; unsupervised claude runs in bot dirs still kill bridges. **(b)** upstream-only — kills the hijack class, but leaves no recovery for non-hijack dark flavors (the restart-time 30s connect-timeout was real and unexplained; heal covers it). **(c)** both, layered.
- **Lean:** (c). Heal ships in a day and covers unknown-unknowns; prevention makes heal firings rare instead of routine. Neither alone closes the incident.
- **Ratifier:** Chris · **Status:** open

### Fork F2: Fix locus and adoption path for prefer-live-holder
- **Context:** The bug is in plugin code we don't own; version pinning doesn't exist (`validator.py:476-479`), but fork marketplaces do.
- **Options:** **(a)** upstream PR to `anthropics/claude-plugins-official`, heal covers the interim — zero fork-maintenance burden, timeline not ours. **(b)** upstream PR + interim fork-marketplace adoption (`fleet.plugins.marketplaces` + `channels` override — existing machinery, no new claudlobby code) — fastest protection, temporary drift burden, must track upstream releases until re-converged. **(c)** fork-only, no upstream — permanent drift burden on 1000+ lines of TS we don't own; rejected out of hand.
- **Lean:** (a), with a pre-agreed trigger to escalate to (b): another hijack-class incident during review wait, or review stalls past ~2 weeks. Heal (Phase 2) makes waiting cheap.
- **Ratifier:** Chris (external-facing PR to an Anthropic repo) · **Status:** open

### Fork F3: 409-deaf third-flavor handling
- **Context:** Confirmed: current detection classifies a deaf-but-alive holder `up` (every `bridge_state` gate is existence/identity; the 409-exhaustion branch keeps the process alive holding the slot). Heal never fires on it.
- **Options:** **(a)** upstream-only — `shutdown()` on 409 exhaustion releases the slot; existing detection + heal handle the rest with zero fleet-side change. **(b)** fleet-side-only — heartbeat-staleness detection; catches broader deaf modes but leaves the plugin lying about "Exiting." **(c)** both.
- **Lean:** (c). (a) is the correct root fix; (b) additionally catches deaf modes with other causes (and external-consumer 409s where a bounce can't win the slot back — the heal budget escalation then names it for a human).
- **Ratifier:** ari · **Status:** open

### Fork F4: Heal attempt budget and rollout posture
- **Context:** #453 built: cap 3 (`BRIDGE_HEAL_MAX_ATTEMPTS`), 300s shared grace, idle-only, persisted counter, escalate-once at cap, reset on recovery.
- **Options:** **(a)** ship the built defaults untouched. **(b)** tighten (cap 2) to reduce bounce thrash on unfixable causes. **(c)** loosen grace for faster recovery.
- **Lean:** (a) — the machinery was harness-validated at defaults; there is zero burn-in evidence arguing for different numbers. Tune only on data (rider after Phase 2 burn-in).
- **Ratifier:** ari · **Status:** open

### Fork F5: Transient-source isolation (subagent MCP env)
- **Context:** ~80% confidence the hijackers are headless/subagent claude runs inheriting bot env. Prevention could also happen at the source: keep transients from ever launching the channel plugin.
- **Options:** **(a)** isolated `TELEGRAM_STATE_DIR` per transient context — transients get their own harmless slot namespace (and a 409 against the real token if they actually poll: still a live consumer conflict, just not a murder). **(b)** strip the channel plugin from subagent/headless invocations — removes legitimate capability (subagents sometimes need outbound sends) and needs a reliable "am I transient" signal that doesn't exist today. **(c)** none at source — prefer-live-holder makes transients harmless no-ops (defer + exit/outbound-only), and the Phase 3 boot-audit names them so this fork can be re-decided on facts.
- **Lean:** (c). Both (a) and (b) are invasive env special-cases justified only by an attribution that is still 20% uncertain; the defer fix removes the harm regardless of identity. Revisit with `poller-audit.log` data (rider R1).
- **Ratifier:** Chris (fleet-wide subagent env semantics) · **Status:** open

### Fork F6: Holder-liveness signal design
- **Context:** `kill(pid, 0)` proves existence, not polling. The pid file is a bare pid written once; the fleet's `bridge_state` numeric-parses it, so its content format is load-bearing beyond the plugin.
- **Options:** **(a)** pid-alive only — recreates deferral-to-deaf-holder for every stopped-heartbeat mode; rejected by the replication evidence. **(b)** pid-alive + mtime heartbeat (touch `bot.pid` every 5s from the watchdog interval, `pollerActive`-gated; content unchanged) — fully backward/forward compatible with old plugins and the fleet parser. **(c)** JSON `{pid, ts}` rewrite — richer (could carry session identity later) but breaks `bridge_state` L384-390 and old-plugin coexistence during rollout.
- **Lean:** (b). Compatibility during mixed-version rollout is non-negotiable; mtime carries exactly the one bit needed. If upstream later wants JSON, the fleet parser can learn both formats first — sequencing exists precisely for that.
- **Ratifier:** ari · **Status:** open

## Companion Plans

- Fleet-local root-cause findings doc (source evidence): `local/<fleet>/shared/planning/active/2026-07-10-telegram-bridge-dark-poller-root-cause.md`.
- #453 "Telegram bridge heal ladder" epic (merged: #454 bridge_state SSOT, #457 bring-up verify, #473 pulse detect, #577 keepalive heal, #581 restart-path tests) — this plan executes its dormant Fork F6b Tier-2 rollout and extends its detection.
- Lesson `orphaned Telegram poller steals the single-consumer getUpdates slot` (#504) — the earlier sighting of this class.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Upstream review latency (F2a) | hijack class persists during wait | Phase 2 heal live first — recovery in ≤ a few ticks; pre-agreed F2b fork-marketplace trigger |
| False-stale heartbeat under host load → newcomer reaps a live-but-slow holder | self-inflicted bridge kill | threshold ≥ 24× cadence; SIGTERM (graceful, ownership-unlink) not SIGKILL; heal backstops |
| Staleness gate enabled while old plugins still deployed | false `no_bridge` storm → fleet-wide bounce loop | `BRIDGE_HEARTBEAT_EXPECTED` flag + hard sequencing (Phase 5 after Phase 4); harness asserts flag-off behavior |
| Prefer-live-holder deployed without supervised-boot reclaim | restart split-brain: new session defers to zombie old session | Phase 6 explicitly blocks Phase 4; harness scenario proves the ordering |
| Heal bounces an idle bot that was dark for an unfixable cause (revoked token, external 409 consumer) | wasted restarts, noise | `no_token` never bounces (built); budget cap + one-shot escalation names it for a human |
| Harness needs bun + installed plugin + tmux; CI is pytest-only | validation silently skipped on thin hosts | explicit skip-with-reason (existing pattern); PR bodies must cite a real run per the repo's empirical-validation mandate |
| Channel-plugin update path is unverified (not in `FLEET_PLUGINS_REQUIRED`) | Phase 4 Path A stalls on "how does it even update" | Phase 4 step 1 determines it empirically before rollout; Path B needs no answer |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 1 — promote harness | S | — | 2 |
| 2 — enable heal (stopgap) | S | — | 1 |
| 3 — upstream plugin patch | M | 1; forks F2/F3/F6 locked | 6 |
| 4 — adopt fixed plugin | S | 3 (merged or forked), **6** | 5 prep |
| 5 — deaf-holder detection | M | 3+4 (heartbeat fleet-wide) | — |
| 6 — supervised-boot reclaim | M | 1 | 3 |

Critical path: **1 → 3 → 4 → 5**, with 6 landing before 4. Phase 2 is day-one risk reduction independent of everything else. Profile: 3×S, 3×M, 0×L/XL — no phase exceeds a single PR.

## Test Plan

- Promoted harness scenarios (Phases 1, 3, 4, 5, 6) — each phase's PR cites a real run (3× consecutive, identical results) per the repo's empirical-validation mandate; claimed evidence is not evidence.
- Unit layer: `tests/test_bridge_state.py` extensions (Phase 5), `tests/test_validate_harness.py`-pattern wrapper (Phase 1), `tests/test_bash_parse.py` picks up any new lib/ script automatically.
- Live drills: Phase 2 (induced dark → heal), Phase 4 (transient launch → defer), Phase 6 (mid-session restart → reclaim). Each drill's event-log lines land in the PR body.
- Burn-in: after Phases 2 and 4, a multi-day `claudlobby events` watch — heal firings and `bridge_down` counts are the success metric (target: zero unexplained).

## Verification Checklist

- [ ] Promoted harness runs green 3× consecutively on the fleet host (`bash lib/validate-bot-change.sh` scenario output attached)
- [ ] `BRIDGE_HEAL` bounce + recovery observed live on an induced-dark idle bot (event JSONL lines attached)
- [ ] Patched plugin: hijack attempt defers (`poller-audit.log` line), victim survives, dead-holder reap still claims (harness asserts)
- [ ] 409-exhaustion drill: process exits AND `bot.pid` released (was: deaf zombie holding slot)
- [ ] Deaf-holder simulation classified `no_bridge` with `BRIDGE_HEARTBEAT_EXPECTED=1`, `up` without (unit + harness)
- [ ] Mid-session service restart: `SLOT_RECLAIM` fires when a holdover exists; exactly one poller per state dir after (`pgrep` census)
- [ ] `poller-audit.log` exists fleet-wide and names the transient class on first post-rollout event (closes the attribution question)
- [ ] Schema doc documents all bridge-heal/heartbeat vars (`fleet-yaml-schema.md` script-read section)

## What NOT To Do

- **Don't add more reap-before-spawn anywhere.** The reap is the murder weapon, not the fix — the plugin already reaps; that's why there were no orphans to find.
- **Don't serialize with a lock file.** Deferral IS the correct lock semantics; a lock file adds stale-lock failure modes without adding safety.
- **Don't change `bot.pid` to JSON in this workstream** (F6c) — the fleet parser and mixed-version rollout depend on the bare-pid format.
- **Don't flip `KillMode`** — the composer comment is correct that cgroup cleanup kills the tmux server; Phase 6 solves the slot half of the leak, rider R2 owns the rest.
- **Don't hand-edit the live plugin cache as deployment** — every update silently reverts it; prototyping only.
- **Don't enable the staleness gate before the heartbeat plugin is fleet-wide** — false-positive storm that bounces healthy bots.
- **Don't skip the live drills because the harness is green** — the harness proves mechanism on a throwaway namespace; the drills prove the composed fleet behaves.

## Context

- Source skill: forge · Area: `lib/` (keepalive, lib-common, start-bot, validate-bot-change) + upstream `external_plugins/telegram/server.ts` + `documentation/` · Effort: 6 phases (3×S, 3×M) · Risk: Medium (mixed-version rollout sequencing is the sharp edge) · Priority: High (recurring fleet-wide comms outages)
- Riders (decisions deliberately deferred, not phases): **R1** — re-decide Fork F5 with `poller-audit.log` attribution data. **R2** — leaked-session-tree cleanup beyond the slot (KillMode residue). **R3** — promote bridge-heal vars to structured `observability:` config + consider default-on for all fleets, after burn-in. **R4** — retune heal budget/grace on burn-in data.
