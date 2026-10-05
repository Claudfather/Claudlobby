---
title: "Review handoff — runtime-neutral observability (#2145): forge, two ironclad cycles, rulings applied"
type: handoff
status: ready for implementation (forks F1–F18 locked; no holds)
owner: operator
created: 2026-10-05
epic: 2026-10-04-runtime-neutral-observability-plan.md
issue: Claudfather/Claudlobby#2145
pr: Claudfather/Claudlobby#2144
---

# Review handoff for the implementing session

The plan is back. Every fork is locked, every amendment the review raised is ruled, and the rulings are folded into the
epic and the six per-PR plans. Everything below is on branch `claude/affectionate-archimedes-2akwbf` (PR #2144). Nothing
has merged; the review merged nothing by design.

## 1. What happened

| Commit | What it is |
|---|---|
| `c5b40181` | Forge pass: the six missing specs written into the epic, six per-PR plans for P1–P3, §14 open questions. |
| `5df2429b` | Ironclad cycle 1 fold (7 lenses; [comment](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-5987380718)): facts corrected; nine fork amendments and two decisions recorded in epic §16 for the operator. |
| `a1deeacc` | Interim fold: PR packaging honours the holds; one spelling per name. |
| `74e40fce` | Ironclad cycle 2 fold (7 lenses on Opus; [comment](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-5999082732), [correction](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-5999138836)): no plan-fixable Blocker left; D1/D2 gates stated; F18 recorded; D2 wording made ratifiable; F14(a)'s seal on the order-3 PR; drift gate rewritten. |
| `c26c5b99` | **Rulings applied.** The operator ruled all eleven items on 2026-10-05; the fold removed every `Held:` line and every "if ratified" branch and wrote the ratified amendments into their forks. This handoff landed in the same commit. |

Lock record on #2144 (fork lifecycle protocol): [F18](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000050806) ·
[F17](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051065) · [F4](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051327) ·
[F3](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051564) · [F5](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000051858) ·
[F1, F2, F6–F16 and D2](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137).

## 2. The rulings (2026-10-05)

| Item | Ruling | What it means for implementation |
|---|---|---|
| F18 (D1) | **(a)** | Claudlobby composes agent CLIs: Claude Code today, Codex through #2149, a further CLI by its own fork. The mission text lands in Half A (plan 2 Task 5 Step 2). |
| D2 | ratified | clauDNA's mission amendment as worded in plan 3 Task 5 Step 2b; the plan 3 PR carries it. |
| F17 → **(c)** | A-F17 ratified | `session_uid` is left out of the hashed receipt projection (null-normalized in `expected_fact`/`reconcile_facts`). No `RequestIntent` change, no receipt format bump, no decoder-first two-release sequence, no rollback floor. Half B needs no release gate. |
| F4 → **(c) amended** | A-F4 + A-F4b ratified | **No OpenTelemetry Collector.** Bots export OTLP/HTTP-JSON straight to the host's opt-in `plane-otel` intake, which writes the rotated raw files and applies the allowlist. "Telemetry enabled, intake not enrolled" is a warning plus a doctor rung, not a refusal. **C6a is the confirmation gate**: if the direct export fails on the floor host, F4 reopens. |
| F3 **(b)** sequenced | A-F3 ratified | P2-a splits: the raw-file PR (P2-a1) first; the plane leg lands with its first reader's repoint (P2-a2: `usage_read`/`brief_read` → `session.tokens`, `session.api_requests`). |
| F5 → **(a) narrowed** | A-F5 ratified | Semconv names are the intake's internal vendor→house mapping; the plane's registry names are the reader surface; the Claudron register names no semconv attribute. |
| F1 (c) | A-F1 declined | The per-act join stands; the receiver-hook variant is not built. |
| F6 (b) | A-F6 declined | No transcript pointer in the export item; clauDNA's summary carries what the consumers read. |
| F10 (a) | A-F10 declined | `claude -p` summarizes every runtime; §14 Q10 is the revisit trigger, with P4. |
| F14 (a) | A-F14 lapsed | Per-bot roots stand; the seal rides the order-3 PR; the rename/removal runbook runs after the retention window. |

## 3. The documents

All in `documentation/plans/`:

| Doc | File | Carries |
|---|---|---|
| Epic | `2026-10-04-runtime-neutral-observability-plan.md` | §3 forks (all locked, with Evidence links), §5.1 canary waits, §6 phase specs, §8 release gates, §10 PR order + §10.1 index, §14 questions, §15 change log, §16 the ruled amendment records |
| Plan 1 | `…-p1-claudron-boundary-spec.md` | Claudron boundary-spec amendment (register rows 10/12, §10.2 with house names only) |
| Plan 2 | `…-p1-claudlobby-runtime-and-join-key.md` | Half A: `BotConfig.runtime`, `ids.session_alias`/`derive_session_uid`, the fleet-event row helper, the C10 scrub, the F18 mission text. Half B: null-normalized receipts, the doors write the uid, `_SESSION_ID_ENV` |
| Plan 3 | `…-p1-claudna-runtime-at-open.md` | clauDNA records `runtime` at session open → 0.27.0; the D2 mission text |
| Plan 4 | `…-p2-otel-pipeline.md` | P2-b (marker-only `bot-vitals.sh`, #874 fix); P2-a1 (telemetry config, intake receiver, raw writer, rung); P2-a2 (mapping → plane rows, the two events, the reader repoint) |
| Plan 5 | `…-p3-claudna-export-contract.md` | `claudna.export/1` (open schema), `entrypoint.json`, `--include-skipped` → 0.28.0 |
| Plan 6 | `…-p3-claudlobby-summaries.md` | order-3 per-bot `CLAUDNA_STATE_DIR` PR with the seal; 6b (`session-export`, `session_summary` rows, digest/SessionStart retirement, the F14 runbook); 6a (Claudron pin bump) |

No plan carries a `Held:` line any more.

## 4. Order of work (epic §10)

1. **P0, the Claude-only canary batch**: C6a (F4's confirmation gate — the direct OTLP/HTTP-JSON export works on the
   floor host; exporter facts), C10 (the child-shell env leak that decides Task 7b), C11 (the main-thread uid equality).
   Scripts and the run log are the review's Step 3 and are **not written yet**.
2. **P2-b** (plan 4 Tasks 1–2). Cut from `origin/main`; whichever of #2140 and P2-b lands second rebases. Its evidence is
   the before-leg `tool_call` rows and the transcripts' `tool_use` ids. 48 h canary-root window. Record the §14 Q9 answer
   before merge.
3. **The per-bot `CLAUDNA_STATE_DIR` PR** (plan 6 Task 1 Steps 1–2). Activation seals the old root's bot sessions: per
   bot `list --bot <b> --json --limit 100000 --include-private --root ~/.claudna`, `seal <sid>` for each open one, then
   `CLAUDNA_UNCLOSED_AFTER_H=1 … sweep --root ~/.claudna` until it closes nothing. Never `seal` "every open session": that
   closes live interactive sessions in the same root. Canary-root check before merge.
4. **The Claudron boundary-spec PR** (plan 1), Codex names plain.
5. **P1 Claudlobby Half A** (plan 2, release N), carrying the F18 mission text.
6. **clauDNA runtime-at-open** (plan 3) → 0.27.0, carrying the D2 text.
7. **P2-a1** (plan 4) in parallel with **clauDNA 0.28** (plan 5) → **6b** (plan 6).
8. **P2-a2** (after P2-a1's canary week) in parallel with **6a**.
9. **P1 Claudlobby Half B** (plan 2, Tasks 6–9 plus docs and the door observation), after Half A has merged, C11 is in
   the run log and, if C10 leaked, Task 7b's scrub is live on every bot; no release gate.
10. **P0, the Codex batch**: install → step 0 (`[otel] metrics_exporter = "none"`, `[analytics] enabled = false`) →
    C1–C5, C7–C9.
11. **P4** per repo.
12. Companions (#2149, clauDNA#404).

## 5. Facts the review established that implementation must respect

- **Receipts.** Nothing changes format. `expected_fact` and `reconcile_facts` (`request_facts.py:27-51,106`) hash
  `session_uid`/`sender_session_uid` as null, so every existing hash is unchanged (their eight importers write neither
  column before Half B) and neither door family conflicts across the activation. A replay after a rollback of a request
  committed after the change fails at the proof of the already-committed row: `ReceiptConflict` from `_replayed` for the
  task doors (`task_operations.py:305-306`), `MessageConflict` from `_proof` for the message doors
  (`message_operations.py:133-134`); a prepared-but-unrecorded request retried after a rollback does not conflict. The
  doors write the uid into the fact rows (`ingest.py:162,256` columns exist, null today). General fact for any future
  receipt change: the release migration planner allows a write-version change only from 0 → 1 with implemented decoders
  (`migration_plan.py:450-457`); `host activate` refuses on any blocker. `host migrate` does not exist; the doors are
  `host activate` and `migration plan --source-release --target-release`.
- **Join key.** `derive_session_uid = derive_uid("sess", session_alias(id, runtime))`; `session_alias` is the raw id for
  `claude`, `"<runtime>:"+id` otherwise. Stated once (plan 2 Task 4; Claudron register row 10); every test's expected
  value is computed through the Python one-liner, never a bash `shasum` copy. The hook's `session_id` for comparisons
  comes from clauDNA's per-bot store (`session show <sid> --json`), never from `CLAUDE_CODE_SESSION_ID`.
- **One fleet-event row helper**: `claudlobby/plane/fleet_events.py` `fleet_event_request(event_type, *, fleet,
  subject_kind, subject, source, data, bot=None, occurred_at=None, observed_at=None, legacy_ts=None, event_id=None,
  key=None)` (plan 2 Task 9b, Half A). No hand-built row anywhere else. The intake's no-bot fallback is
  `subject_kind="session", subject=session_alias(id, runtime)`.
- **Transport rule, stated once (epic §6 P2):** resident services → `daemon.send_batch`; bash hooks and timer scripts →
  the `plane-emit.sh` shim; Python CLI doors and one-shot Python ticks → `emit_batch` in-process.
- **Registry gate.** Any Python module that builds a `"subject_kind":` dict must be in `PY_WRITERS` and pass literal
  event types (`otel_intake.py`, `session_export.py`).
- **The intake** (`plane-otel.sh --port 4319 --retention-days 14`, a resident host service) answers 200 before
  forwarding, drops content-bearing attributes for non-`full` bots before the raw write, writes the raw rotated
  `state/otel/*.jsonl` (bound ≈ 1.92 GB, 0700 directory created before it binds, `UMask=0077`, the `otel sink` rung in
  `plane doctor`), and maps with one dict (the F5 artefact). Bot env block: six `export` lines for `content: metadata`,
  eight for `full`, with `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` and the endpoint `http://127.0.0.1:4319`. Switch rows:
  `telemetry` (per bot) and `plane-otel` (host service). The A-F4b warning key is `telemetry-intake`. No binary to
  install; P2 owes no cold-host onboarding run.
- **The first reader** (`usage_read`/`brief_read`) prints tokens, turns and models, so P2-a2 repoints it to
  `session.tokens` and `session.api_requests`. `session.tool_calls`, `active_time_s`, `api_error` and
  `tool_failure_streak` have no named reader yet; the allowlist is F3's ratified content.
- **`export_stalled` is `notice`** and pages through `fleet_notification.notify_fleet(level="alert", …)`; "stall reported"
  is recorded only after that emit lands. The `unexportable` fallback has its own event id. `harvest: true` with
  `session_summary: false` is refused at config load.
- **`claudna_version` pins nothing** (`plugin_ensure` runs `claude plugin update`); the guard against an early 0.28 is
  the order-3 per-bot root.
- **Codex telemetry defaults are hosted** (`metrics_exporter = statsig`); P0 step 0 turns them off before the first Codex
  canary.
- **Export-contract drift gate.** `conformance.yml:61-73` checks clauDNA out at its default branch and runs only the rename
  map; plan 6 Task 8 adds `contracts/claudna.ref`, vendors `lib/claudna/session_store/schemas/export.schema.json`
  (repo-relative) and an offline canned-envelope test. `claudna.export/1` is an open schema.
- **"F18" has two meanings** in Claudlobby docs (design v2's cutover fork and this epic's mission fork); the epic
  qualifies the former as "design-v2 F18". Do the same in new text.
- **Not filed yet:** the digest reader defect (`session_digest` rows invisible to `fleet_events`/`legacy_event_row`
  since #1503) has no GitHub issue. It is a §13 action; file it before 6b opens and record the number there.

## 6. Still open on the review side

- **Step 3 of the review brief**: canaries C1–C11 as small safe scripts (style of clauDNA's `scripts/session_canary.py`;
  log field names and sizes, never prompt content), exact instructions for the interactive steps, the run log
  `documentation/plans/2026-10-04-runtime-neutral-observability-run-log.md`. Not started. Codex is not installed on the
  review machine (Homebrew cask `codex` 0.157.1 is available).
- **The closing comment on #2145** (links to the per-PR plans and the run log; the amendment list) is not posted.
- The review's aggregator script drops nested sub-bullets from lens results; fix before any further ironclad cycle.

## 7. Working rules that still apply

- Forks are locked; a fork found wrong during implementation is reopened with `[FORK-REOPEN F<n>]` on #2144 and
  re-locked, never edited silently.
- No hosted export, no LangChain-family tooling; telemetry stays on the local box.
- Every GitHub comment ends with `\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_`.
- Each repo's `CLAUDE.md` rules: Claudlobby's Defaults rule and mandatory runtime validation; clauDNA's stdlib-only
  `lib/`, Python 3.9 floor and approval gates; Claudron's single-dependency rule and R1–R7.
