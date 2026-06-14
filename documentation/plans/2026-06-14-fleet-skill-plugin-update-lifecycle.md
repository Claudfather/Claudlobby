---
title: Fleet Skill/Plugin Update Lifecycle — No-Restart-by-Default
type: plan
status: draft
owner: alex
tags: [plugins, skills, reload, restart, composer, start-bot, session-handoff, lifecycle]
created: 2026-06-14
updated: 2026-06-14
---

# Fleet Skill/Plugin Update Lifecycle — Implementation Plan

> **For agentic workers / reviewers:** This is a `/forge`-style plan. The **Decision Forks** are **OPEN** — each is resolved by a `[FORK-LOCK F<N>]` PR comment from the designated ratifier before its phases are implemented. Phase steps use `- [ ]` checkboxes. Sizing is **S/M/L per phase — no calendar estimates** (fleet convention). This is **plan-only**; no implementation in this PR. Destined for `/ironclad` multi-lens review. An author-side adversarial pass has already run (see Adversarial Review Findings); its blockers are folded into v2 of this draft.

**Goal.** Codify how the fleet updates skills and plugins so updates are **no-restart by default** — Chris does not want daily context resets. Today every clauDNA update is restart-bound (marketplace `claude plugin update` only fires inside `start-bot.sh` at launch), and the one restart path that exists (`update-claude-code.sh`) destroys context. This plan makes skill/plugin updates land **live** on running bots, and makes residual unavoidable restarts **lossless** where a handoff is possible.

**Architecture (target).** Three delivery models, each with an explicit update path:

| Delivery model | Where it lives today | Update path (target) | Restart? |
|---|---|---|---|
| **Composed skills** | `library/skills/<name>` → symlinked into `<bot>/.claude/skills/` by `composer.link_skills` (`composer.py:565-614`) | `claudlobby generate` (re-link) + broadcast `/reload-skills` | **No** |
| **Local `--plugin-dir` plugin** (proposed for clauDNA, fleet-global) | a pristine local clauDNA git checkout the fleet points at | `git pull --ff-only` + broadcast `/reload-plugins` | **No** |
| **Marketplace-install plugin** | `claude plugin install/update` in `start-bot.sh:157-193` | `claude plugin update` at next launch | **Yes** (Claude Code: "restart required to apply") |

The design move: migrate the fleet's clauDNA from **marketplace-install** (restart-bound) to a **`--plugin-dir` local checkout** (live-reload), **fleet-global** (every bot on the fleet uses the same delivery), while the **marketplace stays the external distribution + release channel** (unchanged for the outside world). A `lib/` broadcaster runs `claudlobby generate` then fans `/reload-skills` (+ `/reload-plugins`) to every idle running bot. For restarts that genuinely can't be avoided (a Claude Code binary update), a seamless `session-handoff → restart → session-resume` path makes **intentional** restarts lossless; crash-restarts are best-effort (resume from the last checkpoint).

**Tech stack.** Bash `lib/` scripts (`set -euo pipefail`, source `lib-common.sh`), the Python compositor (`claudlobby/composer.py`, `config.py`), tmux session control, systemd-user / launchd supervision, and the clauDNA `session-handoff`/`session-resume` skills (0.6.0).

---

## Verified Current State

Every claim below is verified against the live tree (`/home/crog/claudlobby`) at plan time.

**Plugin loading is 100% marketplace, restart-bound.**
- `composer.py:418-432` emits `CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1`, `FLEET_PLUGINS_REQUIRED=claudna@Claudfather`, `FLEET_PLUGINS_MARKETPLACES=Claudfather=github:Claudfather/clauDNA` into `bot.conf`; `composer.py:1059-1063` also writes `enabledPlugins`/`extraKnownMarketplaces` into `settings.local.json` (two parallel declarations of the same end state).
- `start-bot.sh:157-193` runs `claude plugin marketplace add` → `claude plugin install` (cold) / `claude plugin update` per `FLEET_PLUGINS_REQUIRED`, **then** `exec claude $CLAUDE_FLAGS --name <session>` (`start-bot.sh:150-155`). The `plugin update` step only fires on (re)start → **updating clauDNA today requires a restart**. The cache is **shared host-wide** (`start-bot.sh:161`: `~/.claude/plugins/cache/` — first bot fetches, others no-op) — so plugin delivery is effectively a host/fleet-level property, not per-bot.
- **`--plugin-dir` has zero occurrences** anywhere in the repo. No local-checkout plugin path exists.
- **clauDNA ships zero MCP servers** (verified: `~/.claude/plugins/cache/Claudfather/claudna/<ver>/.claude-plugin/plugin.json` has no `mcpServers`). So the `/reload-plugins` "skips MCP-tool changes unless `--force`" caveat **does not fire for clauDNA today** — it is a future contingency only, not a path this plan builds for.

**Composed skills are already a live-source channel.** `composer.link_skills` (`composer.py:565-614`) symlinks `library/skills/<name>` → `<bot>/.claude/skills/<leaf>` (it `unlink`s then re-creates every symlink on each generate — `:585-590`). Editing a linked `SKILL.md` updates it in place with no recompose and no restart. `ari` has 10 such symlinks; `alex` has 0. **`--plugin-dir` is the plugin-level analog of this existing symlink-to-live-source pattern.**

**The seamless-restart machinery is ~80% built and disconnected:**
- `lib/pre-stop-handoff.sh` sends `/session-handoff --auto` into the live session and waits up to 30s for `$BOT_DIR/.claude/session.md` (5-min freshness skip). It is **wired into nothing** — the composed systemd unit's `ExecStop` is a bare `tmux kill-session` (`composer.py:506-508`).
- `start-bot.sh:225-238` sends `STARTUP_PROMPT` into the session on **every** launch (two-step send + verify-retry, `set +H;`-prefixed). **No bot's `STARTUP_PROMPT` invokes `/session-resume`** — and every live fleet bot sets a **custom** `startup_prompt` (`fleet.yaml`, all 8+ bots), so the composer default (`composer.py:449-455`, the `else` branch) reaches **zero** current bots.
- The clauDNA `session-handoff`/`session-resume` skills are keyed by **cwd = `$BOT_DIR`** → file `$BOT_DIR/.claude/session.md`, **exactly** matching `pre-stop-handoff.sh`. Both support `--auto`. The file contract aligns; only the wiring is disconnected.
- `library/skills/restart/SKILL.md` implements the full `handoff → restart → resume` flow but is **launchctl-only**, assigned **only to `ari`**, and assumes the two unwired hooks above.
- **Restart topology has 5+ entrypoints**, and the highest-frequency one — `keepalive.sh` — calls `systemctl --user restart` **directly** on session death (every 60s tick), bypassing both `spin-up-bot.sh` and any handoff. The others (`spin-up-bot.sh`, `update-claude-code.sh:79-88`, `reconcile-fleet.sh --enroll`, `move-bot`) route through `spin-up-bot.sh`. A crash-restart has **no live session to hand off from** by definition.

**Reusable broadcast/idle/iteration primitives exist:** `dispatch.sh` (race-safe two-step `set +H;` send), `lib-common.sh` `marker_is_newer "$bot/data/.idle" "$bot/data/.last-tool-call"` and `pane_is_idle` (live capture-pane classifier; `.idle` is written **only** by `keepalive.sh:154/161` on its 60s tick), `resolve_bots_dir` + the `for bot_dir in "$BOTS_DIR"/*/` loop (`fleet-pulse.sh:95-111`). Timers are declared in `claudlobby/system_defaults.yaml` `fleet_timers` and generated by `compose_fleet_timers` (`composer.py:1394-1455`). **No git hooks exist anywhere.** `tests/test_script_consumers.sh` fails if a new `lib/*.sh` is not referenced in CLAUDE.md/protocols/docs.

**Mission alignment.** `PROJECT_MISSION.md` frames consumption as *"Bots install clauDNA via marketplace plugin"* **and** *"Local-first, no required hosted dependencies,"* and lists new provisioning dependencies under **"Requires approval."** See F1 rationale for how the marketplace stays the external channel while the fleet flips internally.

---

## Decision Forks (OPEN — human ratification required)

### F1 — clauDNA fleet delivery · *Status: leaning (a), fleet-global*

- **Context:** clauDNA is the fleet's primary skill source and is marketplace-installed, so every clauDNA update (e.g. the just-migrated `claudna:ironclad`) is restart-bound. Flip the **fleet's** clauDNA to a `--plugin-dir` local checkout for live updates, or keep marketplace-install and rely on a seamless restart?
- **Options:**
  - **(a) `--plugin-dir` local checkout, fleet-global** — every bot on the fleet loads clauDNA from one pristine local git checkout (`git pull --ff-only` + `/reload-plugins` = live, no restart). Marketplace stays the external distribution + release channel. *Trade-off:* small new wiring in `composer.py` + `start-bot.sh`; the fleet must keep the checkout current (pinned as a hard invariant below); loses marketplace's self-healing re-fetch-on-restart.
  - **(b) keep marketplace-install + seamless-restart** — no plugin-loading change; lean entirely on F4. *Trade-off:* clauDNA updates **never** meet the no-restart goal (marketplace updates are restart-bound by definition); the no-restart win is limited to composed skills only.
- **Lean: (a), fleet-global.** It is the only option that delivers no-restart-by-default for clauDNA itself (the bulk of fleet skills, including `/ironclad`), and it **extends the existing live-source pattern** (`link_skills` already symlinks skills to a live source — `composer.py:565-614`) rather than forking a new mechanism. **Fleet-global** (not per-bot): the host plugin cache is shared (`start-bot.sh:161`), so a per-bot split would be incoherent; making all bots use the same delivery kills the mixed-host hazard and lets the broadcaster treat `/reload-plugins` as a fleet property, not a per-bot lookup. **Mission:** the marketplace consumption model stays true for the outside world — a clauDNA release is a `plugin.json` bump on `main` delivered to external consumers via `claude plugin update` (per `shared/knowledge/clauDNA/release-procedure.md`); the fleet's `--plugin-dir` is an internal override that is *more* local-first (mission line 19). **Robustness invariant (resolves the loss of marketplace self-healing):** the checkout is a **pristine, read-only clone** the fleet never commits into; `reload-fleet.sh`'s first step is `git -C <checkout> pull --ff-only`, and it **aborts the entire broadcast** if the pull fails or the tree is dirty — never serve a stale/dirty checkout. **Concrete `/ironclad` resolution:** flip the fleet to `--plugin-dir` → `/ironclad` updates land live via `git pull` + `/reload-plugins`, no restart.
- **Ratifier:** Human (Chris) — provisioning change, "Requires approval" per mission.
- **Status:** open
- **Evidence:** `composer.py:418-432`, `start-bot.sh:157-193` + `:161` (shared cache), `composer.py:565-614`.

### F2 — Reload-broadcast busy-pane policy · *Status: leaning (a)*

- **Context:** The broadcaster (`lib/reload-fleet.sh`) runs `claudlobby generate` then sends `/reload-skills` (+ `/reload-plugins` for F1(a)) to running bots. A bot mid-task must not be interrupted — Enter into a busy pane can submit ghost autocompletion text (`keepalive.sh:5-9`). How are busy bots handled?
- **Options:**
  - **(a) defer-busy with a pending-reload marker** — broadcast to idle bots now (live `capture-pane` + `pane_is_idle`, re-checked immediately before the Enter); for busy bots drop a `data/.reload-pending` marker that `keepalive.sh` consumes when the pane next goes idle. *Trade-off:* a perpetually-busy bot stays on stale skills until it idles (acceptable — reloads at its next natural boundary).
  - **(b) skip-busy, no marker** — broadcast to idle, log skipped busy. *Trade-off:* busy bots update only on next restart.
  - **(c) interrupt-all** — *Rejected:* ghost-text hazard, violates `safe-worker-restart`.
- **Lean: (a).** Idle bots reload now; busy bots converge at their next idle boundary via the marker (`keepalive.sh` already evaluates idle every tick — extend, don't fork). **Ordering invariant:** `claudlobby generate` must **complete** before any reload dispatches (generate `unlink`s/relinks `.claude/skills/` symlinks — `composer.py:585-590` — so a reload mid-relink could read a missing symlink). **Residual TOCTOU (named, accepted):** a bot can flip busy between the live idle-check and the Enter; the window is small and a `/reload-skills` keystroke into a busy pane is low-harm (queues as a follow-up message, not a destructive action). The broadcaster re-checks idle immediately before sending to shrink it. Sends via `dispatch.sh` (inherits the two-step `set +H;` Enter primitive).
- **Ratifier:** Framework (engineering judgment via `/weigh-development-paths`).
- **Status:** open
- **Evidence:** `dispatch.sh:14-27`, `lib-common.sh:259-295`, `keepalive.sh:148-162`, `fleet-pulse.sh:95-111`, `composer.py:585-590`.

### F3 — Trigger model · *Status: leaning (a + c)*

- **Context:** What fires `lib/reload-fleet.sh`?
- **Options:**
  - **(a) on-demand** — a manager-invocable command / `lib/` entrypoint run after skill/plugin changes land. Explicit, operator controls timing.
  - **(b) git post-merge hook** — auto-reload after a `git pull`. Most automatic, but would be the **first git hook in the repo** (no pattern), fires unpredictably, lives in untracked `.git/` (own installer needed).
  - **(c) timer** — a `fleet_timers` entry (`system_defaults.yaml` + `compose_fleet_timers`) that periodically pulls + generates + reloads, à la `update-claude-code.sh`. Predictable; reuses existing infra; lags merge→reload by the interval.
- **Lean: (a) primary + (c) opt-in, default off.** On-demand is the safe MVP (operator controls timing, pairs with F2 deferral). The timer is opt-in for hands-off fleets and **reuses the existing `fleet_timers`/`compose_fleet_timers` infra** (consolidate, don't fork). **Defer (b)** — a bespoke git-hook subsystem is a new mechanism class with interrupt risk for marginal benefit (YAGNI).
- **Ratifier:** Human (Chris).
- **Status:** open
- **Evidence:** `system_defaults.yaml` `fleet_timers`, `composer.py:1394-1455`, `install-claude-update-systemd.sh:19-52`.

### F4 — Seamless-restart scope & handoff trigger · *Status: leaning (a)*

- **Context:** Some restarts are unavoidable (Claude Code binary update via `update-claude-code.sh`; an operator restart). They should be lossless. Both handoff/resume endpoints exist and the file contract aligns — but the trigger is unwired, and a **crash-restart has no live session to hand off from**. What's the scope and where does handoff fire?
- **Options:**
  - **(a) resume-universal + handoff-before-intentional-stop** — make **`/session-resume --auto` run on every start** (covers intentional *and* crash restarts; resume reads the last `session.md`). Drive **`/session-handoff` from the intentional restart path** (`spin-up-bot.sh` / `update-claude-code.sh` / restart skill call `pre-stop-handoff.sh` **before** the restart) with a **short timeout (~10s) that never blocks the restart**. Crash-restarts (keepalive's direct `systemctl restart`) get **resume-from-last-checkpoint** (no fresh handoff possible) + the existing "commit WIP" discipline. *Trade-off:* crash-restart losslessness is best-effort (bounded by how fresh the last `session.md` is).
  - **(b) `ExecStop=pre-stop-handoff.sh`** wired into the composed unit (`composer.py:506-508`) + launchd equivalent. Matches the script's documented intent, but `ExecStop` ordering is uncertain for the bot's unit type and only fires on supervised stop, not crash or ad-hoc restart.
- **Lean: (a).** Resume on every start is the universal win — it closes the loop for **all** restart entrypoints (including keepalive's direct-systemctl crash path) because resume only needs the last `session.md`, not a fresh handoff. Handoff-before-intentional-stop captures fresh state when a live session exists; the short non-blocking timeout means a hung bot never stalls a restart (today there's no wait, so the bound must be tight). **This honestly scopes the guarantee:** intentional restarts are lossless; crash-restarts are best-effort from the last checkpoint — the plan does **not** claim a single path makes every restart lossless. **Resume injection (not the no-op default):** because all bots set custom `startup_prompt`, the resume must be injected for **every** bot — `start-bot.sh` sends `/claudna:session-resume --auto` as a separate first keystroke **before** `STARTUP_PROMPT` (or the composer prepends it to the rendered prompt), not via the dead `else`-branch default. Then **generalize `library/skills/restart` to cross-platform** (`systemctl --user restart` + `launchctl kickstart`) as the single *intentional* restart entrypoint, and retire the raw context-destroying bounce in `update-claude-code.sh` in favor of it (consolidate, no shim).
- **Ratifier:** Human (Chris).
- **Status:** open
- **Evidence:** `pre-stop-handoff.sh` (unwired, 30s wait), `keepalive.sh` (direct `systemctl restart`, the crash path), `start-bot.sh:225-238` + `composer.py:449-455` (custom prompts ⇒ default is a no-op), `library/skills/restart/SKILL.md` (launchctl-only), `update-claude-code.sh:79-88`.

---

## File Structure

```
documentation/plans/2026-06-14-fleet-skill-plugin-update-lifecycle.md   # this plan
documentation/fleet-update-lifecycle.md                                 # NEW — codified lifecycle doc (Phase 5)
lib/reload-fleet.sh                                                      # NEW — pull + generate + broadcast /reload-skills(+plugins) (Phase 2)
lib/install-reload-fleet-systemd.sh                                      # NEW (opt-in) — enroll the timer (Phase 3c)
claudlobby/config.py                                                     # EDIT — PluginsConfig fleet-global local-dir field (Phase 1)
claudlobby/composer.py                                                   # EDIT — emit --plugin-dir wiring (P1); resume-injection (P4); fleet_timers entry (P3c)
claudlobby/system_defaults.yaml                                          # EDIT — reload-fleet timer entry (Phase 3c)
lib/start-bot.sh                                                         # EDIT — consume --plugin-dir; branch plugin install block (P1); inject /session-resume before STARTUP_PROMPT (P4)
lib/spin-up-bot.sh, lib/update-claude-code.sh                           # EDIT — drive handoff before intentional restart; route bounce through the restart skill (P4)
library/skills/restart/SKILL.md                                          # EDIT — cross-platform; single intentional-restart entrypoint (P4)
library/skills/lifecycle/ (or a manager skill)                           # EDIT — expose on-demand reload (Phase 3a)
fleet.yaml.example, documentation/fleet-yaml-schema.md, environment-variables.md, CLAUDE.md  # EDIT — document knobs (P1, P5)
tests/                                                                   # EDIT — composer/config tests; extend validate-bot-change.sh (all phases)
```

## Phases

### Phase 0 — Ratify forks · **Gate**
- [ ] Lock F1–F4 via `[FORK-LOCK F<N>]` comments. F1, F3, F4 need Chris; F2 is engineering judgment. No implementation until F1 and F4 lock (they set the delivery + restart shape).

### Phase 1 — clauDNA via `--plugin-dir` (fleet-global) · **M** · *gated by F1(a)*
- **Files:** `claudlobby/config.py`, `claudlobby/composer.py`, `lib/start-bot.sh`, `fleet.yaml.example`, `documentation/fleet-yaml-schema.md`, `documentation/environment-variables.md`, `tests/test_config.py`, `tests/test_composer.py`
- [ ] Add a **fleet-global** local-checkout field to `PluginsConfig` (`config.py:237-241`) + parse in `_coerce_plugins` (`config.py:348-378`) — e.g. `plugins.local_dir: {plugin: path}` applied to all bots. Keep the `{source, repo}` marketplace schema intact (no local `path` source variant).
- [ ] `composer.py`: emit the resolved checkout path (extend the plugin-sync block `:418-432` to write `FLEET_PLUGIN_DIR`, or append `--plugin-dir <path>` in `CLAUDE_FLAGS` assembly `:315-332`). When `--plugin-dir` is set, **skip** emitting the marketplace `FLEET_PLUGINS_*`/`enabledPlugins` for that plugin (consolidate — one delivery path, not both).
- [ ] `start-bot.sh`: append `--plugin-dir "$dir"` to the launch (`:150-155`) and **branch** the install/update block (`:157-193`) so the `--plugin-dir` plugin skips `claude plugin marketplace add`/`install`/`update`.
- [ ] Document the knob in the (currently commented-out) `fleet.yaml.example` plugins block (`:38-45`) + schema docs. Marketplace stays the default; `--plugin-dir` is opt-in per fleet, fleet-global when set.
- [ ] **Validate:** *(harness)* `claudlobby generate` → assert `bot.conf`/`CLAUDE_FLAGS` carry `--plugin-dir` and the marketplace plugin lines are absent; `pytest` composer/config tests. *(manual, live auth)* spin a test bot, confirm `/ironclad` resolves from the checkout, edit the checkout + `git pull` + `/reload-plugins`, confirm the change is live with no restart. Cite both.

### Phase 2 — `lib/reload-fleet.sh` broadcaster · **M** · *gated by F2*
- **Files:** `lib/reload-fleet.sh` (new), CLAUDE.md `lib/` table + lifecycle doc (for `test_script_consumers.sh`), `lib/keepalive.sh` (consume `.reload-pending`), `lib/validate-bot-change.sh` (extend)
- [ ] `lib/reload-fleet.sh <fleet>`: source `lib-common.sh`; **first**, for the F1(a) checkout, `git -C <checkout> pull --ff-only` and **abort the whole run** if it fails or the tree is dirty (F1 invariant — never serve a stale/dirty checkout); then run `claudlobby --fleet "$fleet" generate` to **completion**; then iterate `resolve_bots_dir` → running bots (`check_tmux_session`) → classify idle via live `capture-pane` + `pane_is_idle` (re-check right before send); idle → `dispatch.sh "$session" "/reload-skills"` (+ `/reload-plugins` if the fleet uses `--plugin-dir`); busy → `touch "$bot_dir/data/.reload-pending"`.
- [ ] `keepalive.sh`: on IDLE classification, if `.reload-pending` exists, fire the reload and clear the marker (the deferral consumer).
- [ ] Reference the script in CLAUDE.md's `lib/` table + the lifecycle doc (or `test_script_consumers.sh` fails).
- [ ] **Validate:** *(harness — what it CAN prove)* extend `validate-bot-change.sh` to assert the idle test bot's pane received the `/reload-skills` keystroke and the busy bot got a `.reload-pending` marker (pane uninterrupted), and that `generate` completed before any dispatch. *(manual, live auth — what the harness CANNOT prove)* that the skill actually reloaded in-session. State the split explicitly in the PR.
- [ ] **De-scoped (YAGNI):** no MCP-skip handling is built — clauDNA ships no MCP servers (verified). Contingency note only: *if* clauDNA later adds an MCP server, the broadcaster must detect a `/reload-plugins` MCP-skip and route that bot to the Phase 4 restart; not built until that exists.

### Phase 3 — Trigger wiring · **S** · *gated by F3*
- **Files:** Phase 3a: a manager/lifecycle skill exposing on-demand reload. Phase 3c (opt-in): `claudlobby/system_defaults.yaml`, `composer.py` (`compose_fleet_timers`), `lib/install-reload-fleet-systemd.sh` (new).
- [ ] **3a (primary):** expose `reload-fleet.sh` as an on-demand manager command (extend `library/skills/lifecycle` or a small dedicated skill). Document when to run it (after merging skill/library changes or pulling the clauDNA checkout).
- [ ] **3c (opt-in, default off):** add a `reload-fleet` entry to `fleet_timers` (`system_defaults.yaml`); `compose_fleet_timers` generates the unit; add a thin `install-reload-fleet-systemd.sh` cloning `install-fleet-pulse-systemd.sh`.

### Phase 4 — Seamless restart (lossless for intentional; best-effort for crash) · **M** · *gated by F4(a)*
- **Files:** `lib/start-bot.sh` (`:225-238` resume injection), `claudlobby/composer.py`, `lib/spin-up-bot.sh`, `lib/update-claude-code.sh`, `library/skills/restart/SKILL.md`, `library/protocols/worker-lifecycle.md`, `lib/pre-stop-handoff.sh`, `lib/validate-bot-change.sh`
- [ ] **Resume-universal:** inject `/claudna:session-resume --auto` as a separate first keystroke **before** `STARTUP_PROMPT` in `start-bot.sh:225-238` (works for every bot regardless of its custom prompt — the composer default-branch edit would be a no-op). Fires on every start, including keepalive crash-restarts.
- [ ] **Handoff-before-intentional-stop:** `spin-up-bot.sh` + `update-claude-code.sh`'s bounce call `pre-stop-handoff.sh "$bot_dir"` **before** the restart, with a **short timeout (~10s) and a hard rule that the restart proceeds regardless of handoff outcome** (never block a restart on a hung bot). Add an explicit `exit 0` to `pre-stop-handoff.sh`'s timeout path.
- [ ] **Consolidate the intentional-restart path:** generalize `library/skills/restart/SKILL.md` to cross-platform (`systemctl --user restart` + `launchctl kickstart`); route `update-claude-code.sh`'s bounce through the same handoff→restart→resume sequence; retire the raw bounce (no shim).
- [ ] Update `worker-lifecycle.md`'s "a restart will never lose your progress" to reference the real `session.md` continuity for intentional restarts (convention, not event).
- [ ] **Validate:** *(harness)* assert `session.md` was written pre-stop and that the new session's pane shows `/session-resume` was sent first. *(manual, live auth)* confirm the resume briefing actually loaded into context. State the split.

### Phase 5 — Codify + consolidate the lifecycle · **S**
- **Files:** `documentation/fleet-update-lifecycle.md` (new), CLAUDE.md, `documentation/fleet-yaml-schema.md`
- [ ] Write the canonical "Fleet Update Lifecycle" doc: the three delivery models, which is no-restart, when each restart path applies (intentional=lossless, crash=best-effort), the `reload-fleet` command, and the resume-on-every-start guarantee. Reconcile with PR #399's `update-claude-code.sh` (now routed through the lossless restart). One documented lifecycle, not three folk practices.

---

## Companion Plans
- **PR #399** (`feat(fleet): Claude Code update script…`, merged) — added `lib/update-claude-code.sh` (the daily fleet **bounce**). F4 **generalizes** that restart into a lossless one and Phase 5 reconciles them. Must not contradict.
- **clauDNA `/ironclad` migration** (Claudfather/clauDNA#146 + the `fleet-dispatch-capability` protocol) — produced the marketplace `claudna:ironclad` that is the concrete restart-to-update example F1 resolves.
- `shared/knowledge/clauDNA/release-procedure.md` — the marketplace release process F1 leaves unchanged.
- `shared/planning/active/deterministic-idle-detection.md` — source of the `.idle`/`.last-tool-call` markers F2 reuses.

## Dependencies

| Dependency | Blocks | Risk |
|---|---|---|
| `/reload-skills` / `/reload-plugins` behave as documented (live, no restart) on CLI v2.1.177 | Phases 1–3 | Low — verified by Chris |
| clauDNA ships no MCP servers (verified today) | F1/F2 scope | Resolved — keeps Phase 2 lean; re-check if clauDNA adds one |
| clauDNA 0.6.0 `session-handoff`/`session-resume` `--auto` maturity | Phase 4 | Low — present, contract aligned |
| A pristine local clauDNA checkout the fleet can `pull --ff-only` | Phase 1 (F1a) | Low — fleets already keep `projects/` checkouts |
| F1 + F4 locked | Phases 1, 2, 4 | Coordination — Phase 0 gate |

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `--plugin-dir` flip diverges from the mission's "marketplace plugin" line | Medium (alignment) | Marketplace stays external/release channel unchanged; `--plugin-dir` is internal + more local-first. Human-ratified (F1). |
| Local checkout drifts / dirty → serves stale skills fleet-wide (loss of marketplace self-healing) | Medium | F1 hard invariant: pristine read-only clone; `reload-fleet.sh` does `git pull --ff-only` first and **aborts on failure/dirty**. |
| Crash-restart can't hand off → context loss | Medium | Honestly scoped: resume-on-every-start restores from the last checkpoint; intentional restarts hand off fresh; "commit WIP" discipline backs the gap. Not over-claimed as lossless. |
| Handoff-before-restart adds latency / partial handoff on a hung bot | Low | Short (~10s) non-blocking timeout; restart proceeds regardless. |
| F2 TOCTOU: bot flips busy between idle-check and Enter | Low | Re-check idle right before send; `/reload-skills` into a busy pane is low-harm (queues, non-destructive). |
| `generate` mid-flight relinks `.claude/skills/` symlinks while a bot reads them | Low | Ordering invariant: `generate` completes before any reload dispatch (F2/Phase 2). |
| New `lib/reload-fleet.sh` orphaned → `test_script_consumers.sh` fails | Low | Phase 2 references it in CLAUDE.md + lifecycle doc. |

## Validation Strategy

Per the **MANDATORY** bot-behavior validation loop (CLAUDE.md): unit tests prove composition; only running proves behavior. **Critical scoping (from the author adversarial pass):** `lib/validate-bot-change.sh` runs **without Claude auth** — it can assert emitted events, marker files, dispatched keystrokes, and composed outputs, but it **cannot** observe that a skill actually reloaded or a resume briefing landed in context. Those require a **manual live-bot observation with real auth**. The table below splits the two; the PR must cite both.

| Criterion | Harness-provable (`validate-bot-change.sh` / `pytest`) | Requires manual live observation (real auth) |
|---|---|---|
| `--plugin-dir` lands in launch | ✅ grep `bot.conf`/`CLAUDE_FLAGS`; marketplace lines absent | — |
| clauDNA updates live (no restart) | — | ✅ edit checkout → `git pull` + `/reload-plugins` → changed skill behaves differently, same session |
| reload reaches idle, defers busy | ✅ idle pane received `/reload-skills` keystroke; busy bot has `.reload-pending`; generate-before-dispatch ordering | ✅ the skill actually reloaded |
| busy bot converges | ✅ `.reload-pending` cleared after `keepalive` sees idle | ✅ reload applied on convergence |
| intentional restart is lossless | ✅ `session.md` written pre-stop; new pane shows `/session-resume` sent first | ✅ resume briefing present in context |
| no composition regression | ✅ `pytest` green; `claudlobby validate`/`generate` clean | — |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 0. Ratify forks | S | — | — |
| 1. clauDNA `--plugin-dir` (fleet-global) | M | F1 locked | 4 |
| 2. `reload-fleet.sh` broadcaster | M | F2 locked, Phase 1 | 4 |
| 3. Trigger wiring | S | Phase 2 | 4 |
| 4. Seamless restart | M | F4 locked | 1, 2, 3 |
| 5. Codify + consolidate | S | 1–4 | — |

**Critical path:** 0 → (1 → 2 → 3) and (0 → 4) in parallel → 5.

## Spec Coverage (brief requirement → phase/fork)

| Brief requirement | Where |
|---|---|
| No-restart-by-default design | Architecture; Phases 1–3 |
| Three delivery models codified | Architecture table; Phase 5 |
| F1 clauDNA delivery (`--plugin-dir` vs marketplace+restart) | F1; Phase 1 |
| F2 reload-broadcast lib/ script (two-step Enter, busy-pane) | F2; Phase 2 |
| F3 trigger model (on-demand/hook/timer) | F3; Phase 3 |
| F4 seamless restart (handoff→resume) | F4; Phase 4 |
| Extend not fork (generate/composer, start-bot, update-claude-code, session skills) | Phases 1, 2, 4 cite exact extension points |
| No-shims + consolidate-not-fork | F1 (one delivery path, not both); F4 (single intentional-restart path, retire raw bounce) |
| `/ironclad` concrete example | F1 lean (resolved: `--plugin-dir` live-reload) |
| Sizes S/M/L, plan-only | Phase headers; this PR is the plan doc only |

## Adversarial Review Findings

Author-side adversarial pass run before handoff; blockers + majors folded into v2 above. `[x]` = resolved in this draft; `[ ]` = open for ratifier / `/ironclad`.

- [x] **F4 over-claimed a single lossless restart path; keepalive's 60s crash-restart bypasses handoff.** *(critical)* Resolved: F4 rescoped — resume-on-every-start covers all entrypoints (incl. crash) from the last checkpoint; handoff is fresh only for intentional restarts; crash losslessness is honestly labeled best-effort. The over-claim is removed.
- [x] **Empirical-validation story unachievable with the auth-free harness.** *(critical)* Resolved: Validation Strategy now splits harness-provable (markers, keystrokes, composition) from manual-live-with-auth (reload/resume actually applied). No phase claims the harness proves live behavior.
- [x] **MCP-skip plumbing was YAGNI — clauDNA ships zero MCP servers (verified).** *(major)* Resolved: cut the MCP routing from F1/F2/Risks; kept a single contingency note in Phase 2.
- [x] **F1 should be fleet-global, not per-bot (shared host cache; mixed-host incoherence).** *(major)* Resolved: F1 is now explicitly fleet-global; the broadcaster treats `/reload-plugins` as a fleet property; mixed-host open item dropped.
- [x] **F1 checkout-currency unpinned → not ratifiable.** *(major)* Resolved: F1 carries a hard invariant (pristine read-only clone; `pull --ff-only` first; abort on failure/dirty).
- [x] **Phase 4 resume-default was a no-op (all bots set custom `startup_prompt`).** *(minor)* Resolved: resume is injected as a separate first keystroke in `start-bot.sh`, not the dead `else`-branch default.
- [x] **`pre-stop-handoff.sh` latency / exit semantics when driven from the restart path.** *(minor)* Resolved: short non-blocking ~10s timeout, restart proceeds regardless, explicit `exit 0`.
- [x] **Citation drift** *(info)* — re-pinned `settings.local.json` block to `composer.py:1059-1063`; noted the `fleet.yaml.example` plugins block is currently commented out.
- [ ] **Open: busy-bot staleness bound.** F2 converges a busy bot at next idle but sets no max-staleness. Is a force-reload/restart-after-N-hours escalation worth it, or YAGNI? (Ratifier input.)
- [ ] **Open: crash-restart checkpoint freshness.** Resume-from-last-checkpoint is only as good as the last handoff. Worth a periodic/proactive `session-handoff` checkpoint (e.g. on `/compact`, or a low-frequency timer), or rely on intentional-restart handoffs + WIP discipline? (Ratifier input.)
