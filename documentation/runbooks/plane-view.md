---
title: Operator plane (plane view)
description: Serving the Phase-4 read-only operator plane and fronting it with Tailscale Serve
---

# Operator plane — `claudlobby plane view`

The Phase-4 v1 UI (design walk 2026-08-28): a strictly read-only window over
the plane db — the story-first channel, attention queue, tasks, fleet roster,
header totals (bots · working · need you · overdue — summed by
`/api/overview` itself, and rendered with the disclosures the fleet cards
carry: the unconfirmed share of the bot count, a live poll that is degraded
or unavailable, and "no fleet recorded" rather than four zeros when the host
has recorded none), and an SSE live stream. It can observe everything and
touch nothing: no non-GET route exists (pinned by
`tests/test_plane_view.py`), and every db connection opens `mode=ro` with
`PRAGMA query_only`.

## Run it

```bash
python3 -m pip install -e '.[plane-ui]'   # FastAPI/uvicorn — part of the documented install
claudlobby plane view                     # binds 127.0.0.1:8899
claudlobby plane open                     # print/launch the URL (§17's open verb)
```

`/healthz` is a **data-freshness probe**: it answers 503 whenever the plane
db is absent or unreadable — including on a brand-new host where the
recorder simply has not written yet — so wire monitors accordingly. The
header's recorder pill is a live daemon PROBE (typed handshake), never
socket-file presence.

Supervised: **enrolled by default since chunk N** — `plane-view` composes
its units and `lib/setup-system` enrolls them, because a read-only localhost
UI reaches none of the four categories the defaults rule reserves for opt-in.
Exposing it beyond the host (Tailscale Serve, below) stays deliberately your
step. To turn it off, set `plane-view.enroll: false` under `host.jobs` in
**this host's own** system.yaml (compose-time dormancy, exactly like
`plane-daemon` — regenerating then prunes the units), then stop the installed
unit. Knobs: `PLANE_VIEW_PORT`; `PLANE_VIEW_HOST` is the raw-bind dev fallback
only.

**It needs the `[plane-ui]` extra, and the compositor checks.** Where fastapi
and uvicorn do not import in the install's venv, `generate` composes **no**
view unit at all and `claudlobby host doctor --switches` renders `plane-view` off
with `pip install -e '.[plane-ui]'` as its arm line. That is the fold's F1:
"the unit exits saying so" is an honest failure for a hand run and a **crash
loop every 5s, forever** under `Restart=always` — and enrolling by default is
what turns the first into the second. `lib/setup-system` installs the extra
(first install and upgrade both), so a host that followed the documented path
has it.

## Front it with Tailscale Serve (the ruled exposure)

One-time on the tailnet: admin console → DNS → enable MagicDNS + HTTPS.
Then on the host:

```bash
tailscale serve --bg --https=443 localhost:8899
claudlobby plane open                 # now prints the https://…ts.net URL
```

The daemon stays on localhost; Serve adds TLS, tailnet-only reachability,
and the viewer's tailnet identity (`Tailscale-User-Login`) — which is what
will attribute the §11 reveal act when that lands.

## Capture policy and the channel

The channel shows message BODIES for rows recorded under `full` capture,
which is the **shipped default** since 2026-09-20 — the channel is the
product, and under the previous `metadata` default every message rendered
"captured as metadata only (N bytes)" forever on an install nobody had
misconfigured.

To keep shapes without words, opt out in `state/plane/capture.json` — host
wide with `{"*": "metadata"}`, or per fleet with `{"noisy-fleet": "metadata"}`
(a named fleet beats `*`). The mode in force is visible in three places: the
`capture config` rung of `claudlobby plane doctor`, the `<mode> capture` label
on every fleet card, and the trust surface. A malformed file fails LOUD and
resolves to no mode at all, which matters more under a `full` default than it
did before: a silent fallback would store content an operator opted out of
keeping.

Either way the ledger is append-only, so **flipping capture starts words at
the flip and never retroactively** — and rows already recorded as metadata
cannot be recovered, because the body was dropped at the door.

Optional `state/plane/channels.json` maps raw carrier addresses to names
(`{"-100123": "Engineering group"}`) so a Telegram destination never renders
as a raw chat id.

## Two fleets on one host (U, #1467)

A host that runs more than one fleet gets the **fleet dimension**: a tab strip
(one tab per fleet the plane records, plus `all`), an overview strip above the
channel (one card per fleet, one host card), and every board scoped to the tab.

- **Tabs and the default.** `/api/fleets` lists every fleet from the registry's
  fleet identities (never the roster rail's last-seen window, so a quiet fleet
  keeps its tab). `default` is the fleet whose *room* moved most recently — a
  communication sent by the fleet or to it — and is the tab a first visit opens;
  the viewer's pick is remembered per browser (`localStorage` key `plane.fleet`).
  `_`-prefixed scope sentinels (the `_host` fleet the host probe emits under) are
  never fleets or participants.
- **One axis, every route.** `/api/tasks`, `/api/identities`, `/api/channel`,
  `/api/search`, `/api/grid`, `/api/presence`, `/api/inventory`, `/api/org` and
  `/api/utilization` take `?fleet=<name>`. A fleet's bots are the aliases in
  `bot:<fleet>/…` — one case-sensitive rule on every arm (`queries.fleet_alias_range`
  in SQL, `inventory.fleet_of` in Python), so a fleet named `en_` cannot absorb
  `eng`'s bots and `Eng` is not `eng`. `fleet=` (empty) and `fleet=all` are the
  host-wide read. A fleet the plane holds **no identity for** (while it holds
  others) answers a typed `unknown` state naming the fleets it does hold — the
  plane's own rule, never a healthy empty room; a plane holding no fleet yet lets
  the name through to the route's idle remedy (`generate`). The grid and presence
  routes also accept a fleet the sampler knows from disk before its first row.
- **The roster rail.** Inside a room, a flat list ordered by last-seen. Under
  `all` — the only read that spans fleets — the rail groups by fleet under a
  small header (the fleet's alias, its bot count) so the fleets do not
  interleave; recency still orders the rows inside a group and the groups
  themselves. Humans belong to every room and no fleet, so they get their own
  group, headed `humans` and counted `N humans`. No row is dropped by the
  grouping. WHICH fleet a row belongs to is stamped by the API (`fleet` on
  every identity row — `inventory.fleet_of`, the one Python spelling of the
  axis); the page parses no alias of its own.
- **Names.** Inside a room, bare names. Wherever two fleets meet — the `all` room,
  a cross-fleet thread in either room, an all-fleets inventory — every bot reads
  `fleet/name` (`inventory.qualified_labels`), so a twin (`erlich` on both fleets)
  and a unique name are both unambiguous. Each channel message carries
  `sender_fleet` / `recipient_fleet` read off the parties' own aliases (never the
  fleet the row was emitted under) and a `cross_fleet` mark.
- **The overview card.** Per fleet: `bots` (with the `provisional` part disclosed —
  actors no registry scan has confirmed; a mistyped dispatch target mints one),
  presence counts scoped to the room, `open` (**the matcher's rule**,
  `OPEN_ASSIGNMENTS_AT_SQL` per actor — the same count `claudlobby brief --bot`,
  fleet-pulse and `dispatch-overdue.py` show), `attention` and `overdue` (the
  attention queue's rows and its deadline arm), `orphaned` (the watchdog's
  `.spawn` split; `null` with a reason when the view's root holds no bot
  directories for the fleet), the newest report and the 24-hour report count,
  `last_activity_at` (ledger time) and the capture policy. The host card: recorder
  up/down, spool, rows, ingest lag with its state (`none` / `ok` / `warn` past
  120s, stamped by the API), and the host probe's newest facets (load, RAM, disk,
  thermal, under-voltage) — `null` until `plane-host-probe` has ever recorded.
  A figure whose source is absent is `null` with a reason, never `0`.
- **Unacked reports (chunk K).** A generated viewer runs
  `claudlobby --json fleet reports list --unacknowledged` and then
  `claudlobby fleet reports ack --through ACK_CURSOR --request-id UUID`, using the
  `ack_cursor` returned by the list. The ack records the viewer's read position
  as one `reports_acked` plane event on its actor; the event detail carries the
  `ingest_seq` reached. `claudlobby brief` remains a read-only view. A fleet's
  reports are ONE definition (`queries.FLEET_REPORTS_SQL`): report-class communications on its room
  axis, sent by the fleet or addressed to it. The card counts, through the same rule
  the brief lists (`plane-readers.unacked_rows`: terminal or status-less reports past
  the fleet's newest readable ack by any of its actors; a `progress` note never),
  "N unacked · acked by <bot> 2h ago"; a fleet that has never acked reads
  `no ack recorded` (`null` + reason), never a count of everything ever; a
  `reports_acked` row with no readable cursor is skipped, not a reset. The ack
  validates the served report prefix before recording and requires a committed
  plane fact; an uncertain recording must be inspected through its request UUID.

## The attention rail

`/api/tasks` shows one card per canonical fleet-owned Task, including queued
intake. Its task ID, state, and title come from the Task reducer; a current
assignment may add a deadline. The current assignment, prior assignments, and
message delivery evidence are shown separately: assignment or delivery status
is not task completion. Cards are not grouped by similar prose or dispatch
time. A queued Task has no current assignee and appears as fleet intake.

The rail shows cards with attention reasons, dated by recorded events or
deadlines. It can show a manager's escalation, failed or never-activated delivery,
overdue or stale work, a waiting blocker, or an unanswered nudge. Under metadata
capture a question may be withheld; the recorded attention reason remains.
Inspect the canonical Task with `claudlobby --json task show TASK_ID` and, when
a delivery message exists, its proof with
`claudlobby --json message receipt MESSAGE_ID`. A manager can explicitly
withdraw or reassign open work through `claudlobby task withdraw` or
`claudlobby task reassign`, using each command's required reason and fresh
request UUID. **The page itself is read-only.**

The attention badge counts Task cards needing attention, not assignment rows
or inferred broadcasts. `/api/tasks` selects the newest 200 Tasks in the
selected fleet room (or across fleets in the host view), so the badge and cards
describe that displayed window. `truncated` warns that older work may still
need attention. `issue_count` and the labeled `issues` disclose unresolved
Task history; at most the first 50 issues are shown, and `issue_scope` says
whether they cover displayed Tasks or the selected fleet. A zero card count
does not establish that older work or unresolved history is clear.

## The grid shows raw terminals — operators only (ruling 2026-08-29)

The thumbnail grid and focus pane render each bot's LIVE terminal verbatim
(`tmux capture-pane`), **ungoverned by the message-capture privacy policy**.
Anything on a bot's screen — a token echoed by a tool, a `git remote` URL,
vault content, PII, an in-flight credential — is visible to every tailnet
peer of the page. This is deliberate: it is the founding "watch my fleet
work" capability, and the trust boundary is the same as an operator running
`tmux attach` after SSHing to the host — your fleet, your tailnet, your
terminals.

Posture, therefore: **the plane view is operators-only and tailnet-scoped.**
Do not expose it beyond the tailnet, and treat viewer access as equivalent
to shell access to the host. The channel's per-fleet `capture.json` policy
does **not** apply to the grid; a metadata-only fleet's full terminal is
still visible there. A per-bot grid opt-out / secret-redaction knob is
tracked as a follow-up — until it lands, the grid is all-or-nothing per host
(gate the whole view daemon if any bot must never be shown).
