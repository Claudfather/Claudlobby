---
title: Plane UI harvest audit — the sibling cockpit spec vs the operator plane
type: audit
status: draft
owner: chrisrogers37
created: 2026-08-31
repos: [Claudfather/Claudlobby]
---

# Plane UI harvest audit — the sibling cockpit spec vs the operator plane

## Summary

Two systems grew from the same substrate — a compositor, one tmux session per bot, the same supervision scripts — and built opposite UI layers on top. The sibling built a **cockpit**: a write-capable Gateway that classifies every pane every second, dispatches, runs a self-maintaining board, a decision queue, durable chat and schedules — with in-memory truth that resets on restart and no authentication inside the perimeter. Claudlobby built a **flight recorder**: an append-only ledger with minted identity, provenance on every envelope, typed panel states, capture policy, and a view that is read-only by construction.

The recorder is the moat and should stay. What it lacks is a **present tense**: the plane can say what happened but not what is happening. The grid answers "is the session up", never "is the bot working"; the attention rail knows dispatch trouble and deadlines, not a bot's question, a stale in-flight task, or a stopped fleet (#1361). Every high-ranked item below is a present-tense instrument, and all of them become cheap after one wiring job: recording the verdict `keepalive` already renders every minute as the metric samples the design reserved for it, so that *presence* — the derivation the design names as live, poller-fed and never a table — has both of its inputs.

What follows: the compare/contrast, a ranked punch list (10), what is deliberately not harvested and why, and five candidate directions for the next kindle round. Recommendation: spec **Play A — Present tense** first.

## Context

**Target (product-enhance step 1).**

- *Product:* `claudlobby plane view` — the Phase-4 v1 operator plane: story-first channel with fleet rooms and search, thumbnail grid + focus pane, attention + tasks rails, trust/gaps view, SSE live stream, read-only over the plane db.
- *Users:* the fleet operator (1–3 tailnet viewers). Bots do not use it; their read door is `brief`.
- *Success:* the ruled clocks (design v2 §17) — health/URL/next action in under 60 s for a returning operator; symptom → understood lifecycle in under 10 min — plus the story-first acceptance criterion (a schema-naive viewer can say what the fleet is doing) and §16's three questions: *what needs me now? what happened, and why? what changed or became untrustworthy?*
- *Access:* full code, design docs, and the live-preview feedback recorded in commit history. **No usage data** beyond the operator's own screenshot feedback ("machiney", the unlabeled title block, the unattributed header, the stuck overlay). Where friction is inferred rather than observed, it says so.

**The spec.** `<vault>/projects/agent-fleet-observability-ui-spec.md` (vault commit `f9a736b`, 2026-08-31): a clean-room, anonymized extraction of a sibling fleet system's observability/UI layer, every statement tagged required-vs-replaceable, with 20 verified defects and 10 unknowns. Its own audience line: *"deciding which mechanisms are worth harvesting into a sibling system."* This doc is that decision, UI-focused. The sibling's Supervisor and Monitor suites are near-verbatim this repo's `lib/` (the reconcile audit's four classes, edge-triggered credential checks, the cold-start CSV, the 30-day data purge), which is what makes the UI comparison clean: same floor, different house.

**Where the plane stands.** Main at `140f29a` (Phase 4 closed 2026-09-01); this branch `plane/phase2b-registry` adds the registry lane. Views channel / grid / trust; rails fleet, attention, tasks, machinery toggle; 5 of §16's 10 panel states shipped; ~1k production rows on the canary host.

## Observations — compare and contrast

### The two philosophies

| | Sibling cockpit (spec) | Claudlobby operator plane |
|---|---|---|
| Truth | Live pane scrape + an in-memory pane map and event ring (non-durable; sequence resets on restart) + a relational store + per-agent JSONL | Append-only SQLite ledger; every fact enters through a door emit with provenance, freshness and typed source state |
| Write posture | Write-capable Gateway: dispatch, chat send, decision resolve, schedules, personas, setup — unauthenticated inside the perimeter | Read-only by construction: no non-GET route (pinned), `mode=ro` + `query_only` on every connection; management verbs deferred to Phase 6, "equip first, through git" |
| Present tense | Busy / idle / dead per agent within ~1 s, activity sub-label, context-% — "the heart of the layer" (§5.1) | Grid: up / down / sampling (liveness only); fleet rail "live" = emitted within the last hour |
| Human action queue | Typed decisions (PR, issue, fork, plan approval, blockage, generic) with deep links, ack/resolve/dismiss, 5-min ack expiry, messenger mirror once | Attention = assignments with dispatch trouble or a passed deadline; no message-class awareness, no ack, no deep link |
| Conversation | Durable operator↔agent chat with unread counts, an anchored unread divider, mentions, attachments — independent of the messenger | The channel is what the doors sent (threads, delivery ladder, reports, closure); the operator's away-channel stays Telegram |
| Work | Self-maintaining board: six columns, auto-movers, PR fold, stale sweep, done-card GC, lane collapse, freshness tiers | Tasks rail: the 200 most recent assignments with a derived status ladder; workstreams exist in the db but not in the UI |
| Recurring work | Schedules engine with consent-gated trials, a run ledger, supersede stamping | Timers composed from `fleet.yaml` / `system.yaml`; not visible in the UI |
| Trust surface | A health endpoint (build id, uptime); no gaps/provenance surface; own documented gaps: no reconnect banner, no boot epoch, no WS resync | First-class: quarantine with reasons, spool age, per-door freshness, capture policy, provisional identities; every panel carries provenance; SSE resumes via `Last-Event-ID` |
| Privacy | Chat content only from explicit channels, never scraped (invariant) — but full pane text and credential-flow progress ride an unauthenticated WebSocket | Capture policy per fleet, alias-first, raw identifiers never on the wire, FTS only over permitted content with completeness disclosed; the grid shows raw terminals under an operators-only/tailnet ruling (#1390 pending) |
| Live transport | WebSocket, channel-multiplexed, no backpressure, no replay | SSE off the ingest cursor, head-start + resume |

Agreement worth naming: both hold *uncertain → do nothing* (the sibling's invariant; claudlobby's `source_state` rule is the stronger form), both keep chat content off the pane scrape, and both make the human decision an explicit act rather than a side effect of viewing.

### Where the plane is ahead

Typed panel states and provenance; unreachable ≠ empty; a resume-safe live stream; capture policy and alias-first presentation; a trust surface at all; fleet rooms; preserved selection and focus across re-render; read-only by construction. The sibling's Gateway is the "dashboard that can type into manager panes" class — fleet-wide RCE if the perimeter fails — that the observability research explicitly warned against.

### Where the sibling is ahead

The present tense (§5.1); a real human-action queue (§5.5); header stats in operator language (§8.10: idle workers, in progress, pending decisions); freshness tiers suppressed while the pane works (§8.10); durable operator conversation with unread (§5.6); recurring work with consent (§5.8). Setup, personas and tracker sync are also ahead — and out of the plane's lane (see *Not harvested*).

### A caution on the classifier

The sibling's activity sub-labels (thinking / editing / writing / reading / running) come from grepping the CLI's verb ladder. `lib/lib-common.sh` retired exactly that — "the churning verb lists silently degrade on UI changes and must not reappear" (gate: `tests/test_busy_ssot.py`) — in favour of one stable affordance (`esc to interrupt`) plus the `data/.last-tool-call` marker. Harvest the two-window *scoping* and the *debounce*, not the verb ladder as a truth source.

### One truth, two inputs (the split-truth question)

The sibling keeps two truths — an in-memory pane map and a store — and its own defects list is what that costs (§7 entities 10–11 vanish on restart; sequence numbers reset without an epoch). The plane's design forecloses it: **facts are recorded as `metric_samples`; presence is a derivation over the latest samples plus a live poll, and the derivation is deliberately never a table** (§9b: *"Presence now names the Lane C LIVE derivation — is-it-up-right-now, in-memory, poller-fed, never a table"*). Today only half of that exists. The view's sampler polls liveness live (`tmux capture-pane` return code, 5 s), while keepalive's busy/idle verdict — the other input, already computed every tick from the SSOT patterns — is written to the legacy JSONL and never reaches the plane. `metric_samples` (table, contract, ingest path) landed on `plane/phase2b-registry` in migration 0006 with `bot.heartbeat`, `bot.session_up` and `bot.pane_last_change_age_s` already seeded in the metric registry and no emitter yet. The earlier draft of item 1 would have classified inside the poller and emitted "later" — that is exactly the sibling's split, and it is withdrawn. The recorded half comes first: keepalive becomes a door. The poller keeps rendering pixels and alive/dead, and classifies nothing. One input stays unrecorded by design — the live poll — and that is not a second truth: if the poller dies, the page degrades to "last recorded heartbeat 3m ago", typed as stale.

### What OTel can and cannot replace

In plain terms: the plane records one fact — "this bot was active at this moment" — as a `bot.heartbeat` row, derives presence from the latest such row plus a live session poll, and renders that on the grid. The row shape, the derivation and the grid are built once and never care who wrote the row. Today the writer would be keepalive (marker age + the busy regex). Later the writer of that same row could be an adapter over OTel's event stream. Swapping the writer changes nothing downstream — that is what "the model stays, the source changes" means. What OTel can *not* do is replace the mechanism, because it is silent exactly when a bot is dead, wedged or stalled — the second input, "is the session alive", has to come from outside the process no matter what.

From the current Claude Code monitoring docs (read 2026-08-31): seven event types (`user_prompt`, `assistant_response`, `tool_result`, `api_request`, `api_error`, `tool_decision`, `permission_mode_changed`, all carrying `prompt.id`), six metrics (`session.count`, `cost.usage`, `token.usage`, `lines_of_code.count`, `code_edit_tool.decision`, `active_time.total`), logs exported every 5 s and metrics every 60 s by default, OTLP over `http/json` straight to a localhost endpoint with no collector, `OTEL_RESOURCE_ATTRIBUTES` honoured (so the composer can stamp `bot_instance_uid` / fleet onto everything a session emits), content gated behind opt-in flags (`OTEL_LOG_USER_PROMPTS`, `…ASSISTANT_RESPONSES`, `…TOOL_DETAILS`, `…TOOL_CONTENT`, `…RAW_API_BODIES`). **Undocumented:** the attributes on `api_error` (status / limit class), any idle heartbeat, any session-end or process-death event, hook visibility, and the `agent_id` / `workflow.run_id` attributes the design recorded as verified on 2026-08-18 — the pilot re-verifies those.

What that buys: the **inside half** of presence without touching tmux — an activity stream per bot (`tool_result` / `api_request` at 5 s freshness) that is the standardized form of today's `.last-tool-call` marker, plus tokens and cost for the usage lane, plus possibly the one thing neither a pane regex nor the hook payload can see (`bot-vitals.sh` says so in its header): an `api_error` at the instant a session stalls on a limit — #1361's class — *if* its attributes carry the class, which the docs do not say. What it cannot buy, by construction: the **outside half**. Nothing is emitted when a session is idle, dead, wedged or stopped mid-turn; the four states #1361 needs separated look identical from inside, because the signal is produced by the thing that failed. `bot.session_up` stays an outside observation (keepalive / the sampler's `has-session`) under any telemetry.

LangSmith is the other axis: intra-session traces (messages, tools, subagents, `thread_id` grouping), hosted, opt-in per fleet under §11. It answers *what did the bot do in that turn*, never *is it alive* — and polling a SaaS to learn whether a local process is up is the wrong direction. Phase-5 material (the why), not presence.

So: no rebuild. Item 1 opens one existing door. The Phase-3 OTel pilot (§12) then decides whether the OTel stream becomes the writer of the heartbeat's *busy* half — the ingest daemon would gain an OTLP/HTTP door, which fits its ingest-only tripwire — and keepalive keeps emitting `session_up` either way. The model stays; the source of one input changes.

## Gaps — the ranked punch list

Ranked by (impact × ease) / risk. Top three in full; the rest one line each.

### 1. Give the grid a present tense — as the presence derivation the design already names

- **Lenses:** wedge · coverage · differentiation
- **Severity:** high
- **Impact:** the founding surface ("watch my fleet work") cannot tell working from idle; #1361 documents five hours of a stopped estate reading as busy from every existing signal. Lands §17's 60-second "next action" clock and the grid half of the story-first criterion.
- **Effort:** hours to a day or two — **nothing new is classified.** `lib/keepalive.sh` already renders BUSY / IDLE / UNKNOWN for every bot on its tick (marker-first: `.last-tool-call` within 180 s; then the one busy regex; `pane_is_idle` for the prompt) and writes it only to the legacy JSONL. Make keepalive a door: emit `bot.heartbeat {state, marker_age_s}` and `bot.session_up` as `metric_samples` rows each tick — the table, contract and ingest path are on this branch (0006), the metric names are seeded, and §9b names keepalive as exactly this emitter at per-minute cadence (sized 30–45k rows/day, 30-day retention). Presence is then the Lane C derivation the design describes — latest heartbeat sample joined to the view sampler's live alive/dead poll — and the grid card, the header counts and the freshness suppression all read presence. Show `working · idle · down` with the marker age (#1361's "one column"); context-% only if a source ever carries it, never fabricated.
- **Risk:** the drift risk in the earlier draft is gone — there is no second classifier anywhere, and the sampler never classifies. What is real: busy/idle lags up to one keepalive tick (a cadence knob, not a design cost), and keepalive is not yet a door, so this is the first Phase 2b *emitter* (bash, the `plane_emit_events` pattern in lib-common, dormant behind `PLANE_EMIT_ENABLED` like the five doors). Reversible.
- **Open question:** heartbeat cadence — keep keepalive's tick or add a lighter one; and whether the Phase 3 OTel pilot supersedes the *busy* half of the heartbeat (see *What OTel can and cannot replace* below).

### 2. A header that speaks the operator's language

- **Lenses:** friction
- **Severity:** medium
- **Impact:** the header reads `ingest 12s · rows 1023 · spool 0` — machinery, the very thing the story-first ruling demoted — while the operator's first question ("is anyone stuck?") has no answer above the fold. Replace with `N working · N idle · N down · N need you` beside the recorder beat; rows and spool already live on the trust view.
- **Effort:** hours (`down` and `need you` today; `working` / `idle` once item 1 lands).
- **Risk:** none; one-commit reversal.

### 3. Widen the attention rail into a decision queue (read half)

- **Lenses:** coverage · wedge
- **Severity:** high
- **Impact:** §16 names failed dispatches, unacknowledged, overdue, orphaned *and broken emitters/spool* as the attention queue. Today the rail sees only assignments; the recorder gaps live on another tab; and a bot's `question` / `alert` to the operator — already typed in the ledger's message classes — is not in the queue at all. Add typed kinds with severity ordering and a deep link where one exists (a report carrying a PR URL → "ready for you"); a `blocked_waiting` task; an idle bot holding in-flight work (after item 1); recorder gaps. A client-local "seen" marker lets a glance clear the badge with no write route.
- **Effort:** days.
- **Risk:** alarm fatigue → typed kinds + the seen marker. Wording matters: Telegram replies are not recorded, so a question renders as "asked you 40m ago", never "unanswered". The write half — persisted ack/resolve attributed through the Tailscale identity header — is the first non-GET route and needs its own walk ruling; it is not this item.

4. **Freshness tiers on in-flight tasks** — quiet "idle 6h", amber at a day, red at three, suppressed while the assignee is working (§8.10) — friction — hours, after item 1.
5. **"New since you last looked" divider in the channel**, per room, client-local (§5.6's anchored divider, read side only) — friction — hours.
6. **A work view: workstreams → assignments, lane-collapsed** (not started / in progress / done), agent-grouped — the presentation half of #1394 — coverage — days.
7. **Mobile triage order** — on narrow screens the attention rail sits below the entire channel; put header counts, attention, then a grid strip first (§16: mobile is awareness/triage only) — friction — hours.
8. **A recurring panel, read-only** — briefings, sprint and sweep timers with last fire and outcome from ledger rows; the consent gate stays a management verb — coverage — days.
9. **Vocabulary pin** — a test that every task/transmission token in `KIND_MANIFEST` has a presentation label (the vanilla-era form of F2's generated types; today a new token renders raw) — cost of change — hours.
10. **Typed "recorder schema behind" state** — the view detects a db older than its expected schema and renders the remediation ("bounce the ingest daemon") instead of failing (the v4/v5 deploy gotcha) — cost of change — hours.

Cut: splitting `app.js` by view (real, low impact at three viewers). Filed elsewhere: the grid privacy knob (#1390); follow-up dispatches carrying the work item (#1394, half 1) — the emission-side fix that makes item 6 legible.

### Meta-pattern

The plane can say what happened; it cannot say what is happening. Items 1–4 and 7 are all the present tense — working now, needs me now, moved recently — and all of them read *presence*, the derivation the design already names (latest heartbeat sample + live session poll), which nobody has wired because keepalive's verdict still lands only in the legacy JSONL. Open that one door and the rest become derivations rather than features. The recorder stays the moat; the sibling's advantage was never its data model, it was the one-second present tense — and the plane gets a one-minute one for the price of a door.

## Risks

**Not harvested, and why**

- *Setup wizard* — the one-credential gate is right and already exists as `/setup`; durable config is declared in git (F4).
- *Persona composer, team create/delete* — writes `fleet.yaml`; that is Phase 6's equip verb, through git, not a form.
- *Tracker sync UI* — no tracker in the plane's lane; the spec's own warning is a second, ungated write path.
- *Docs browser* — low value; the vault is the corpus.
- *Operator composer / reply-from-page* — the write path; gate on authenticated emitters (F22) and the single-echo invariant; Phase 6.
- *Board auto-movers from pane signals* — F9: never infer task state from a pane or its text. Pane activity may be shown as a pane fact, never recorded as a task event.

**Sibling defects not to import:** memory-only decisions that vanish on restart; sequence numbers that reset without an epoch; full pane text over an unauthenticated socket; dispatch that blocks the event loop; four busy classifiers (`lib/` already consolidated to one — keep it so; item 1's mitigation is about exactly that).

**Cost of change in the plane today** (observations, not blockers): one 747-line `app.js` carrying four views with shared globals; hand-copied label maps; migrations ride the write path so a view-only deploy fails until the daemon bounces (item 10); the sampler is per-process; `view.py` does not consume `brief.py` although design §16 intended it, so "overdue" has two definitions during dual-write; `documentation/architecture/` does not yet describe the plane.

## Questions

For the kindle round, one at a time — the first is the ask at the end of this doc:

1. Which play first? (recommendation: A)
2. Inside A: what should the page open on — the channel (today), the grid, or a composite "now" page (counts + attention + a grid strip)?
3. Inside B: does the seen-marker stay client-local until the authenticated write path exists, or is the Tailscale identity header enough to persist it now?

## Directions for kindle (Product Vision lens)

| Play | Bundles | Why it compounds | Impact | Effort | Mission | |
|---|---|---|---|---|---|---|
| A. Present tense | keepalive → `bot.heartbeat` / `bot.session_up` samples; the presence derivation; items 1, 2, 4 | header counts, freshness suppression and "idle bot holding work" all read one derivation — recorded and live | High | Low–Med | Strong | 🟢 |
| B. One queue of human calls | items 3, 5, 7 → then the first write verb (ack/resolve, attributed) | the mobile triage story; the seam for the §11 reveal act and the first authenticated non-GET route | High | Med (read) / High (write) | Strong | 🟢 |
| C. Work, not messages | item 6 + #1394 + projects from the 2b registry | the mission lens — the north star's multi-workstream gap | Med–High | Med | Strong | 🟡 |
| D. Recurring, with consent | item 8 → the consent gate via git | filling idle time is north-star; the surface is secondary | Med | Med | Med | 🟡 |
| E. Talk back | composer → `dispatch.sh`, durable operator messages, unread | the sibling proves it is wanted; the highest-risk write | High | High | Strong | 🔴 until F22 |

The Phase-3 OTel pilot can later replace the writer of the heartbeat's busy half; it is not a prerequisite for A, and nothing built for A is discarded if it wins.

Deprecation candidates: the `rows` / `spool` / `ingest` header pills (duplicated on trust); `fleet-utilization.sh`'s keepalive-log parse once `bot.heartbeat` samples exist.

## Related

- `documentation/plans/2026-08-18-observable-plane-design-v2.md` §16–§18
- `documentation/runbooks/plane-view.md`
- Issues #1361, #1390, #1394

## Origin

Session 2026-08-31 — sibling-spec review + kindle / product-enhance pass on branch `plane/phase2b-registry` (main `140f29a`).
