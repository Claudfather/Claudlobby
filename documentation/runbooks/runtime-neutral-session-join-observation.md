---
title: Runtime-neutral session join observation
type: runbook
status: pending
created: 2026-10-15
tags: [observability, canary]
links: ["#2145"]
---

# Runtime-neutral session join observation

This is the remaining P1 Half B runtime gate from
[Task 11](../plans/2026-10-04-runtime-neutral-observability-p1-claudlobby-runtime-and-join-key.md).
A model-free CLI/Plane rehearsal proves stored joins and request replay; it does
not prove a live session's env, supervision, or either observation window.
**Status: pending.** No shortened or synthetic window satisfies this gate.

Use one independent canary root with its own fleet labels, host `unit_prefix`,
Plane state, native units and throwaway channels. Follow
[validating bot changes](../validating-bot-changes.md). Keep production roots out
of every command. Preserve the baseline release ID, candidate release ID, plan
ID, source revisions, exact CLI paths, activation times and UTC window endpoints
in private evidence. Publish only scrubbed observations in the epic run log.

1. **Before activation:** run the existing canary release with normal keepalive
   and fleet-pulse cadence for a full 48 hours. Record `BEFORE_START` and
   `BEFORE_END` in UTC; their elapsed duration must be at least 172800 seconds.
   Save `fleet uptime`, `event list` and `plane samples bot.heartbeat` outputs using that
   release's supported flags (`--help` names them). Retain the canary's
   keepalive/pulse logs for the same interval. Count `keepalive_restart` events
   by bot and cause, retain every `activity_stuck` event and compare the
   `bot.heartbeat` verdicts (BUSY/IDLE/HELD/UNKNOWN/LIMIT as supported by that
   release). Missing samples or unreachable reads are evidence gaps, never zero.
2. **Stage and activate:** seal the reviewed candidate, run
   `claudlobby --root ROOT config plan --release RELEASE_ID`, inspect
   `config diff PLAN_ID`, then activate through the operator's lifecycle door
   `host activate PLAN_ID --install-directory INSTALL_DIR`. Record the exact
   activation UTC instant and selected release. Do not activate during the
   baseline window or count activation downtime as healthy observation time.
3. **Exercise the live doors:** have the canary manager admit and assign one
   task. From the worker's live Bash tool, accept it with request UUID R1.
   `task show TASK` and `plane-lookup.py --root ROOT --by-assignment ASG
   --any-state` must expose the same `session_uid`. Independently read the
   session ID from clauDNA's per-bot store using `session show SID --json` and
   compare with `derive_session_uid(SID)`; reading the env twice is circular.
   After `/clear`, repeat acceptance with R1: replay true, same event and first
   session uid. Repeat the pair for `fleet reports submit` with R2. An Agent
   subagent runs `assignment progress` with R3 and records the same session uid
   as its parent (C11); the universal child marker is never the reference.
   Preserve receipt format 1 and the first attempt's fact projections.
4. **After activation:** record `AFTER_START` and `AFTER_END` and observe the
   candidate for another full 48 hours at the same cadence, retaining the same
   outputs and logs. Require at least 172800 seconds and disclose gaps. Compare
   counts by bot/cause and pulse/heartbeat verdicts against the before window.
   Pass only with no new restart cause and no new `activity_stuck` or busy-verdict
   change attributable to a door. Investigate differences before claiming pass.
5. **Rollback preview:** with both releases sealed in this canary root, run
   `claudlobby --root ROOT migration plan --source-release CANDIDATE
   --target-release HALF_A` and repeat toward the pre-P1 release. Neither may
   list a `receipt_format` blocker. Preserve both outputs. The fail-safe conflict
   after rollback is covered by the request-facts tests; do not resend real work
   to demonstrate it.
6. Record both full windows and the independent live-session comparison in the
   epic run log, cite them in the PR, then reap only the independently named
   canary units/root through the documented teardown. A pending window, missing
   live session, or unavailable target release remains pending in the audit.

Use a new request UUID only for a new effect or the documented conflict remedy;
a changed caller session alone must replay the original request. A crashed
prepared but unrecorded attempt is attributed to its committing session. Tests
cover that interruption point without killing a live door between preparation
and commit.
