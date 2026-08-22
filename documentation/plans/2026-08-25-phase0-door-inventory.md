# Phase-0 door & callsite inventory — DRAFT (started 2026-08-25)

**Status:** COMPLETE for the callsite matrix (2026-08-25) — verified against CURRENT `origin/main` via `git grep`/`git cat-file` on the ref itself, not a stale checkout. Remaining sub-items are listed at the end and are Phase-2-design work, not inventory work. The five disproof targets each carry their evidence state.

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

## Per-door matrix (complete — measured on origin/main 2026-08-25)

| Door | ID minting | Authoritative sink | Write/send order | Return semantics | Retry | Legacy readers |
|---|---|---|---|---|---|---|
| `dispatch-task.sh` | task_id for `--type task` only (mint_task_id); other types + freeform: none | `state/dispatch-log.jsonl` (flock) | **LEDGER then send** (header: "record the task to the dispatch ledger, then send" — confirmed on main) | nonzero on missing session/empty task; send failures via dispatch.sh | none | dispatch-overdue.py (watchdog + open doors), brief.py, supersede-hint |
| `dispatch.sh` (transport) | none | none (ledger-less by design) | send only | `bot_tmux_send` semantics; send_miss logged on miss | one verify-retry inside pane_send_verified path | none |
| `report-back.sh` | resolves open task_id via dispatch-overdue `--open-task` when omitted | `<fleet>/report-back.jsonl` (flock) | **SEND then ledger** (:152 send → :157 emit — confirmed on main; inverse of dispatch-task) | `\|\| true` on BOTH legs — caller never sees failure | none | dispatch-overdue join, brief.py, who-reviewed.py |
| `tg-post.sh` | none | none (stateless) | send only | exit 0 even on REJECTED sends for env-less callers (header-documented) | none | none |
| Telegram bridge (external plugin) | carrier msg ids per part | plugin-internal | chunks one message → MULTIPLE API sends; can partially fail | plugin-internal | plugin-internal | none |
| `workstream-update.sh` | ws-slug ids (single-writer) | `workstreams.json` (in-place mutate, lock) | mutate only (no send) | nonzero on unknown id/verb | none | brief.py, fleet-pulse stall checks, sprint selection |
| `fleet-pulse.sh` | none | per-bot `data/events/*.jsonl` + debounce state | detect → emit event → inject alert (bot_tmux_send) + tg-post, independently | sweep-level; per-check `\|\| true` | debounce, not retry | fleet-pulse decision table (self), events CLI |
| `keepalive.sh` | none | keepalive.log + events JSONL | detect → restart → event; reload nudges via pane_send_verified | watchdog loop | 60s cycle IS the retry | uptime.py (log), events CLI |
| `start-bot.sh` | none | startup events; rc markers | boot → inject STARTUP_PROMPT (pane_send_verified ×5 sites incl. never-drawn repair) | rc_timeout burst-detected once | verify-retry + never-drawn resend | selfstart-snapshot, boot samplers |
| `sprint-trigger.sh` / `bot-sweep-cron.sh` | none | none | inject nudge (bot_tmux_send) | cron-level | none | none |
| `briefing-trigger.sh` | none | none | `dispatch.sh <bot> "/briefing SLOT"` — ID-less BY DESIGN (slash command must be first chars in pane) | exit of dispatch.sh | none | none |
| `telegram-instant-ack.sh` | none | none | tg send only | ack-level | none | none |

**Shim classification that falls out:** semantic communications = dispatch-task (all types), report-back, tg-post bodies, bridge both directions · control-plane raw = keepalive/start-bot/pre-stop injections, sprint/sweep nudges · **judgment row:** briefing-trigger is a *scheduled command delivery* — semantically a `briefing`-class communication carried as a raw slash injection; the shim should mint it a communication (class=briefing, command payload in body) rather than leave it in the raw net. Harness callers (validate-bot-change, ab-comms-eval, boot samplers) are net-exempt via their existing stub/throwaway conventions; `personal/evening-audit.sh` is operator-local → exempt, documented.

## Remaining (Phase-2 design work, not inventory)
Exact ambiguous-success branch list from `pane_send_verified`; the harness-exemption mechanism; evening-audit ruling.
