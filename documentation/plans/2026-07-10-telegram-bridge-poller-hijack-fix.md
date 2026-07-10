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

This plan (5 phases; revised in ironclad cycle 1) layers three defenses, each empirically validated against the replication harness: **recovery** (enable the already-built, harness-proven #453 keepalive heal ladder — its planned Fork F6b Tier-2 rollout, for which this incident is the gate evidence — delivered as first-class `observability:` config, since the `.env`-tier route was empirically proven a silent no-op), **prevention** (an upstream prefer-live-holder patch to the plugin, with the rightful-owner reap moved to the supervised boot path), and **attribution** (a boot-audit breadcrumb that names the transient processes definitively). Fleet-side deaf-holder *detection* is deliberately deferred to a burn-in-gated rider (R5) per Fork F3's ratified lock: the upstream slot-release fix covers the one observed deaf mode.

## Evidence

Current state, verified 2026-07-10 against the installed plugin and `main` @ c6a7170 (independently re-audited by the cycle-1 correctness lens):

**Plugin** (`~/.claude/plugins/cache/claude-plugins-official/telegram/0.0.6/server.ts`; upstream `anthropics/claude-plugins-official`, `external_plugins/telegram/server.ts`; only 0.0.6 installed):

- L60-69 (module top level): boot reap. Reads `bot.pid`, probes holder with `process.kill(stale, 0)`, writes `replacing stale poller pid=` to stderr, `process.kill(stale, 'SIGTERM')` — **no distinction between an orphaned zombie and the live poller of the current session**. Dead holder (ESRCH) is swallowed by the bare `catch {}` (L68) and the file is claimed (L69).
- L69: `bot.pid` is a bare decimal pid, written **once at boot, never refreshed** (audited: exactly one `writeFileSync` to `PID_FILE` in the file) — no heartbeat signal exists for freshness checks.
- L993-1032: polling loop (`bot.start()`, grammy default long-poll). 409-Conflict branch at L1016-1022: after `attempt >= 8` it logs "…Exiting." and `return`s — **but only exits the async IIFE, not the process**. No `process.exit()`, no `bot.pid` unlink: the MCP stdin keeps the process alive, deaf, holding the slot (the third flavor; the loop's own header comment L989-992 describes this exact failure).
- L636-647 `shutdown()`: ownership-checked unlink (L641: only if `bot.pid` still holds our pid), `bot.stop()` + 2s force-exit. Wired to stdin end/close (L648-649), SIGTERM/SIGINT/SIGHUP (L650-652).
- L654-664 orphan watchdog: 5s interval, self-exits on reparent or destroyed/ended stdin. Catches a poller whose claude *died* — not one whose claude is alive.

**Fleet detection** (`lib/lib-common.sh`, SSOT since #454):

- `bridge_state()` L366-448: gates on handle → token → `bot.pid` exists (L381-382) → pid alive (L384-390) → comm `bun` + args `server.ts` (L395-402) → env ownership via `/proc/<pid>/environ` (L408-412) → live `claude` ancestor via PPID-chain walk (L428-444) → `up`. **Every gate is existence/identity/lineage — nothing observes whether polling actually works** (audited: zero freshness/heartbeat/staleness logic in the file). A 409-deaf poller with a live claude parent passes all gates and reads `up`: it would never be healed.
- `bridge_down_state()` L464-480: wraps with `data/.spawn` grace (default 300s) and collapses to actionable verdicts.

**Heal ladder** (`lib/keepalive.sh`, #453 Phase 5, merged #577 — built, harness-validated, **dormant**):

- `_bridge_heal` L129-162: gated by `OBSERVABILITY_BRIDGE_HEAL` (default `0`, L130); fires only from the `IDLE` pane branch (L312 — a working bot is never bounced); `no_bridge` → `_bridge_heal_bounce` under a per-bot lock; `no_token` → never bounces.
- `_bridge_heal_bounce` L168-195: persisted attempt counter (`data/.bridge-heal`), cap `BRIDGE_HEAL_MAX_ATTEMPTS` (default 3, L170), increments before bouncing, calls the shared `restart_bot_service` ladder (L95-118), emits `BRIDGE_HEAL` events; at cap emits a one-shot `emit_failure_alert … bridge_down` escalation; resets on recovery (L155-159).
- `documentation/guides/observability.md:125-126` documents both vars and states: ships OFF, "enable via a bot/fleet `env:` entry once the gate clears (issue #453 Fork F6b)". **This plan is that gate clearing — but not via `env:`, see below.**

**How the heal flag actually reaches keepalive (cycle-1 correction — the original Phase 2 mechanism was a silent no-op):**

- `keepalive.sh:23` loads env via `load_bot_conf` **only** — bot.conf plus inherited process env. The tiered `.env` chain (`~/.env` → `local/<fleet>/.env` → `<bot>/.env`) is sourced **only** by `start-bot.sh:53` (`source_env_tiered`); no systemd unit sets an `EnvironmentFile`. The in-code comment at `lib-common.sh:343-344` states it outright: the `.env` chain is what "keepalive/fleet-pulse never source" (the one exception is the scoped token resolver `resolve_bot_telegram_token`, lib-common.sh:353-366, which sources the tiers *in a subshell* to resolve a single named value).
- **Empirically proven two-sided** (cycle 1, real `keepalive.sh` + recorder-stub restart ladder + idle tmux pane, private namespaces): `OBSERVABILITY_BRIDGE_HEAL=1` placed *only* in `local/<fleet>/.env` → gate at `keepalive.sh:130` stays closed, no bounce, no `BRIDGE_HEAL` event (the no-op). The same flag in **`bot.conf`** → gate opens, `_bridge_heal` reaches the restart ladder (recorder counted exactly one bounce) and emits `BRIDGE_HEAL`. bot.conf is therefore the delivery vehicle; the composer must emit it.
- Bridge-heal vars are today plain env passthrough, not structured `observability:` fields (`config.py:81-97` has only 4 fields; `composer.py:419-447` emits only those; grep for `BRIDGE_HEAL|OBSERVABILITY_BRIDGE` across `config.py`/`composer.py`/`validator.py`: zero matches). Per-bot `env:` reaches `bot.conf` via `composer.py:530-533`, but **`defaults.env` is never merged** — `config.py:925` reads per-bot `env:` only (the line above it merges `mounts`, corroborating the gap is real; in-code warning at `validator.py:251-253`). The structured `observability:` block, by contrast, **does** merge fleet-wide (`_coerce_observability` config.py:655, `_merge_observability` config.py:686, applied at config.py:920) — which is exactly why Phase 2 promotes the heal vars into it.

**Composition constraints** (why the phases are shaped the way they are):

- **Plugin version pinning does not exist**: plugins are strictly `name@marketplace` (`validator.py:476-479` — a `warnings.append`, so it doesn't hard-block `generate`, but no version syntax exists to pass); install/update pass the name verbatim (`start-bot.sh:204-211`, `reload-fleet.sh:74`). Custom fork **marketplaces** are supported (`fleet.plugins.marketplaces`, `config.py:376-390`, emitted at `composer.py:519-528`), and the telegram channel is per-bot config (`config.py:329` default `plugin:telegram@claude-plugins-official`, composed at `composer.py:339-352`; hardcoded fallback `start-bot.sh:162`) — so an interim fork adoption needs **zero new claudlobby code**.
- `KillMode=process` is deliberate (`composer.py:603-608` — cgroup cleanup would kill the tmux server). The composed unit's stop path is `ExecStop=tmux -L <bot_service> kill-server` (composer.py:610-613): it tears down the bot's private tmux server, **but observed journal evidence shows pane processes survive it** — "Unit process … (claude) remains running after unit stopped" (×4 children, production, 2026-07-10). kill-server plus KillMode=process therefore leaks the old session tree (claude and its MCP poller child) across a restart. This observed leak is the premise for Phase 5.

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
recovery     keepalive heal ladder (#453, enabled via first-class observability
             config): no_bridge on an idle bot → bounded bounce → escalate at
             budget; covers every dark flavor regardless of cause, including
             ones this plan didn't foresee
attribution  poller-audit.log breadcrumbs name every process that ever touches
             the slot — the transient-identity question answers itself
detection+   (rider R5, burn-in-gated) bridge_state learns heartbeat freshness
             so a deaf holder reads no_bridge — built only if burn-in surfaces
             a deaf mode the upstream slot-release doesn't already eliminate
```

Design principle: the plugin becomes *polite* (never kills a live peer), the supervisor becomes *authoritative* (only the supervised boot displaces a holder), and the fleet layer recovers from anything.

## Implementation Plan

### Dependencies

None — Phase 1 and Phase 2 start immediately and independently.

### Blocks

Completes the #453 epic (Fork F6b Tier-2 rollout). Unblocks retiring the recurring manual "bridge dark again" firefights and the transient-identity investigation (rider R1).

### Steps

Phases in dependency order. Every phase carries its own validation against the promoted harness — the harness IS the dummy-bot test; no phase ships on claimed behavior.

---

#### Phase 1 (S) — Promote the hijack repro into the validation harness

**Summary.** Promote the bot-local `repro-bridge-hijack.sh` into the repo as the executable proof of the hijack mechanism and the regression gate for every later phase. Follow the established pattern: a scenario **in `lib/validate-bot-change.sh`** (the #453 heal scenario already lives there at L273-378) plus a pytest subprocess wrapper (`tests/test_validate_harness.py` precedent), with per-run `TMUX_TMPDIR` isolation (#586). One harness, one scaffolding — a separate sibling script is out (cycle 1): the split trigger is "cannot share the scaffolding," not size, and the scaffolding (throwaway fleet, shadow tmux, recorder stubs, cleanup traps) is exactly what this scenario reuses.

**Steps.**
1. Add a `bridge-hijack` scenario to `lib/validate-bot-change.sh`: live poller A (real plugin, fake token `8888888:AAA…`, private `TELEGRAM_STATE_DIR`) → newcomer B same state dir → assert `replacing stale poller pid=A` + A dies gracefully + `bot.pid`=B → B stdin-EOF → assert slot abandoned, no poller returns.
2. Generalize the hardcoded plugin path: resolve the newest installed `~/.claude/plugins/cache/claude-plugins-official/telegram/<version>/` (the current script pins `0.0.6`).
3. Keep the patch-anchor assertion (`assert src.count(old) == 1 … refusing to patch`) so plugin drift fails loudly instead of silently testing nothing.
4. Skip semantics for CI: the scenario requires `bun`, `tmux`, and the installed plugin — skip cleanly (with a printed reason) when absent, mirroring `tests/test_validate_harness.py` `skipif`. CI runs pytest only; the wrapper is the enforcement point on hosts that have the deps.
5. Throwaway-bot `bot.conf`s in the scenario must set `BOT_SERVICE="tmux-<name>"` matching the shadow socket — `tmux_socket_for_bot` refuses an empty `BOT_SERVICE` when `FLEET_NAME` is set (cross-fleet collision guard; hit and fixed during the cycle-1 proof rig).
6. Retire the bot-local script in favor of the promoted one (single source; no fork of the harness left behind).

**Validation.** Run the promoted scenario 3× consecutively — identical pass counts; run existing `validate-bot-change.sh` scenarios — no regression; `pytest tests/test_validate_harness.py` (or the new wrapper) green locally.

**What NOT to do.** Don't stub the poller here — the point of this scenario is the *real* plugin binary exhibiting the real reap; recorder stubs stay for the restart ladder only. Don't fork the harness scaffolding into a sibling script.

---

#### Phase 2 (M) — Enable the bridge heal fleet-wide via first-class observability config (executes #453 Fork F6b Tier-2)

**Summary.** Turn on the dormant, harness-validated keepalive heal for the fleet — through the **structured `observability:` block**, because the obvious routes were proven broken during planning: `defaults.env` is silently dropped (`config.py:925`) and the fleet-tier `.env` never reaches keepalive (empirically confirmed no-op — keepalive loads `bot.conf` only, `keepalive.sh:23`). bot.conf is the proven delivery vehicle (cycle-1 rig: flag in bot.conf → gate opens → ladder fires → `BRIDGE_HEAL` emitted), and the observability block is the existing first-class path that merges fleet-wide and lands in bot.conf. This folds former rider R3 into the phase as a requirement. Converts a hijack from permanent-dark into dark-for-≤-a-few-keepalive-ticks; ships first because it covers **all** dark flavors — including the observed restart-time 30s-MCP-connect-timeout flavor that prevention can't reach.

**Steps.**
1. `config.py`: add `bridge_heal: bool | None` and `bridge_heal_max_attempts: int | None` to `ObservabilityConfig` (L81-97), following the existing 4-field pattern through `_coerce_observability` (L655) and `_merge_observability` (L686) so `defaults.observability` merges fleet-wide with per-bot override.
2. `composer.py`: emit `OBSERVABILITY_BRIDGE_HEAL` / `BRIDGE_HEAL_MAX_ATTEMPTS` from the observability block (L419-447), only when set — absent fields keep today's bot.conf byte-identical.
3. `validator.py`: bounds-check (`bridge_heal` boolean; `bridge_heal_max_attempts` 1..10), same shape as the pulse-interval checks (L180-199).
4. Docs: schema (`fleet-yaml-schema.md` observability section + emitted-env list) and `guides/observability.md:125-126` (enable instructions change from "a bot/fleet `env:` entry" to the observability field; note explicitly that `.env` tiers do NOT reach keepalive). `OBSERVABILITY_BRIDGE_DOWN_GRACE` stays env-passthrough (working default, F4 ships defaults untouched) — noted, not promoted.
5. Tests: `test_config.py` (coerce/merge incl. fleet-default + bot-override) and `test_composer.py` (emission present when set, absent when not) — crib the existing observability tests.
6. Fleet rollout: set `defaults.observability.bridge_heal: true` in the fleet's `fleet.yaml`; `claudlobby generate`; **verify the composed `bot.conf` carries `OBSERVABILITY_BRIDGE_HEAL=1`** on every bot; `claudlobby diff` shows only the new lines.
7. Prove delivery end-to-end (the cycle-1 rig, now against a composed bot.conf): run `lib/keepalive.sh <bot-dir>` and confirm the gate at `keepalive.sh:130` passes and `_bridge_heal` is reached (recorder-stub or event evidence). No bot restarts required for keepalive to see it (keepalive re-loads bot.conf every tick).
8. Controlled live drill on ONE idle bot: SIGKILL its poller (stale pid file, like a real crash) and separately SIGTERM-abandon a transient we start ourselves — observe: `no_bridge` classification, `BRIDGE_HEAL … bounce attempt 1/3` event, `BRIDGE_READY` after the bounce, ladder reset (`poller recovered`) on the next tick.
9. Burn-in watch: `claudlobby events` for `BRIDGE_HEAL` / `bridge_down` across the fleet; any heal that fires for a cause other than an induced drill gets a root-cause note before Phase 4 proceeds.

**Validation.** Unit tests green; composed-bot.conf keepalive gate-pass evidence (step 7) and live-drill event lines (step 8) pasted into the PR. Known limitation to document, not fix: heal fires only from the IDLE pane branch (`keepalive.sh:312`) — a busy bot with a dark bridge stays dark until idle; fleet-pulse still nudges the manager meanwhile.

**What NOT to do.** Don't put the flag in `fleet.yaml` `defaults.env` (dropped silently at `config.py:925`); don't put it in the fleet-tier `.env` (empirically proven to never reach keepalive — the cycle-1 critical finding); don't set per-bot `env:` on 16 bots (drift-prone); don't touch `OBSERVABILITY_BRIDGE_DOWN_GRACE` (detection and heal share it deliberately).

---

#### Phase 3 (M) — Upstream plugin patch: prefer-live-holder + slot hygiene + boot audit

**Summary.** Fix the class at the source, upstream in `anthropics/claude-plugins-official` `external_plugins/telegram/server.ts`. Four tightly-coupled changes in one file, one PR (with the issue filed first; split only if maintainers ask):

**Steps.**
1. **Prefer-live-holder** (replaces the L60-69 murder): after the `process.kill(stale, 0)` liveness probe succeeds, check holder freshness (Fork F6, locked: `bot.pid` mtime age < threshold). Fresh → log `deferring to live holder pid=…` and **do not kill, do not claim**; stale or dead → reap/claim as today. Implementation note (cycle 1): the dead-holder path deliberately keeps the existing semantics — ESRCH swallowed by the bare `catch {}` (L68), then claim — including its narrow, pre-existing pid-reuse window; environ-verification of holders stays a fleet-side concern (Phase 5), not a plugin concern. Defer behavior: minimal form exits 0; preferred form stays up serving *outbound-only* MCP tools (sendMessage needs no slot) and skips `bot.start()` — maintainer's choice, both satisfy the fleet need.
2. **Heartbeat**: touch `bot.pid` (mtime; content unchanged — bare pid stays, so old readers never break) inside the existing 5s orphan-watchdog interval (L658-664), gated on a `pollerActive` flag that flips false when the polling IIFE returns. Heartbeat means "poll loop still running", not "process exists" — that distinction is what makes the 409-deaf holder reapable.
3. **409 exhaustion releases the slot**: the L1016-1022 branch calls `shutdown()` instead of bare `return` — the process exits, `bot.pid` unlinks, existing fleet detection sees `no_bridge`, heal bounces. The log line finally tells the truth ("Exiting."). This is the ratified F3(a) leg: the one *observed* deaf mode fixed at its source.
4. **Boot-audit breadcrumb**: append one line per boot and per reap/defer decision to `$STATE_DIR/poller-audit.log`: ts, own pid/ppid, parent argv (best-effort `/proc/<ppid>/cmdline`; on macOS fall back to `ps -o args= -p <ppid>`, degrade gracefully — attribution fidelity is best-effort off-Linux), decision (`claimed|reaped pid=N|deferred pid=N`). Retention: size-guard on boot (truncate to the last ~200 lines when the file exceeds ~256 KB) — no external rotation dependency. First hijack attempt after this log exists names the transient definitively (closes the ~80%-subagents attribution question).
5. Prototype every change against a patched **copy** first (harness Phase-2b technique — patch-anchor assert, run scenario suite), then file upstream: issue describing the confirmed mechanism + PR. Track review latency against the F2 escalation trigger (written gate in Fork F2).

**Validation.** Extended harness scenario against the patched copy: hijack attempt → `deferring` logged, victim survives, `bot.pid` untouched; dead-holder → still claimed (orphan-reap regression guard); 409-exhaustion (forced via a stub 409 endpoint or grammy error injection — engineering judgment) → process exits AND `bot.pid` gone; audit log contains boot/defer/reap lines. All checks green 3× consecutively. The patched-copy prototype artifact from this step is what Phase 5's validation consumes — Phase 5 does not wait for upstream merge.

**What NOT to do.** Don't change the `bot.pid` content format to JSON (breaks `bridge_state` L384-390 numeric parse and any old-plugin coexistence); don't heartbeat from a code path that beats while polling is dead (the watchdog interval must be `pollerActive`-gated); don't add a lock file (deferral IS the lock — a lock file adds stale-lock failure modes).

---

#### Phase 4 (S) — Adopt the fixed plugin in the fleet

**Summary.** Get the patched plugin running fleet-wide, through whichever adoption path Fork F2 lands on, and prove it against the harness and a live drill. **Mechanically gated on Phase 5** — deploying prefer-live-holder without the supervised-boot reclaim regresses restarts into split-brain (see Phase 5 summary). The gate is enforced by the harness and the checklist, not prose (cycle 1).

**Steps.**
1. **Mechanical gate (before anything deploys):** the Phase 4 harness scenario opens by probing both sides — the installed plugin for the defer marker AND `lib/start-bot.sh` for the `SLOT_RECLAIM` marker. Deferring plugin + missing reclaim ⇒ the scenario **hard-fails** (not skips) with "Phase 5 gate violated". Additionally, the rollout checklist requires `git merge-base --is-ancestor <Phase-5-merge-sha> HEAD` to pass on the fleet install before the first bot flips.
2. Path A (upstream merged): determine the channel-plugin update path empirically — channel plugins load via `--channels plugin:telegram@claude-plugins-official` (`composer.py:339-352`), and the telegram plugin is not in `FLEET_PLUGINS_REQUIRED` by default, so neither `start-bot.sh:204-211` nor `reload-fleet.sh:74` obviously updates it. Observe what refreshes `~/.claude/plugins/cache/...telegram/` (binary update? `claude plugin update telegram@claude-plugins-official` by hand?) and codify the answer in the runbook. Confirm the new version lands (`ls` the cache, grep server.ts for the defer line).
3. Path B (upstream stalled past the F2 written trigger): publish the patched plugin in a fork marketplace repo; register via `fleet.plugins.marketplaces` and point `channels` at `plugin:telegram@<fork-marketplace>` in fleet.yaml; `claudlobby generate`; verify composed `--channels` flag and `FLEET_PLUGINS_MARKETPLACES` in `bot.conf`.
4. Re-run the FULL harness against the *installed* plugin (not a patched copy) — the Phase 1 scenario's "hijack occurs" assertions flip to "defer occurs": the scenario branches on plugin capability (probe server.ts for the defer marker), so the harness stays truthful across both plugin generations.
5. Live drill on one bot: launch a second claude-family process in the bot's env that starts the telegram MCP; assert via `poller-audit.log` + `bot.pid` that it deferred and the victim survived; assert the bot still replies on Telegram afterward.
6. Canary rollout with a written abort/rollback: one bot → a day of `poller-audit.log` + events → fleet-wide. **Abort criteria:** any missed-reply report, any `BRIDGE_HEAL` firing attributable to defer behavior, any split-brain sighting (two pollers or a zombie-session reply). **Rollback:** flip `channels` back to `plugin:telegram@claude-plugins-official` (Path B) or pin the prior cache version by hand while escalating (Path A), `claudlobby generate`, restart the canary — heal (Phase 2) covers the gap while rolled back.

**Validation.** Gate probe exercised both ways (deferring-plugin + missing-reclaim hard-fails; with reclaim present, passes); harness green against installed plugin; drill evidence in PR/report; zero `BRIDGE_HEAL` firings attributable to hijack post-rollout (heal stays as backstop, its silence becomes the success metric).

**What NOT to do.** Don't hot-patch the live plugin cache as the rollout mechanism (any update overwrites it silently — it's fine for prototyping, it is not deployment); don't skip the capability probe in the harness (a pinned-old-plugin host would otherwise fail spuriously or, worse, pass vacuously); don't treat the Phase 5 gate as advisory — it is a hard-fail assertion.

---

#### Phase 5 (M) — Supervised-boot slot reclaim (restart-overlap teardown)

**Summary.** Prefer-live-holder has one dangerous interaction: the composed stop path (`ExecStop=tmux -L <svc> kill-server`, composer.py:610-613) plus `KillMode=process` (deliberate — composer.py:603-608) does **not** reliably end the session tree — kill-server tears down the tmux server, but the observed production journal shows claude and its children surviving it ("remains running after unit stopped", ×4). So across a restart, the OLD session's poller can be alive with a live claude parent and a fresh heartbeat. A naive prefer-live-holder newcomer in the NEW session would *defer to the zombie* — new session bridgeless, telegram traffic flowing to the unsupervised old claude: split brain, undetectable by lineage checks. Today the plugin's murder-reap accidentally papers over this. The fix moves displacement authority to the one place it's legitimate: the supervised boot path reclaims the slot before launching the new session.

**Steps.**
1. In `lib/start-bot.sh`, before launching claude (near the bring-up verify machinery from #457): if `TELEGRAM_STATE_DIR/bot.pid` exists and the holder's `/proc/<pid>/environ` matches THIS bot, SIGTERM the holder, wait briefly for the graceful unlink, SIGKILL + unlink on timeout. Log a `SLOT_RECLAIM` breadcrumb to startup.log + events. (The `SLOT_RECLAIM` string in start-bot.sh doubles as the Phase 4 gate marker.)
2. **Ownership-helper extraction with an explicit regression requirement (cycle 1):** the environ-ownership check is extracted from `bridge_state()` into a shared `lib-common.sh` helper used by both callers. The extraction must be behavior-identical: `tests/test_bridge_state.py` passes **unmodified** (it is the SSOT's contract — #454/#577 precedent shows exactly this shape regressing), plus new helper-level unit tests for the extracted function (match/no-match/unreadable-environ → the `unknown` semantics callers must not heal on).
3. Known, accepted residual (cycle 1): a narrow pre-existing pid-reuse race (pid read from file, then acted on by number) is inherited by design; the environ-match immediately before signaling is precisely the mitigation, and the window is the gap between check and kill. Not new to this phase; documented, not fixed.
4. Scope discipline: this phase reclaims the *slot* only. The broader leaked-tree question (the surviving old claude process itself) stays with the existing KillMode/ExecStop tradeoff — rider R2, not this PR.
5. Guard: never fire when `bot.pid` holds the *current* session's poller (can't happen pre-launch, but assert pid ≠ anything spawned by this invocation — cheap insurance ordered before the claude launch).
6. Harness scenario: fake "old session" (poller + stand-in claude parent holding it) → run the start path → assert old poller dead, slot empty, then new poller claims cleanly; with the Phase 3 patched-copy prototype (deferring plugin) — assert the new session does NOT defer to a zombie because the reclaim ran first. Depends on Phase 1 (scaffolding) + Phase 3's prototype artifact only — runs in parallel with Phase 3's upstream review, not after merge.

**Validation.** Harness scenario green with both plugin generations; `test_bridge_state.py` green unmodified + new helper tests; live restart drill on one bot: `systemctl --user restart <bot>` mid-session → `SLOT_RECLAIM` logged when a holdover existed, `BRIDGE_READY` for the new session, exactly one poller for the state dir afterward (`pgrep` census).

**What NOT to do.** Don't change `KillMode` or `ExecStop` (the comments are right — cgroup cleanup kills the tmux server; kill-server is still the correct tmux teardown); don't sweep by name (`pkill -f server.ts` would hit other bots) — environ-match only; don't put the reclaim in the plugin (transients run the plugin too — that's how we got here; only the supervised path has the authority); don't rewrite `bridge_state()` while extracting the helper — extraction is behavior-identical by test contract.

---

## Decision Forks

### Fork F1: Fix strategy
- **Context:** Heal (recovery) and the upstream patch (prevention) are independent defenses.
- **Options:** **(a)** heal-only — zero new code, but sessions keep getting murdered+bounced, losing context each time; unsupervised claude runs in bot dirs still kill bridges. **(b)** upstream-only — kills the hijack class, but leaves no recovery for non-hijack dark flavors (the restart-time 30s connect-timeout was real and unexplained; heal covers it). **(c)** both, layered.
- **Lean:** (c). Heal ships fast and covers unknown-unknowns; prevention makes heal firings rare instead of routine. Neither alone closes the incident. Cycle-1 lens concurrence: cost-benefit and correctness both back (c).
- **Ratifier:** Chris · **Status:** leaning (pending Chris)

### Fork F2: Fix locus and adoption path for prefer-live-holder
- **Context:** The bug is in plugin code we don't own; version pinning doesn't exist (`validator.py:476-479`), but fork marketplaces do.
- **Options:** **(a)** upstream PR to `anthropics/claude-plugins-official`, heal covers the interim — zero fork-maintenance burden, timeline not ours. **(b)** upstream PR + interim fork-marketplace adoption (`fleet.plugins.marketplaces` + `channels` override — existing machinery, no new claudlobby code) — fastest protection, temporary drift burden, must track upstream releases until re-converged. **(c)** fork-only, no upstream — permanent drift burden on 1000+ lines of TS we don't own; rejected out of hand.
- **Lean:** (a), with the escalation trigger as a **written gate** (cycle 1): escalate to (b) when EITHER another hijack-class incident occurs during the wait, OR upstream review is silent past 2 weeks from PR open. The interim safety of (a) is explicitly conditional on Phase 2 (heal) being live first — sequencing already enforces that.
- **Ratifier:** Chris (external-facing PR to an Anthropic repo) · **Status:** leaning (pending Chris)

### Fork F3: 409-deaf third-flavor handling
- **Context:** Confirmed: current detection classifies a deaf-but-alive holder `up` (every `bridge_state` gate is existence/identity; the 409-exhaustion branch keeps the process alive holding the slot). Heal never fires on it.
- **Options:** **(a)** upstream-only — `shutdown()` on 409 exhaustion releases the slot; existing detection + heal handle the rest with zero fleet-side change. **(b)** fleet-side-only — heartbeat-staleness detection; catches broader deaf modes but leaves the plugin lying about "Exiting." **(c)** both.
- **Decision:** **(a) now, (b) deferred to burn-in-gated rider R5; eventual target (c)** if burn-in ever surfaces a deaf mode the slot-release doesn't eliminate. Rationale: (a) fixes the one *observed* deaf mode at its source with zero fleet-side surface; the fleet-side leg was speculative insurance whose cycle-1 findings (flag-op gap, `stat` portability, threshold-by-assertion) all attached to building it now. Lens preferences recorded: risk (c), cost-benefit (a)+rider, correctness (c), substrate-fit (c) — ratifier weighed cost-benefit's unobserved-residual argument.
- **Ratifier:** ari · **Status:** **locked** — ratified in the cycle-1 revision dispatch (2026-07-10); formal `[FORK-LOCK F3]` PR comment to be posted by the ratifier for the comment-ledger record.

### Fork F4: Heal attempt budget and rollout posture
- **Context:** #453 built: cap 3 (`BRIDGE_HEAL_MAX_ATTEMPTS`), 300s shared grace, idle-only, persisted counter, escalate-once at cap, reset on recovery.
- **Options:** **(a)** ship the built defaults untouched. **(b)** tighten (cap 2). **(c)** loosen grace.
- **Decision:** **(a)** — the machinery was harness-validated at defaults; there is zero burn-in evidence arguing for different numbers. Tune only on data (rider R4). Risk lens concurred: shipping unvalidated speculative numbers is the worse risk.
- **Ratifier:** ari · **Status:** **locked** — ratified in the cycle-1 revision dispatch (2026-07-10); formal `[FORK-LOCK F4]` comment to follow.

### Fork F5: Transient-source isolation (subagent MCP env)
- **Context:** ~80% confidence the hijackers are headless/subagent claude runs inheriting bot env. Prevention could also happen at the source: keep transients from ever launching the channel plugin.
- **Options:** **(a)** isolated `TELEGRAM_STATE_DIR` per transient context — transients get their own harmless slot namespace (and a 409 against the real token if they actually poll: still a live consumer conflict, just not a murder). **(b)** strip the channel plugin from subagent/headless invocations — removes legitimate capability (subagents sometimes need outbound sends) and needs a reliable "am I transient" signal that doesn't exist today. **(c)** none at source — prefer-live-holder makes transients harmless no-ops (defer + exit/outbound-only), and the Phase 3 boot-audit names them so this fork can be re-decided on facts.
- **Lean:** (c). Both (a) and (b) are invasive env special-cases justified only by an attribution that is still 20% uncertain; the defer fix removes the harm regardless of identity. Revisit with `poller-audit.log` data (rider R1). Cost-benefit lens concurred.
- **Ratifier:** Chris (fleet-wide subagent env semantics) · **Status:** leaning (pending Chris)

### Fork F6: Holder-liveness signal design
- **Context:** `kill(pid, 0)` proves existence, not polling. The pid file is a bare pid written once; the fleet's `bridge_state` numeric-parses it, so its content format is load-bearing beyond the plugin.
- **Options:** **(a)** pid-alive only — recreates deferral-to-deaf-holder for every stopped-heartbeat mode; rejected by the replication evidence. **(b)** pid-alive + mtime heartbeat (touch `bot.pid` every 5s from the watchdog interval, `pollerActive`-gated; content unchanged) — fully backward/forward compatible with old plugins and the fleet parser. **(c)** JSON `{pid, ts}` rewrite — richer but breaks `bridge_state` L384-390 and old-plugin coexistence during rollout.
- **Decision:** **(b)** — compatibility during mixed-version rollout is non-negotiable; mtime carries exactly the one bit needed. If upstream later wants JSON, the fleet parser learns both formats first. Lens concurrence: risk, correctness, and substrate-fit all independently backed (b).
- **Ratifier:** ari · **Status:** **locked** — ratified in the cycle-1 revision dispatch (2026-07-10); formal `[FORK-LOCK F6]` comment to follow.

## Companion Plans

- Fleet-local root-cause findings doc (source evidence): `local/<fleet>/shared/planning/active/2026-07-10-telegram-bridge-dark-poller-root-cause.md`.
- #453 "Telegram bridge heal ladder" epic (merged: #454 bridge_state SSOT, #457 bring-up verify, #473 pulse detect, #577 keepalive heal, #581 restart-path tests) — this plan executes its dormant Fork F6b Tier-2 rollout.
- Lesson `orphaned Telegram poller steals the single-consumer getUpdates slot` (#504) — the earlier sighting of this class.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Upstream review latency (F2a) | hijack class persists during wait | Phase 2 heal live first — recovery in ≤ a few ticks; written F2 escalation trigger (incident OR 2-week silence → fork-marketplace path) |
| Phase 2 touches compositor core (config/composer/validator) — every bot's bot.conf regenerates | composition regression fleet-wide | crib the existing 4-field observability pattern; unit tests both ways (set/unset); `claudlobby diff` must show ONLY the new lines before rollout |
| False-stale heartbeat under host load → newcomer reaps a live-but-slow holder | self-inflicted bridge kill | threshold ≥ 24× cadence; SIGTERM (graceful, ownership-unlink) not SIGKILL; heal backstops |
| Prefer-live-holder deployed without supervised-boot reclaim | restart split-brain: new session defers to zombie old session | Phase 4's mechanical gate: harness hard-fails on deferring-plugin + missing `SLOT_RECLAIM`; merge-ancestry checklist item |
| Heal bounces an idle bot that was dark for an unfixable cause (revoked token, external 409 consumer) | wasted restarts, noise | `no_token` never bounces (built); budget cap + one-shot escalation names it for a human |
| Harness needs bun + installed plugin + tmux; CI is pytest-only | validation silently skipped on thin hosts | explicit skip-with-reason (existing pattern); PR bodies must cite a real run per the repo's empirical-validation mandate |
| Channel-plugin update path is unverified (not in `FLEET_PLUGINS_REQUIRED`) | Phase 4 Path A stalls on "how does it even update" | Phase 4 step 2 determines it empirically before rollout; Path B needs no answer |
| Adopted plugin misbehaves on the canary | inbound loss on one bot | Phase 4 step 6 written abort criteria + rollback (channels flip / prior version), heal covers the rolled-back gap |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 1 — promote harness | S | — | 2 |
| 2 — heal via observability config | M | — | 1 |
| 3 — upstream plugin patch | M | 1; F6 locked (done), F2 pending Chris | 5 (once 3's prototype exists) |
| 4 — adopt fixed plugin | S | 3 (merged or forked) + **5 (mechanical gate)** + 2 (heal live) | — |
| 5 — supervised-boot reclaim | M | 1 + Phase 3's patched-copy prototype artifact (not upstream merge) | 3's upstream review |

Critical path: **1 → 3 → 5 → 4**, with 2 landing day-one in parallel and gating 4 (heal must be live before adoption). Profile: 2×S, 3×M, 0×L/XL — no phase exceeds a single PR. (Former Phase 5, fleet-side deaf detection, is rider R5 — not on any path.)

## Test Plan

- Promoted harness scenarios (Phases 1, 3, 4, 5) — each phase's PR cites a real run (3× consecutive, identical results) per the repo's empirical-validation mandate; claimed evidence is not evidence.
- Unit layer: `test_config.py`/`test_composer.py` observability extensions (Phase 2), `tests/test_bridge_state.py` unmodified-green + new ownership-helper tests (Phase 5), `tests/test_validate_harness.py`-pattern wrapper (Phase 1), `tests/test_bash_parse.py` picks up lib/ edits automatically.
- Live drills: Phase 2 (induced dark → heal, plus composed-bot.conf gate-pass proof), Phase 4 (transient launch → defer), Phase 5 (mid-session restart → reclaim). Each drill's event-log lines land in the PR body.
- Burn-in: after Phases 2 and 4, a multi-day `claudlobby events` watch — heal firings and `bridge_down` counts are the success metric (target: zero unexplained). Burn-in evidence also feeds riders R4/R5.

## Verification Checklist

- [ ] Promoted harness runs green 3× consecutively on the fleet host (`bash lib/validate-bot-change.sh` scenario output attached)
- [ ] Composed `bot.conf` carries `OBSERVABILITY_BRIDGE_HEAL=1` fleet-wide after `generate`, and a real `keepalive.sh` run against a composed bot passes the `keepalive.sh:130` gate and reaches `_bridge_heal` (the cycle-1 two-sided rig, re-run against composed output)
- [ ] `BRIDGE_HEAL` bounce + recovery observed live on an induced-dark idle bot (event JSONL lines attached)
- [ ] Patched plugin: hijack attempt defers (`poller-audit.log` line), victim survives, dead-holder reap still claims (harness asserts)
- [ ] 409-exhaustion drill: process exits AND `bot.pid` released (was: deaf zombie holding slot)
- [ ] Phase 4 gate probe exercised both ways: deferring-plugin + missing `SLOT_RECLAIM` marker hard-fails; with Phase 5 merged, passes
- [ ] Mid-session service restart: `SLOT_RECLAIM` fires when a holdover existed; exactly one poller per state dir after (`pgrep` census)
- [ ] `tests/test_bridge_state.py` green **unmodified** after the ownership-helper extraction, plus new helper-level tests
- [ ] `poller-audit.log` exists fleet-wide and names the transient class on first post-rollout event (closes the attribution question)
- [ ] Schema + observability guide document the new `observability.bridge_heal` fields (and explicitly warn that `.env` tiers never reach keepalive)

## What NOT To Do

- **Don't add more reap-before-spawn anywhere.** The reap is the murder weapon, not the fix — the plugin already reaps; that's why there were no orphans to find.
- **Don't serialize with a lock file.** Deferral IS the correct lock semantics; a lock file adds stale-lock failure modes without adding safety.
- **Don't change `bot.pid` to JSON in this workstream** (F6c, rejected in the lock) — the fleet parser and mixed-version rollout depend on the bare-pid format.
- **Don't flip `KillMode` or remove the `ExecStop` kill-server** — both are deliberate; Phase 5 solves the slot half of the leak, rider R2 owns the rest.
- **Don't deliver the heal flag via `.env` tiers or `defaults.env`** — both empirically/structurally proven not to reach keepalive (cycle-1 critical finding); the observability block is the vehicle.
- **Don't hand-edit the live plugin cache as deployment** — every update silently reverts it; prototyping only.
- **Don't build fleet-side heartbeat detection now** — rider R5 is burn-in-gated by the F3 lock; building it early re-imports the exact findings (flag gaps, portability, asserted thresholds) cycle 1 removed.
- **Don't skip the live drills because the harness is green** — the harness proves mechanism on a throwaway namespace; the drills prove the composed fleet behaves.

## Context

- Source skill: forge (ironclad cycle 1 folded) · Area: `lib/` (keepalive, lib-common, start-bot, validate-bot-change) + `claudlobby/` (config, composer, validator) + upstream `external_plugins/telegram/server.ts` + `documentation/` · Effort: 5 phases (2×S, 3×M) · Risk: Medium (compositor-core touch and mixed-version rollout sequencing are the sharp edges) · Priority: High (recurring fleet-wide comms outages)
- Riders (decisions deliberately deferred, not phases): **R1** — re-decide Fork F5 with `poller-audit.log` attribution data. **R2** — leaked-session-tree cleanup beyond the slot (ExecStop/KillMode residue). **R4** — retune heal budget/grace on burn-in data. **R5** (new, from the F3 lock) — fleet-side deaf-holder detection: `bridge_state` heartbeat-freshness gate, built ONLY if burn-in surfaces a deaf mode the upstream slot-release doesn't eliminate. Carried requirements for whenever R5 opens: use the existing portable `stat_mtime` helper (lib-common.sh:977 — `stat -c %Y` is GNU-only and breaks macOS); derive the staleness threshold empirically under a real load spike, not by assertion; deliver its enable flag through the structured observability block (never `.env` tiers); keep detection and heal reading the same signal. (Former R3 — structured observability config — was folded INTO Phase 2 as a requirement by the cycle-1 critical finding.)
