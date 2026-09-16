# Phase-0 door & callsite inventory

**Status:** COMPLETE — rebuilt 2026-08-23 against **origin/main @ `e9311da`** (every claim below measured on that ref via `git grep`/`git show`, or on the installed artifact where noted; the round-4 rebuild had reused stale-checkout facts — most damagingly tg-post's exit semantics — and this version supersedes it entirely). Bridge facts measured on the installed plugin. Ref-pinning rule: any future re-verification names its ref first.

**2026-08-28 docs-audit addendum — activity vs. currency.** This inventory's status line marks the
*audit activity* complete; it does not claim the measured facts below track main forever. `main`
has since moved through Phase-2 PR-A (`df43c70` #1345), PR-B (`c34f12d` #1372) and an 8-reviewer
gauntlet fix round (`039ede4`) — none of which existed at `e9311da`. Spot-checked two of the
table's claims against the current worktree (HEAD `039ede4`): the **tg-post.sh exit-3-on-rejected-send**
row and the **dispatch-task.sh ledger-then-send** row are both still accurate as stated — verified
directly against `lib/tg-post.sh` (`exit 3` on a parsed `.ok:false`) and `lib/dispatch-task.sh`
(`with_lock "$LEDGER.lock" _append_ledger` at what is now line 635, before the `lib/dispatch.sh`
transport call at line 646). `report-back.sh`'s **send-then-ledger** row (its *legacy* JSONL
ledger) is likewise unchanged — `bot_tmux_send` still precedes the `_emit_ledger_event` call.
None of PR-A/PR-B/the gauntlet touched these legacy facts; what they added sits *beside* them: the
five doors this inventory names as "semantic communications" (dispatch-task, report-back, tg-post,
plus workstream-update and briefing-trigger per the Shim classification section below) now also
carry an additive, dormant-by-default plane dual-write layer (`PLANE_EMIT_ENABLED=1`) that records
its own intent-before-send order on the plane side without altering any behavior measured here —
see `2026-08-24-observable-plane-phase2-ingest.md`'s Outcome section. This was a two-claim
spot-check, not a full re-inventory; other rows (the bridge's per-chunk behavior, `pane_send_verified`'s
ambiguous-success branches, etc.) were not re-verified and should not be assumed current without
checking.

## Per-door matrix (measured @ e9311da)

| Door | ID minting | Authoritative sink | Write/send order | Return semantics | Retry | Legacy readers |
|---|---|---|---|---|---|---|
| `dispatch-task.sh` | task_id for `--type task` only; other types + freeform: none (BY DESIGN) | `state/dispatch-log.jsonl` (flock) | **LEDGER then send** (its own header: "record the task to the dispatch ledger, then send") | nonzero on missing session/empty task; transport result via dispatch.sh | none | dispatch-overdue.py, brief.py, supersede-hint |
| `dispatch.sh` (transport) | none | none (ledger-less by design) | send only | bot_tmux_send semantics; send_miss logged on miss | verify-retry inside the pane path | none |
| `report-back.sh` | resolves open task_id via `--open-task` when omitted | `<fleet>/report-back.jsonl` (flock) | **SEND then ledger** (:152 send → the ledger CALL at :184; :157 declares the function — inverse of dispatch-task) | `\|\| true` on BOTH legs — caller never sees failure | none | dispatch-overdue join, brief.py, who-reviewed.py |
| `tg-post.sh` | none | none (stateless) | send only | **exit 3 on REJECTED sends since `bd4c0aa` (2026-07-24)** — the round-4 inventory's "exit 0" claim was stale-checkout reading. (Round-6 retraction: the :58 header comment is NOT stale — read in full it explains the OLD pipeline bug and states the current implementation parses `.ok` and exits nonzero; my :58 grep excerpt truncated it. No upstream defect exists.) | none | none |
| Telegram bridge — **OUTBOUND replies only** (installed plugin `telegram/0.0.7`; 0.0.6 also cached; inbound is a separate Phase-2 measurement, see Remaining) | Telegram message_ids per chunk, collected in a LOCAL `sentIds[]` — **not persisted to any authoritative store** | **none** (the plugin state dir holds approval/access state, never outbound send history) | one reply → `chunk(text, limit≤4096, mode length\|newline)` → **sequential per-chunk `sendMessage`**; reply-threading on first chunk by default; attachment sends run OUTSIDE the text-loop error wrapper | **mid-loop text failure throws `"reply failed after N of M chunk(s) sent"`** — the completed-part COUNT is reported, the collected ids are not | **none** — no retry in the loop | none |
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
3. **Fanout partial success** — CONFIRMED (alert emitters; bridge chunk loop measured: fail-after-N-of-M with counts). Per-part transmissions are a **Phase-2 ADAPTER requirement** — the bridge exposes chunk boundaries only via its error message and local ids, so the adapter must intercept at the send loop (or wrap it), not merely read what the bridge already persists. **Correction:** tg-post DOES now distinguish rejection (exit 3) — the shim records `failed` from a nonzero exit rather than compensating for a blind caller contract.
4. **Report/dispatch write-order inversions** — CONFIRMED both directions on e9311da; intent-first shim ordering CHANGES report-back's crash exposure — explicit canary item.
5. **Loss of next/terminal disposition** — CLOSED at the model (F21 v2.1); the shim's verb-mapping table proves flag coverage (Phase-2).

## Remaining (Phase-2 design work, not inventory)

Exact ambiguous-success branch list from `pane_send_verified` · the exemption *mechanism* (rulings above are settled) · the shim verb-mapping table for workstream-update · **inbound bridge measurement** (polling retry, notification delivery, dedupe behavior — the outbound row above deliberately excludes it) · the per-part transmission ADAPTER design (see disproof target 3).
