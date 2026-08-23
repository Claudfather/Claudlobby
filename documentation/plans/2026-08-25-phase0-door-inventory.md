# Phase-0 door & callsite inventory

**Status:** COMPLETE — rebuilt 2026-08-23 against **origin/main @ `e9311da`** (every claim below measured on that ref via `git grep`/`git show`, or on the installed artifact where noted; the round-4 rebuild had reused stale-checkout facts — most damagingly tg-post's exit semantics — and this version supersedes it entirely). Bridge facts measured on the installed plugin. Ref-pinning rule: any future re-verification names its ref first.

## Per-door matrix (measured @ e9311da)

| Door | ID minting | Authoritative sink | Write/send order | Return semantics | Retry | Legacy readers |
|---|---|---|---|---|---|---|
| `dispatch-task.sh` | task_id for `--type task` only; other types + freeform: none (BY DESIGN) | `state/dispatch-log.jsonl` (flock) | **LEDGER then send** (its own header: "record the task to the dispatch ledger, then send") | nonzero on missing session/empty task; transport result via dispatch.sh | none | dispatch-overdue.py, brief.py, supersede-hint |
| `dispatch.sh` (transport) | none | none (ledger-less by design) | send only | bot_tmux_send semantics; send_miss logged on miss | verify-retry inside the pane path | none |
| `report-back.sh` | resolves open task_id via `--open-task` when omitted | `<fleet>/report-back.jsonl` (flock) | **SEND then ledger** (:152 send → :157 emit — inverse of dispatch-task) | `\|\| true` on BOTH legs — caller never sees failure | none | dispatch-overdue join, brief.py, who-reviewed.py |
| `tg-post.sh` | none | none (stateless) | send only | **exit 3 on REJECTED sends since `bd4c0aa` (2026-07-24)** — the round-4 inventory's "exit 0" claim was stale-checkout reading; NOTE: the file's own :58 header comment STILL SAYS exit-0 on current main while :76 exits 3 — a live doc-drift defect worth filing upstream | none | none |
| Telegram bridge (installed plugin `telegram/0.0.7`; 0.0.6 also cached) | Telegram message_ids per chunk (`sentIds[]`) | plugin-internal state dir | one reply → `chunk(text, limit≤4096, mode length\|newline)` → **sequential per-chunk `sendMessage`**; reply-threading on first chunk by default | **mid-loop failure throws `"reply failed after N of M chunk(s) sent"`** — partial success REPORTED with exact counts (maps 1:1 onto per-part transmissions) | **none** — no retry in the loop | none |
| `workstream-update.sh` | ws-slug ids (single-writer) | `workstreams.json` (in-place mutate, lock) | mutate only | nonzero on unknown id/verb; carries `--next` + close `--status done\|abandoned` (now in F21) | none | brief.py, fleet-pulse stall checks, sprint selection |
| `fleet-pulse.sh` | none | per-bot `data/events/*.jsonl` + debounce state | detect → event → inject alert (bot_tmux_send) + tg-post, independently | sweep-level; per-check `\|\| true` | debounce, not retry | its own decision table; events CLI |
| `keepalive.sh` | none | keepalive.log + events JSONL | detect → restart → event; reload nudges via pane_send_verified | watchdog loop | the 60s cycle IS the retry | uptime.py, events CLI |
| `start-bot.sh` | none | startup events; rc markers | boot → **exactly 2 direct `pane_send_verified` call sites** (:403 resume-cmd, :418 STARTUP_PROMPT — the round-4 "×5" counted grep matches incl. comments); failure alerts go via emit_failure_alert → bot_tmux_send | rc_timeout burst-detected once | verify-retry + never-drawn resend (inside the primitive) | selfstart-snapshot, boot samplers |
| `sprint-trigger.sh` / `bot-sweep-cron.sh` | none | none | inject nudge (bot_tmux_send) | cron-level | none | none |
| `briefing-trigger.sh` | none | none | `dispatch.sh <bot> "/briefing SLOT"` — ID-less BY DESIGN | exit of dispatch.sh | none | none |
| `telegram-instant-ack.sh` | none | none | tg send only | ack-level | none | none |

**Caller-set corrections vs round-4:** `boot-strand-sampler.sh` and `validate-bot-change.sh` are NOT `pane_send_verified` callers on main — their grep matches are comments and a test-name reference; they are removed from the caller list (their harness exemption question therefore mostly dissolves; validate-bot-change drives doors as a harness and is exempted below).

## Shim classification (rulings — settled here)

- **Semantic communications:** dispatch-task (all `--type`s + freeform), report-back, tg-post bodies, bridge both directions.
- **Control-plane raw:** keepalive/start-bot/pre-stop injections; sprint/sweep nudges.
- **Judgment row:** briefing-trigger = a `briefing`-class communication carried as a raw slash injection; the shim mints it a communication.
- **Exemptions (ruled):** harnesses (`validate-bot-change.sh`, `ab-comms-eval.sh`, boot samplers) are net-exempt; `personal/evening-audit.sh` is operator-local and exempt. **Mechanism = Phase-2 design work** (env flag vs socket namespace), listed under Remaining — the RULING is settled, only the mechanics are open.

## The five disproof targets — evidence state

1. **ID-less paths that matter** — CONFIRMED REAL and representable (communications with `command_type`, no work_item); disproof standard: no caller depends on id-less sends being unrecorded.
2. **Ambiguous-success branches** — CONFIRMED (`pane_send_verified` returns success on acknowledged-ambiguous branches); shim maps those to transmission `unknown`, never `pane_submitted`. Exact branch enumeration = Phase-2 (Remaining).
3. **Fanout partial success** — CONFIRMED (alert emitters; bridge chunk loop measured: fail-after-N-of-M with counts). Modeled: per-carrier and per-part transmissions. **Correction:** tg-post DOES now distinguish rejection (exit 3) — the shim records `failed` from a nonzero exit rather than compensating for a blind caller contract.
4. **Report/dispatch write-order inversions** — CONFIRMED both directions on e9311da; intent-first shim ordering CHANGES report-back's crash exposure — explicit canary item.
5. **Loss of next/terminal disposition** — CLOSED at the model (F21 v2.1); the shim's verb-mapping table proves flag coverage (Phase-2).

## Remaining (Phase-2 design work, not inventory)

Exact ambiguous-success branch list from `pane_send_verified` · the exemption *mechanism* (rulings above are settled) · the shim verb-mapping table for workstream-update · upstream fix for tg-post's stale :58 header comment (filed as a claudlobby issue when the branch merges).
