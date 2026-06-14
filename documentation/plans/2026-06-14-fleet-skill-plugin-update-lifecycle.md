---
title: Fleet Update Lifecycle — No-Context-Loss by Default
type: plan
status: draft
owner: alex
tags: [updates, plugins, skills, reload, restart, session-handoff, session-resume, composer, lifecycle, no-context-loss]
created: 2026-06-14
updated: 2026-06-14
---

# Fleet Update Lifecycle — Implementation Plan

> **For agentic workers / reviewers:** This is a `/forge`-style plan. **Decision Forks** are resolved by `[FORK-LOCK F<N>]` PR comments from the designated ratifier before their phases are implemented. Phase steps use `- [ ]` checkboxes. Sizing is **S/M/L per phase — no calendar estimates** (fleet convention). This is **plan-only**; no implementation in this PR. Destined for a `/ironclad` re-scan to converge.

## Revision Log (v3 — this revision)

v2 went through a 4-lens `/ironclad` cycle (first-principles, cost-benefit, extension-check, adversarial). Its foundational finding — *"the goal conflates 'no-restart' with 'no-context-loss'"* — has been **ratified**, and the empirical premise of the v2 core move has been **disproven on the live fleet**. This revision folds both in:

1. **Goal reframed to *no-context-loss*** (the ratifier's answer to the MAJOR premise question). Restarts are acceptable; *losing context* is the pain.
2. **F1 (`--plugin-dir` delivery flip) is DROPPED entirely.** Empirically proven (2026-06-14, live fleet): marketplace-installed plugins **live-reload** — `claude plugin update` refreshes the shared cache and `/reload-plugins` activates it in a running session with no restart. F1 existed only to escape a restart that doesn't exist. Its whole risk cluster (pristine checkout, abort-on-dirty, checkout-currency) is gone.
3. **The lifecycle splits cleanly by update type into TWO mechanisms:**
   - **Mechanism 1 — plugins + composed skills:** a **daily, automatic, live reload** (`claude plugin update` + `claudlobby generate` + a `/reload-plugins`/`/reload-skills` broadcast). **No restart. No context loss.** Applies to **every** running bot (managers included).
   - **Mechanism 2 — the Claude Code binary:** it genuinely **cannot** hot-reload. Download it daily, **kill the daily forced bounce**, and apply it via **natural restarts + a weekly *worker-only* restart** (managers excluded), each made best-achievable-fidelity by **F4** (`session-handoff → session-resume`).
4. **F4 (lossless restart) is the spine of Mechanism 2** — promoted from a v2 sidecar to the primary engineered piece (it was ~80% built and disconnected).
5. **Fork landscape:** F1 **dropped**; F3 **resolved** (daily reload timer + on-demand; weekly restart timer; git hook stays dropped); **F2 reframed** to the daily-broadcast busy-pane policy (re-opened by the daily-automatic cadence); **F4 ratified** as primary; **two new forks** — **F5** (manager-exclusion mechanism) and **F6** (stale-checkpoint resume age gate).
6. **The surviving ironclad blocker is reframed:** the "fail-silently" concern moves off the dropped checkout-abort onto the **download + reload paths** — a failed `claude plugin update` / `generate` / `npm install` must be **loud** (event + manager alert).

A full finding-by-finding disposition is in **§ Ironclad Findings Disposition**.

---

**Goal.** Make fleet updates land without ever costing a bot its working context. **Two update classes, two mechanisms.** Plugins and composed skills **reload live, every day** — no restart, no context loss. The Claude Code binary, which cannot hot-reload, is **downloaded daily but applied by restart** — and we make that restart **rare and lossless**: kill the daily forced bounce, pick the binary up on natural restarts plus a **weekly worker-only** restart (managers stay up), each made best-achievable-fidelity by `session-handoff → session-resume`. The daily-reset pain was the binary bounce destroying context; this removes it **without ever putting a plugin or skill update behind a restart.**

**Architecture (target).**

| Update type | Stays current (download) | Applied to a *running* bot | Restart? | Cadence |
|---|---|---|---|---|
| **Composed skills** (`library/skills/<name>` symlinked by `composer.link_skills`, `composer.py:565-614`) | `claudlobby generate` re-links | **`/reload-skills` broadcast** (live) | **No** | **Daily auto** (+ on-demand) — Mechanism 1 |
| **clauDNA marketplace plugin** | `claude plugin update` → shared host cache `~/.claude/plugins/cache/` (`start-bot.sh:191`) | **`/reload-plugins` broadcast** (live) | **No** | **Daily auto** (+ on-demand) — Mechanism 1 |
| **Claude Code binary** | `update-claude-code.sh` daily `npm install -g @anthropic-ai/claude-code@latest` (`:42/:51`) | new `exec claude` process at next start (`start-bot.sh:156/196`) | **Yes** (binary swap) | **Weekly worker-only** restart (managers excluded) + natural restarts; **lossless via F4** — Mechanism 2 |

**The unifying principle:** *the only update that costs a restart is the binary.* Make that restart **rare** (weekly, workers-only) and **lossless** (F4); let **everything else reload live, daily.** The daily reload hits every running bot (managers included — live reload is free and lossless); only the **binary** restart excludes managers, because a binary swap forces a process restart and a manager's long-horizon orchestration context is the least summarizable.

> **Empirical proof that anchors Mechanism 1 (2026-06-14, live fleet).** `claude plugin update` updated the shared cache; a `/reload-plugins` sent per bot via tmux then activated `claudna:ironclad` in the *running* session with **no restart** — two worker bots went `claudna:ironclad` ABSENT → AVAILABLE after the reload, while a third (control, no reload) stayed on the old version. Marketplace plugins are a live-reload channel; **the old F1 `--plugin-dir` flip bought nothing** and is dropped.

**Tech stack.** Bash `lib/` scripts (`set -euo pipefail`, source `lib-common.sh`), the Python compositor (`claudlobby/composer.py`, `config.py`, `system_defaults.yaml`), tmux session control, systemd-user / launchd supervision, and the clauDNA `session-handoff`/`session-resume` skills (0.6.1).

---

## Verified Current State

Every claim below is verified against the checkout at plan time (`projects/claudlobby`, branch `alex/forge-fleet-update-lifecycle`).

**Plugins + skills already have a live, no-restart update path (Mechanism 1 is mostly wiring, not invention).**
- Plugin cache is **shared host-wide** at `~/.claude/plugins/cache/` (`start-bot.sh:162` — "only the first bot to start actually fetches; others get a no-op"). `claude plugin update` (`start-bot.sh:191`) refreshes that cache; `/reload-plugins` activates it live (empirically verified above).
- **`claude plugin update` runs at launch only today** — the sole invocation in the repo is `start-bot.sh:191`, inside the pre-launch block before `tmux new-session` (`:196`). No timer or cron refreshes the cache while bots run. So today a plugin refresh reaches a running bot only on restart — Mechanism 1 closes exactly that gap with a daily download + live broadcast.
- Composed skills are already a live source: `composer.link_skills` (`composer.py:565-614`) `unlink`s then re-creates each `library/skills/<name>` → `<bot>/.claude/skills/<leaf>` symlink on every generate (`:585-590`). Editing a linked `SKILL.md` updates it in place; a `/reload-skills` re-reads it. **No existing helper broadcasts a command to running bots** — `reload-fleet.sh` is genuinely new (closest skeleton: `update-claude-code.sh:74,81-88`).
- Plugin declarations are emitted twice (both unchanged by this plan): `composer.py:432-446` (`CLAUDE_CODE_SYNC_PLUGIN_INSTALL`, `FLEET_PLUGINS_REQUIRED`, `FLEET_PLUGINS_MARKETPLACES` → `bot.conf`) and `composer.py:1068-1072` (`enabledPlugins`/`extraKnownMarketplaces` → `settings.local.json`). Marketplace stays the delivery channel; F1 is dropped, so neither needs suppression.
- **clauDNA ships zero MCP servers** (verified). So `/reload-plugins`'s "skips MCP-tool changes unless `--force`" caveat does not fire today — a future contingency only (Phase 2).

**The binary updater is a daily *bounce* with no manager/worker distinction (the literal daily-reset pain).**
- `lib/update-claude-code.sh` updates the **binary only** — `npm install -g @anthropic-ai/claude-code@latest` (`:42` sudo-path / `:51` plain). It **never** touches plugins (the only `claude plugin update` in the repo is `start-bot.sh:191`).
- Its **fleet bounce** (`:80-89`) loops `for bot_dir in "$BOTS_DIR"/*/` (`:81`) and restarts **every** bot via `spin-up-bot.sh "$bot_dir"` (`:85`) — **all bots, no manager/worker filter.** It only bounces if the version changed (`:61-64` early-exit otherwise).
- The timer is **daily at 04:00** (`install-claude-update-systemd.sh:44` `OnCalendar=*-*-* 04:00:00`, `:45` `Persistent=true`, `:46` `RandomizedDelaySec=600`). Linux-only; **no launchd equivalent exists**. This is a standalone installer, separate from `fleet_timers`.

**Download/update failures are SILENT today (the surviving blocker's real surface).**
- `start-bot.sh`'s plugin steps are each wrapped `with_timeout 30 … || true` (`:164`); output goes only to `$BOT_DIR/logs/startup.log` (`:166/:187/:190`). A failed fetch → the bot silently launches on a stale cache.
- The reusable loud-failure primitives **already exist**: `emit_script_error <bot_dir> <script> <exit_code> <message>` (`lib-common.sh:429-449`, writes `data/events/fleet-<date>.jsonl`, or fleet-level `state/events/` when `bot_dir=""`); manager nudge via the `notify_manager` pattern (`fleet-pulse.sh:61-67`, reads `MANAGER_TMUX`, `send-keys`); `report-back.sh <bot> failed "<msg>"` (`:66-94`, manager-visible + ledgered); Telegram `tg-post.sh` for critical escalation (`fleet-pulse.sh:288`).

**The lossless-restart machinery (F4) is ~80% built and disconnected.**
- `lib/pre-stop-handoff.sh` sends `/session-handoff --auto` (`:38`), waits ≤30s for `$BOT_DIR/.claude/session.md` (`:25,:40-49`), **skips if the file is <5 min old** (`:28-34`), and **never blocks** (exits 0 on skip/success; timeout falls through to an effective 0). It is **wired into nothing** — every composed unit's `ExecStop` is a bare `tmux kill-session` (`composer.py:521`).
- `start-bot.sh:226-239` sends `STARTUP_PROMPT` with a **two-step send + verify-retry** (text `:231` → `sleep 0.5` → `Enter` `:233` → probe/capture `:235-238` → conditional re-`Enter`), `set +H;`-prefixed. **No bot's `STARTUP_PROMPT` invokes session-resume**, and every live bot sets a **custom** `startup_prompt`, so the composer default (`composer.py:466-469`, the `else` branch) reaches **zero** bots. A resume keystroke must be injected as a **separate first keystroke** (~`start-bot.sh:220`, before the `:226` block), reusing the same two-step-verify pattern, and must settle before `STARTUP_PROMPT` to avoid an input-buffer collision.
- `keepalive.sh` is the highest-frequency restart entrypoint: on session death (`:59`) it calls `systemctl --user restart` (`:70`) / `launchctl kickstart` (`:79`) / `start-bot.sh` (`:83`) **directly**, then `exit 0` (`:85`) — bypassing handoff (the session is already dead → nothing to hand off from). It writes the idle marker `data/.idle` (`touch :161` / `rm :154`).
- `spin-up-bot.sh` is the **cross-platform** idempotent restart primitive (`:29` Linux/systemd, `:53` Darwin/launchd, `:67` fallback), already called by `update-claude-code.sh:85` and `reconcile-fleet.sh:193`. A restart skill should delegate to it rather than re-implement OS detection.
- `library/skills/restart/SKILL.md` is **launchctl-only** (`:4/:13/:26`), assigned only to orchestration-style bots, and **assumes both unwired hooks** (ExecStop→handoff and STARTUP_PROMPT→resume) — both false today.
- **`/session-resume --auto` has NO checkpoint-age gate** (clauDNA `session-resume/SKILL.md:24-33`): it reads `session.md` unconditionally and, under `--auto`, proceeds on the briefing (`:102`) — surfacing the stored Next-Step as current. The reaper (`_shared/reaper-rules.md`) prunes by per-item TTL but **exempts `Next Steps` from TTL drop** (`:28`) — so a finished-but-uncommitted task survives and gets replayed. `pre-stop-handoff.sh:28-34` (the 5-min skip) is the **only** age-gate anywhere, and it's on the *write* side. An explicit age gate on *resume* is net-new (F6).

**Manager vs worker is already a readable signal — no new config required.**
- `compose_bot_conf` emits `MANAGER_TMUX` into every `bot.conf` (`composer.py:455-460`): for a **manager** it equals the bot's own id (with the literal comment `# this bot is a manager`); for a **worker** it points at a different bot. `BOT_ID` is also emitted (`composer.py:300`). So a script tests **manager ⇔ `MANAGER_TMUX == BOT_ID`**. The canonical set is `FleetConfig.manager_bots() = {team.manager …}` (`config.py:291-293`); `bot_conf_get` (`lib-common.sh:468`) + the read at `fleet-pulse.sh:63` establish the access pattern. **No expertise/role var reaches `bot.conf`** — expertise is consumed only for CLAUDE.md prose and permissions.

**Timers compose from a declarative table.** `fleet_timers` entries (`system_defaults.yaml:17-33`) take `script:` (+ `$CLAUDLOBBY_ROOT`), one scheduling field (`interval:` / `interval_from:` / `schedule:` OnCalendar), and `type:` (default `oneshot`). `compose_fleet_timers` (`composer.py:1516-1562`) → `_write_timer_units` (`:1389-1459+`) emits a `.service` (`ExecStart=<script> <fleet.name>`, `:1416`), a `.timer` (calendar branch `OnCalendar=<expr>`, `:1448`), and a launchd `.plist`. `creds-check` (`:30-33`, `schedule: "*-*-* 06:00:00"`) is the calendar precedent. **Both new timers (daily reload, weekly worker-restart) drop straight into this table.**

---

## Decision Forks

### F1 — clauDNA `--plugin-dir` delivery flip · **DROPPED**
- **Was:** flip the fleet's clauDNA from marketplace-install to a fleet-global `--plugin-dir` local checkout for live reload.
- **Why dropped:** the fork assumed marketplace plugins were restart-bound. **Empirically false** (proof above) — marketplace plugins live-reload via `claude plugin update` + `/reload-plugins`. The flip bought nothing the marketplace path already gives, at the cost of a pristine fleet-checkout, an abort-on-dirty invariant, checkout-currency management, and new `composer.py`/`start-bot.sh` wiring. **All removed.** Dropping F1 also **moots** ironclad Blocker 1's checkout-abort surface, Risk 8 (checkout identity), and the `pull --ff-only` / read-only-enforcement questions.
- **Disposition:** out of scope. Marketplace remains the unchanged delivery channel.

### F2 — Daily reload-broadcast busy-pane policy · *Status: open, leaning (b) — framework*
- **Context (reframed, re-opened by the daily-automatic cadence):** Mechanism 1's broadcast fires **daily into a live fleet**. A bot mid-task must not be interrupted — `Enter` into a busy pane can submit ghost autocompletion text (`keepalive.sh:5-9`). With the broadcast now automatic and recurring, busy-pane safety + eventual convergence are load-bearing (this is the home of ironclad Risks 5 & 7).
- **Options:**
  - **(a) broadcaster idle-checks + defer marker** — `reload-fleet.sh` classifies each running bot via live `pane_is_idle` (`lib-common.sh:274`, re-checked right before the Enter), dispatches `/reload-*` to idle bots now via `dispatch.sh`, and drops `data/.reload-pending` for busy bots; `keepalive.sh` fires the deferred reload at the bot's next idle tick. *Trade-off:* a residual idle-check→Enter TOCTOU window (small, low-harm) remains in the broadcaster.
  - **(b) keepalive-consolidated activation (the adversarial lens's counter-plan)** — the daily job does **download + generate only**, then drops `data/.reload-pending` on **every** running bot; **`keepalive.sh` performs *all* `/reload-*` at each bot's next idle tick** and clears the marker. Single, idle-gated-by-construction activation path → **no broadcaster-side TOCTOU, no ghost-text** (directly closes Risks 5 & 7). *Trade-off:* expands `keepalive`'s role to send a reload keystroke when idle (today it is restart-only and never presses Enter), and a reload lags up to one keepalive tick (~60s — negligible for a daily reload).
- **Lean: (b).** The daily automatic cadence makes the TOCTOU/ghost-text elimination worth consolidating activation into the already-idle-aware `keepalive`; the timer's only job becomes download → generate → mark. (Option (a) still needs `keepalive` to send in the deferred case, so (b)'s single-path is the smaller net surface and the safer one.)
- **Concurrency invariant (ironclad Risk 5):** the download+generate step runs under `with_lock` (`lib-common.sh:108-124`) so the daily timer and an on-demand run can never relink `.claude/skills/` symlinks concurrently. Generate must **complete** before any marker is dropped (a reload mid-relink could read a missing symlink — `composer.py:585-590`).
- **Ratifier:** Framework (engineering safety judgment via `/weigh-development-paths`).
- **Status:** open.
- **Evidence:** `dispatch.sh:26-28`, `lib-common.sh:108-124,:274-295`, `keepalive.sh:148-162`, `fleet-pulse.sh:95-111`, `composer.py:585-590`.

### F3 — Trigger model · **RESOLVED**
- **Was:** what fires the reload broadcaster — on-demand / git hook / timer.
- **Resolution:** **a daily timer (primary) + on-demand (the same `reload-fleet.sh`, invocable to push a release immediately).** The reload path is now Mechanism 1's automatic daily cadence — a `fleet_timers` entry (Phase 2). The **weekly worker-restart** is a *separate* timer for Mechanism 2 (Phase 3) — different mechanism, different intent. The **git-hook option stays dropped** (YAGNI; would be the repo's first git hook, fires unpredictably, lives in untracked `.git/`).
- **Disposition:** closed.

### F4 — Lossless restart (handoff → resume) · **RATIFIED — the primary piece of Mechanism 2**
- **Context:** the binary cannot hot-reload, so its pickup is a restart. Every restart that applies it (the weekly worker bounce, a keepalive crash-restart, an operator restart) must cost as little context as possible. The machinery is ~80% built and disconnected (Current State).
- **Ratified shape (by dispatch):**
  - **Resume-universal** — `start-bot.sh` injects `/claudna:session-resume --auto` as a separate first keystroke **before** `STARTUP_PROMPT` (~`:220`, reusing the two-step-send+verify pattern at `:231-238`), gated by F6's age policy. Fires on **every** start — intentional, crash, and weekly — because all bots set custom `startup_prompt` (the `else`-default `composer.py:466-469` reaches none).
  - **Handoff-before-intentional-stop** — the intentional-restart entrypoints (the cross-platform restart skill + the weekly-worker-restart script) call `pre-stop-handoff.sh` **before** the restart, with a short (~10s) **non-blocking** timeout (it already exits 0 on timeout). Crash-restarts (`keepalive.sh:70`) get **resume-from-last-checkpoint** — no fresh handoff is possible (session already dead).
  - **Consolidate the restart path** — generalize `library/skills/restart/SKILL.md` (launchctl-only today) to delegate to the cross-platform `spin-up-bot.sh` (**adopts ironclad Risk 9**); make it the single intentional-restart entrypoint and **retire `update-claude-code.sh`'s raw bounce** in favor of it (no shim).
  - **Honest fidelity (ironclad Risk 3)** — `session.md` is a best-achievable **summary**, not the live conversation. The guarantee is **"best-achievable-fidelity, not zero-loss."** This is *exactly* why **managers are excluded from the weekly binary bounce** (F5) — their context is least summarizable, so we don't auto-restart them at all; they update the binary on a deliberate (still lossless) human restart.
  - **Optional hardening** — wiring `ExecStop → pre-stop-handoff.sh` (`composer.py:521`) would also cover ad-hoc operator `systemctl restart`; deferred as defense-in-depth pending the `ExecStop` ordering check flagged in v2. The explicit-call path is the primary, testable guarantee.
- **Ratifier:** Human (Chris) — ratified as primary via dispatch. Sub-options (ExecStop hardening) are framework judgment.
- **Status:** ratified (direction); sub-options open.
- **Evidence:** `start-bot.sh:226-239`, `pre-stop-handoff.sh:25-50`, `keepalive.sh:70-85`, `spin-up-bot.sh:29/:53/:67`, `library/skills/restart/SKILL.md`, `composer.py:463-469/:521`.

### F5 — Manager-exclusion mechanism for the weekly *binary* restart · *Status: open, leaning (a)*
- **Context:** the weekly bounce (Phase 3) restarts **workers only**; managers stay up to preserve their (least-summarizable) orchestration context. The script needs a signal to skip managers. (Note: this exclusion applies **only** to the weekly binary restart — managers still get Mechanism 1's daily live plugin/skill reload.)
- **Options:**
  - **(a) reuse `MANAGER_TMUX == BOT_ID`** — already emitted for every bot (`composer.py:455-460`); the restart loop reads it via `bot_conf_get` (the `fleet-pulse.sh:63` pattern) and skips managers. **Zero new config, zero composer change.** *Trade-off:* couples "excluded from auto-restart" to "is a team manager" — the same set today.
  - **(b) new per-bot `fleet.yaml` boolean** (e.g. `auto_restart: false`) — new `BotConfig` field + new `bot.conf` export; decouples exclusion from manager-ness. *Trade-off:* new config surface for a distinction nothing needs yet (YAGNI).
- **Lean: (a)** — most-consistent-with-codebase (reuses an emitted, already-consumed signal) and most-elegant (one guard line). Promote to (b) only if a real need to decouple appears.
- **Policy note:** the retired daily bounce restarted **all** bots including managers; after this plan, **managers are never auto-restarted** — they pick up a new binary on a deliberate human restart (or any natural restart). Intentional asymmetry; documented in Phase 4. Optional observability add: surface each bot's Claude Code + clauDNA version in `claudlobby status` so manager binary-drift is visible.
- **Ratifier:** Human (Chris) — fleet-policy semantic ("does *manager* always mean *never auto-restart*?").
- **Status:** open.
- **Evidence:** `composer.py:455-460`, `config.py:291-293`, `lib-common.sh:468`, `fleet-pulse.sh:63`, `update-claude-code.sh:80-89`.

### F6 — Stale-checkpoint resume policy (the age gate) · *Status: open, leaning (c)*
- **Context:** `/claudna:session-resume --auto` has **no age gate** (verified) and the reaper exempts `Next Steps` from TTL (`reaper-rules.md:28`). On a days-old `session.md`, `--auto` resume **replays dead state** — re-attempting a finished task or acting on an already-merged PR (ironclad Risk 4, worse than a clean start). F4 makes resume fire on *every* start (including crash-restarts off a possibly-stale checkpoint), so this gate is load-bearing.
- **Options (what `start-bot.sh` does when `session.md` mtime is older than threshold `T`):**
  - **(a) resume anyway, flag staleness** — inject resume but prepend a loud "checkpoint is N old — verify before acting" note.
  - **(b) skip resume, clean-start** — don't inject resume; post the ready message and await dispatch (a clean start beats replaying dead state).
  - **(c) threshold split** — resume normally when fresh (`mtime < T`); skip-and-clean-start when stale (`mtime ≥ T`). One conservative knob, default `T ≈ 24h`.
- **Lean: (c), `T ≈ 24h`.** Fresh checkpoints (the weekly bounce hands off seconds earlier; an operator restart hands off live) resume at full fidelity; genuinely stale ones (a bot crashed days ago) clean-start instead of acting on dead state. The gate lives **fleet-side in `start-bot.sh`** (a cheap `stat_mtime` check, `lib-common.sh:368`) — keeps the policy in fleet control; an upstream age-gate in the clauDNA skill can follow if broadly useful.
- **Sub-question (proactive checkpointing):** to keep crash-restart checkpoints fresh, should a bot periodically refresh `session.md` (e.g. on `/compact`, or a low-frequency handoff)? **Lean: defer (YAGNI)** — rely on intentional-restart handoffs + WIP-commit discipline + this gate; add proactive checkpointing only if crash-restart loss proves real.
- **Ratifier:** Human (Chris) — sets the freshness bar.
- **Status:** open.
- **Evidence:** clauDNA `session-resume/SKILL.md:24-33,:102`, `_shared/reaper-rules.md:26-28`, `pre-stop-handoff.sh:28-34`, `lib-common.sh:368`.

---

## File Structure

```
documentation/plans/2026-06-14-fleet-skill-plugin-update-lifecycle.md   # this plan
documentation/fleet-update-lifecycle.md                                 # NEW — codified lifecycle doc (Phase 4)
lib/reload-fleet.sh                                                      # NEW — Mechanism 1: plugin update + generate + reload broadcast (Phase 2)
lib/install-reload-fleet-systemd.sh                                      # NEW — enroll the daily reload timer (Phase 2)
lib/weekly-worker-restart.sh                                            # NEW — Mechanism 2: weekly worker-only lossless restart (Phase 3)
lib/install-weekly-worker-restart-systemd.sh                            # NEW — enroll the weekly restart timer (Phase 3)
lib/start-bot.sh                                                         # EDIT — inject /session-resume before STARTUP_PROMPT, age-gated (Phase 1)
lib/pre-stop-handoff.sh                                                  # EDIT — explicit exit 0 on timeout (Phase 1)
lib/update-claude-code.sh                                               # EDIT — drop the daily bounce; keep daily binary download; loud on failure (Phase 3)
lib/keepalive.sh                                                         # EDIT (if F2(b)) — consume .reload-pending at idle (Phase 2)
library/skills/restart/SKILL.md                                          # EDIT — cross-platform via spin-up-bot.sh; single intentional-restart entrypoint (Phase 1)
library/protocols/worker-lifecycle.md                                   # EDIT — best-achievable-fidelity framing (Phase 1)
claudlobby/system_defaults.yaml                                          # EDIT — daily reload timer + weekly worker-restart timer entries (Phases 2, 3)
claudlobby/composer.py                                                   # (no change — compose_fleet_timers already handles new fleet_timers entries)
CLAUDE.md, documentation/fleet-yaml-schema.md                            # EDIT — lib/ table + lifecycle knobs (test_script_consumers.sh) (Phases 2-4)
lib/validate-bot-change.sh                                              # EDIT — extend assertions per phase
```

## Phases

### Phase 0 — Ratify · **Gate** · **S**
- [ ] Record the ratified premise in-thread: **goal = no-context-loss** (answers the ironclad MAJOR question).
- [ ] Lock **F5** (manager-exclusion) and **F6** (stale-resume policy) — Chris. Lock **F2** (busy-pane policy) — framework. **F4** is ratified (primary); **F1** dropped; **F3** resolved.
- [ ] No weekly-restart enablement (Phase 3) until **F5 + Phase 1** land; no resume-on-every-start until **F6** sets the age bar.

### Phase 1 — Lossless restart (F4 — spine of Mechanism 2) · **M** · *gated by F4 (ratified) + F6*
- **Files:** `lib/start-bot.sh`, `lib/pre-stop-handoff.sh`, `library/skills/restart/SKILL.md`, `library/protocols/worker-lifecycle.md`, `claudlobby/composer.py` (optional ExecStop), `lib/validate-bot-change.sh`
- [ ] **Resume-universal:** inject `/claudna:session-resume --auto` as a first keystroke before `STARTUP_PROMPT` (`start-bot.sh:~220`), mirroring the two-step-send+verify at `:231-238`; ensure it settles before `STARTUP_PROMPT` (input-buffer collision).
- [ ] **Age gate (F6):** before injecting resume, `stat_mtime` `session.md` and apply the ratified policy (lean: skip+clean-start when `mtime ≥ T≈24h`, else resume).
- [ ] **Handoff-before-intentional-stop:** the restart skill + the weekly-restart script call `pre-stop-handoff.sh` before the restart, ~10s non-blocking; add an explicit `exit 0` on its timeout path (`:50`); restart proceeds regardless of handoff outcome.
- [ ] **Consolidate the restart path:** generalize `library/skills/restart/SKILL.md` to delegate to `spin-up-bot.sh` (cross-platform; retire the launchctl hardcode); make it the single intentional-restart entrypoint.
- [ ] Reframe `worker-lifecycle.md`'s "a restart will never lose your progress" to the honest best-achievable-fidelity `session.md` continuity (convention-over-event: describe the semantic, not the dated change).
- [ ] **(Optional hardening)** evaluate wiring `ExecStop → pre-stop-handoff.sh` (`composer.py:521`) for ad-hoc restarts.
- [ ] **Validate:** *(harness)* new pane shows `/session-resume` sent first; with a stale `session.md`, injection is skipped/flagged per F6; `session.md` written pre-stop on an intentional restart. *(live auth)* the resume briefing actually loads into context. Cite both.

### Phase 2 — Mechanism 1: daily live plugin + skill reload · **M** · *gated by F2*
- **Files:** `lib/reload-fleet.sh` (new), `lib/install-reload-fleet-systemd.sh` (new), `claudlobby/system_defaults.yaml` (`fleet_timers`), `lib/keepalive.sh` (if F2(b)), CLAUDE.md `lib/` table + lifecycle doc, `lib/validate-bot-change.sh`
- [ ] `lib/reload-fleet.sh <fleet>`: source `lib-common.sh`; **under `with_lock`** (`lib-common.sh:108-124`) run `claude plugin update` for each `FLEET_PLUGINS_REQUIRED` (refresh the shared cache) then `claudlobby --fleet "$fleet" generate` to **completion** — **abort loud** on any non-zero (`emit_script_error` + manager notify; mirrors the binary-download failure path). Then activate per F2: **(b lean)** drop `data/.reload-pending` on every running bot and let `keepalive` fire `/reload-plugins`+`/reload-skills` at each bot's next idle tick and clear the marker; **(a)** dispatch to idle bots now via `dispatch.sh` and defer busy via the marker.
- [ ] Daily trigger: add a `reload-fleet` entry to `fleet_timers` (`system_defaults.yaml:17-33`) — `schedule: "*-*-* 03:30:00"` (daily, before the 04:00 binary download so the cache is warm; tunable), `type: oneshot`; `compose_fleet_timers` generates the unit (`composer.py:1516-1562`). Add a thin `install-reload-fleet-systemd.sh` cloning `install-fleet-pulse-systemd.sh`. `reload-fleet.sh` is also invocable **on-demand** (push a release immediately).
- [ ] If F2(b): extend `keepalive.sh` — on IDLE classification, if `data/.reload-pending` exists, dispatch `/reload-plugins`+`/reload-skills` and clear the marker. (If F2(a): the broadcaster sends to idle directly; `keepalive` only consumes the deferred marker.)
- [ ] Reference the new script(s) in CLAUDE.md's `lib/` table + the lifecycle doc (else `test_script_consumers.sh` fails).
- [ ] **MCP-skip contingency (ironclad Risk 6):** clauDNA ships no MCP servers today. Contingency only — *if* it later adds one, `reload-fleet.sh` greps the pulled `plugin.json` for `mcpServers` and, if present, warns + routes that bot to a Phase-3 restart instead (a live `/reload-plugins` would silently skip the MCP change). Not built until that exists.
- [ ] **Removal/rename semantics (ironclad gap):** live reload covers add/modify; a skill/plugin **removal or rename** routes to the next restart (a clean reload at start), not a live broadcast — document it.
- [ ] **Validate:** *(harness — what it CAN prove)* extend `validate-bot-change.sh`: an idle test bot receives the reload (keystroke or marker→keepalive); a busy bot is not interrupted (no ghost text); `generate` completes before any reload; a simulated `claude plugin update`/`generate` failure emits the event + notifies the manager. *(manual, live auth)* the plugin/skill actually reloaded in-session. State the split.

### Phase 3 — Mechanism 2: retire the daily binary bounce; weekly worker-only restart · **M** · *3b gated by Phase 1 + F5*
- **Files:** `lib/update-claude-code.sh` (edit), `lib/weekly-worker-restart.sh` (new), `lib/install-weekly-worker-restart-systemd.sh` (new), `claudlobby/system_defaults.yaml`, CLAUDE.md `lib/` table + lifecycle doc, `lib/validate-bot-change.sh`
- [ ] **3a (S, independent) — downloader only + loud failure:** remove the bounce loop (`update-claude-code.sh:80-89`); keep the daily binary `npm install` (`:42/:51`). On a non-zero install, `emit_script_error "$bot_dir" update-claude-code.sh <code> "<msg>"` **and** notify the manager (`report-back.sh … failed` / the `notify_manager` pattern); reserve `tg-post.sh` for total failure. (Script is now download-only — keep the name to avoid timer-installer churn; document the role shift.)
- [ ] **3b (M, gated by Phase 1 + F5) — weekly worker-only lossless restart:** `lib/weekly-worker-restart.sh <fleet>`: skeleton from `update-claude-code.sh:74,81-88` (`resolve_bots_dir` + `for bot_dir` loop); **skip managers** per F5 (`MANAGER_TMUX == BOT_ID`); for each **worker**, run the lossless intentional restart (`pre-stop-handoff.sh` → `spin-up-bot.sh` → resume-on-start from Phase 1). Loud on a worker that fails to come back.
- [ ] Add a `weekly-worker-restart` entry to `fleet_timers` — `schedule: "Sun *-*-* 05:00:00"` (weekly, after a daily download; tunable), `type: oneshot`; `compose_fleet_timers` generates the unit (invoked `<script> <fleet>`, `composer.py:1416`). Add a thin `install-weekly-worker-restart-systemd.sh` cloning `install-fleet-pulse-systemd.sh`.
- [ ] Reference both new scripts in CLAUDE.md's `lib/` table + lifecycle doc.
- [ ] **Validate:** *(harness)* assert a **worker** bot was restarted and a **manager** bot (`MANAGER_TMUX==BOT_ID`) was **skipped**; the bounce loop is gone from `update-claude-code.sh`; a simulated download failure emits the event + notifies. *(live)* a worker picks up a staged binary after the weekly restart with its context intact (resume briefing present). Cite both.

### Phase 4 — Codify the lifecycle · **S** · *depends on 1–3*
- **Files:** `documentation/fleet-update-lifecycle.md` (new), CLAUDE.md, `documentation/fleet-yaml-schema.md`
- [ ] Write the canonical "Fleet Update Lifecycle" doc, north star = **no context loss**: the **two mechanisms** (plugins/skills = daily live reload, no restart; binary = download daily, apply via rare lossless restart); **why managers get the daily reload but not the weekly binary restart**; **resume-on-every-start + the age gate**; the **loud-failure** contract; the **on-demand** reload escape hatch. Reconcile with PR #399 (its daily bounce is **replaced** by the weekly lossless restart; its daily binary **download survives**). One documented lifecycle, not folk practice.

---

## Companion Plans
- **PR #399** (`feat(fleet): Claude Code update script…`, merged) — added `lib/update-claude-code.sh`. This plan **retires its daily bounce** (Phase 3a) and **keeps its daily binary download**; Phase 1 makes the residual restart lossless; Phase 4 reconciles. Must not contradict.
- **clauDNA `session-handoff`/`session-resume` skills** (Claudfather/clauDNA) — the F4 endpoints; F6's age gate may be upstreamed to `session-resume` later.
- **clauDNA marketplace release procedure** (`shared/knowledge/clauDNA/release-procedure.md`) — unchanged; marketplace stays the delivery channel (F1 dropped → no divergence).
- **Deterministic idle-detection** (`shared/planning/active/deterministic-idle-detection.md`) — source of the `.idle`/`pane_is_idle` primitives F2 reuses.

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| `/reload-plugins` + `/reload-skills` live-reload (empirically verified 2026-06-14) | Phase 2 | Low — proven on the live fleet |
| clauDNA `session-handoff`/`session-resume` `--auto` maturity | Phase 1 | Low — present, contract aligned |
| `MANAGER_TMUX == BOT_ID` reliably marks managers | Phase 3b (F5) | Low — emitted for every bot (`composer.py:455-460`) |
| `with_lock` serializes daily-timer vs on-demand `generate` | Phase 2 | Low — `lib-common.sh:108-124` |
| Phase 1 lossless path before Phase 3b weekly bounce | Phase 3b | Coordination — sequencing gate |
| F5 + F6 locked; F2 locked | Phases 1, 2, 3b | Coordination — Phase 0 gate |

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Stale crash-resume replays dead state (`--auto`, no age gate) | Medium | **F6** age gate (lean skip-when-stale ≥24h) + best-achievable-fidelity framing. |
| "Lossless" over-claims a lossy `session.md` summary | Medium (trust) | Relabel **best-achievable-fidelity**; managers excluded from the weekly bounce precisely because their context is least summarizable (F4/F5). |
| Worker runs ≤1 week on a stale binary before the weekly bounce | Low | Acceptable by design; any natural restart picks it up sooner; the binary isn't urgent. |
| Manager never auto-restarts → binary/clauDNA drift | Low–Medium | Intentional (managers are long-lived); human does a deliberate lossless restart; optional `claudlobby status` version surfacing makes drift visible. |
| Download / `generate` / reload failure is silent (today) | Medium | **Loud-failure contract:** `emit_script_error` + manager notify on any non-zero (Phases 2, 3a) — the reframed surviving blocker. |
| Daily reload `Enter` into a busy pane → ghost text | Medium | **F2(b)** idle-gated activation via keepalive (no broadcaster TOCTOU); or F2(a) re-check idle right before send. |
| Concurrent `generate` (daily timer + on-demand) relinks symlinks mid-read | Low | `with_lock` around download+generate; generate completes before any reload (Phase 2). |
| Skill/plugin removal or rename can't live-reload cleanly | Low | Removals/renames route to the next restart (clean reload at start) — documented (Phase 2). |
| clauDNA later ships an MCP server → `/reload-plugins` silently skips it | Low (future) | Contingency: `reload-fleet.sh` greps `plugin.json` for `mcpServers`, warns + routes to restart (Phase 2). |
| Resume keystroke collides with `STARTUP_PROMPT` | Low | Resume sent first with two-step-verify; settles before `STARTUP_PROMPT` (Phase 1). |

## Validation Strategy

Per the **MANDATORY** bot-behavior validation loop (CLAUDE.md): unit tests prove *composition*; only running proves *behavior*. **Critical scoping:** `lib/validate-bot-change.sh` runs **without Claude auth** — it can assert emitted events, marker files, dispatched keystrokes, skip/restart decisions, and composed outputs, but it **cannot** observe that a plugin actually reloaded or a resume briefing landed in context. Those require a **manual live-bot observation with real auth.** The PR must cite both.

| Criterion | Harness-provable (`validate-bot-change.sh` / `pytest`) | Requires manual live observation (real auth) |
|---|---|---|
| Daily reload reaches a running bot | ✅ idle bot got the reload (keystroke or marker→keepalive); busy bot uninterrupted; `generate`-before-reload ordering | ✅ the plugin/skill actually reloaded in-session |
| Download/generate failure is loud | ✅ simulated non-zero → `emit_script_error` event present + manager notified | — |
| Resume on every start | ✅ new pane shows `/session-resume` sent first | ✅ resume briefing present in context |
| Stale-checkpoint gate (F6) | ✅ old `session.md` → injection skipped/flagged per policy | — |
| Intentional restart hands off | ✅ `session.md` written pre-stop | ✅ briefing fidelity on the new session |
| Weekly bounce excludes managers (F5) | ✅ worker restarted, manager (`MANAGER_TMUX==BOT_ID`) skipped | — |
| Daily bounce removed | ✅ `update-claude-code.sh` no longer contains the bounce loop; daily download intact | — |
| No composition regression | ✅ `pytest` green; `claudlobby validate`/`generate` clean; new timers compose | — |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 0. Ratify | S | — | — |
| 1. Lossless restart (F4) | M | F4 (ratified), F6 | 2, 3a |
| 2. Daily live plugin+skill reload (Mechanism 1) | M | F2 | 1, 3 |
| 3a. Drop daily bounce + loud downloader | S | — | 1, 2 |
| 3b. Weekly worker-only restart | M | Phase 1, F5 | 2 |
| 4. Codify lifecycle | S | 1–3 | — |

**Critical path:** `0 → 1 → 3b`; `2` and `3a` run in parallel off `0`; `4` last. Mechanism 1 (Phase 2) is fully independent of the restart work — it can ship first and delivers the daily no-restart win on its own.

## Ironclad Findings Disposition (cycle 1 → this revision)

| Ironclad cycle-1 item | Disposition |
|---|---|
| **Blocker 1** — F1 abort-on-dirty fails silently | **Mooted** (F1 dropped — no checkout) **+ reframed**: download/`generate`/reload failures must be **loud** (`emit_script_error` + manager notify) — Phases 2, 3a. |
| **FP Risk 1** — goal conflates no-restart vs no-context-loss | **Resolved** — goal = no-context-loss; the two concerns are **split by update type** (plugins/skills = live no-restart; binary = lossless restart), each served by the right mechanism. |
| **FP Risk 2** — daily resets come from the binary bounce | **Resolved** — daily binary bounce **retired** (3a); weekly worker-only lossless restart (3b); plugins/skills never restart. |
| **FP Risk 3** — "lossless" over-labels a lossy summary | **Resolved** — relabeled best-achievable-fidelity; managers excluded from the weekly bounce (F4/F5). |
| **Adv Risk 4** — stale crash-resume replays dead state | **Open fork F6** (age gate), lean skip-when-stale. |
| **Adv Risk 5** — no lock across reload paths | **Folded** — daily timer + on-demand `generate` run under `with_lock` (Phase 2 / F2 invariant). |
| **Adv Risk 6** — silent MCP-skip | **Contingency** — clauDNA ships none; helper greps `plugin.json` if that changes (Phase 2). |
| **Adv Risk 7** — busy-pane TOCTOU undersold | **Addressed via F2** — lean (b) idle-gated keepalive activation eliminates the broadcaster TOCTOU/ghost-text. |
| **CB Risk 8** — dedicated-checkout identity | **Mooted** (F1 dropped — no checkout). |
| **EC Risk 9** — restart skill should call `spin-up-bot.sh` | **Adopted** — F4/Phase 1 delegates to `spin-up-bot.sh`. |
| **CB Risk 10** — full-recompose latency at 50+ bots | **Noted** — daily reload runs one `generate`; scale-conscious, not hot-path; revisit as the fleet grows. |
| **Gap** — timer interval unspecified | **Resolved** — daily reload (`~03:30`) + weekly worker restart (`Sun 05:00`); both tunable. |
| **Gap** — `settings.local.json` double-declaration | **Mooted** (no `--plugin-dir`; marketplace declarations unchanged). |
| **Gap** — resume-injection Enter semantics | **Resolved** — two-step-send+verify; resume settles before `STARTUP_PROMPT` (Phase 1). |
| **Gap** — skill removal/rename live reload | **Addressed** — removals/renames route to the next restart (Phase 2 note). |
| **Gap** — `.reload-pending` payload / hygiene | **Addressed** — F2(b) marker is a simple "reload due" flag consumed + cleared by keepalive at idle. |
| **Gap** — `reload-fleet.sh` generate-failure handling | **Resolved** — loud abort on non-zero `generate` (Phase 2). |
| **Q** — never-restart vs never-lose-context | **Answered: never lose context.** |
| **Q** — update frequency by type | **Characterized** — binary releases land frequently upstream (downloaded daily, applied weekly/at natural restarts); clauDNA releases are sporadic/per-merge; composed-skill edits are on-demand. Plugins/skills now reload **daily live**; the weekly bounce bounds worker *binary* staleness to ≤1 week; the on-demand reload covers urgent skill/plugin pushes. |
| **Q** — busy-bot max-staleness | **Bounded** — a busy bot converges at its next idle tick (F2 marker); no full-day wait. |
| **Q** — crash-restart checkpoint freshness | **F6 sub-question** — age gate + (deferred) proactive checkpoint. |
| **Q** — `pull --ff-only` force-push / pristine read-only enforcement | **Mooted** (F1 dropped). |
| **Q** — resume vs `STARTUP_PROMPT` precedence | **Resolved** — resume first, then `STARTUP_PROMPT` (Phase 1). |

## Adversarial Review Findings

The v2 author-adversarial pass and the cycle-1 `/ironclad` pass are both dispositioned in the table above. New residual risks surfaced by **this** revision (`[ ]` = open for the ratifier / re-scan):

- [ ] **Managers accrue binary/clauDNA drift.** Excluding managers from the weekly bounce (the intended design) means a manager can run weeks on an old binary. Mitigation: deliberate human lossless restart on demand + optional version surfacing in `claudlobby status`. Acceptable, but confirm the human is comfortable owning manager-update timing.
- [ ] **F6 skip-on-stale can drop a genuinely mid-task crashed worker's context.** If a worker crashes mid-task and isn't restarted for >T, the clean-start abandons that task's `session.md`. Mitigation: WIP-commit discipline; a clean start is still safer than replaying dead state. The proactive-checkpoint sub-question (F6) is the lever if this proves painful.
- [ ] **F2 expands `keepalive`'s role (lean b).** `keepalive` gains a reload-keystroke path (today restart-only, never presses Enter). Low risk (idle-gated, marker-driven), but it widens the highest-frequency watchdog's surface — call it out for the reviewer.
- [x] **Daily reload + the 04:00 binary download could race the cache.** Resolved by ordering: reload at `~03:30` (warm the cache + activate), binary download at `04:00`; `with_lock` guards `generate`. No shared mutable state between the plugin-cache refresh and the npm binary install.
