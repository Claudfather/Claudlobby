# Phase-0 door & callsite inventory — DRAFT (started 2026-08-25)

**Status:** DRAFT — the mandatory pre-Phase-2 inventory (spec §13; made mandatory by the round-2 review). Measured against `origin/main`-derived worktree at the plane design branch. Each section names the caller set (grep-measured, not assumed), its classification, and what the shim design must handle. The five disproof targets each carry their evidence state.

## Caller inventory (measured)

### `pane_send_verified` (the keystroke primitive — tier-1 net site)
`keepalive.sh` (reload/restart nudges) · `start-bot.sh` (STARTUP_PROMPT injection) · `pre-stop-handoff.sh` (handoff prompt) · `boot-strand-sampler.sh` + `validate-bot-change.sh` (harnesses — **exempt from the net**: they run against throwaway bots; classify by env flag or socket namespace, TBD in shim design).
Classification: ALL control-plane (`raw_control` if un-doored) — none is a semantic bot→bot communication today.

### `bot_tmux_send` (raw transport — callers that bypass even the verify wrapper)
`dispatch.sh` (the manager send door) · `report-back.sh` (worker→manager line) · `fleet-pulse.sh` (alert injection into manager panes) · `sprint-trigger.sh` + `bot-sweep-cron.sh` (nudges) · `briefing-trigger.sh` via dispatch.sh · `personal/evening-audit.sh` (**operator-local, fleet-external — decide: exempt or door**).
Note: the net must live at `bot_tmux_send` level or cover both — `pane_send_verified` alone misses these callers.

### Alert/notice emitters (fanout class)
`host-health-check.sh`, `fleet-memory-check.sh`, `disk-monitor` (via lib-common emit helpers), `creds-check.sh`, `orphan-browser-reaper.sh`, `notify-behind.sh`, `fleet-pulse.sh` — pattern: ONE semantic signal → tg-post AND (sometimes) manager-pane injection, independently, with independent failure. Shim requirement: one communication intent, N transmissions (per carrier), causation to the system event.

### Dispatch doors
`dispatch-task.sh` (the id-minting door; `--type task|cancel|compact|restart|query`, only `task` mints) → calls `dispatch.sh` for transport. Direct `dispatch.sh` callers that skip the task door: `briefing-trigger.sh`, skills (`dispatch`, `delegate` SKILL.md documented usage), `ab-comms-eval.sh` (harness).

## The five disproof targets — evidence state

1. **ID-less paths that matter** — CONFIRMED REAL: freeform `dispatch.sh` sends (briefing-trigger, skill-documented ad-hoc sends) and all `--type` non-task envelopes carry no task id BY DESIGN. Shim consequence: these become communications with `command_type` set and no work_item — the model already represents them; the disproof is that NO caller depends on id-less sends being *unrecorded*.
2. **Ambiguous-success branches** — CONFIRMED (round-2 review + lib-common source): `pane_send_verified` returns success on acknowledged-ambiguous branches (documented bounds in source). Shim consequence: transmission state for those branches must be `unknown`, never `pane_submitted`. Enumerate the exact branches in the full pass. **[remaining work]**
3. **Fanout partial success** — CONFIRMED REAL (alert emitters above). Modeled: per-carrier transmissions. Disproof needed: no caller treats "tg-post returned 0" as fleet-wide delivery. `tg-post.sh` exits 0 on REJECTED sends for env-less callers — so today some callers CANNOT distinguish; the transmission `outcome` fixes what the caller contract cannot. **[verified: tg-post.sh header]**
4. **Report/dispatch write-order inversions** — CONFIRMED (round-2 review, re-verified): dispatch-task ledgers BEFORE send; report-back sends BEFORE ledger. Shim consequence: communication-intent-first ordering CHANGES report-back's crash exposure (send-then-crash currently loses the ledger row; intent-first records it) — behavior change to canary explicitly, not silently.
5. **Loss of next/terminal disposition** — CLOSED at the model (round-3 F10: open-time `progressed(next_step)`; `closed.disposition`); remaining work is the shim's verb-mapping table proving every `workstream-update.sh` flag lands somewhere.

## Remaining for the full inventory (Phase-0 completion)
Per-door matrix rows (ID minting · authoritative sink · write/send order · return semantics · retry behavior · legacy readers) for: dispatch-task, dispatch, report-back, tg-post, bridge (in+out, version floor), workstream-update, fleet-pulse, keepalive, start-bot, sprint/briefing/sweep triggers, telegram-instant-ack. Plus: exact ambiguous-success branch list from `pane_send_verified`; the harness-exemption mechanism; evening-audit ruling.
