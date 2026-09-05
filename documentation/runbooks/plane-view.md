---
title: Operator plane (plane view)
description: Serving the Phase-4 read-only operator plane and fronting it with Tailscale Serve
---

# Operator plane — `claudlobby plane view`

The Phase-4 v1 UI (design walk 2026-08-28): a strictly read-only window over
the plane db — the story-first channel, attention queue, tasks, fleet roster,
header totals (bots · working · need you · overdue, from the overview),
and an SSE live stream. It can observe everything and touch nothing: no
non-GET route exists (pinned by `tests/test_plane_view.py`), and every db
connection opens `mode=ro` with `PRAGMA query_only`.

## Run it

```bash
pip install -e '.[plane-ui]'          # FastAPI/uvicorn — optional extra
claudlobby plane view                 # binds 127.0.0.1:8899
claudlobby plane open                 # print/launch the URL (§17's open verb)
```

`/healthz` is a **data-freshness probe**: it answers 503 whenever the plane
db is absent or unreadable — including on a brand-new host where the
recorder simply has not written yet — so wire monitors accordingly. The
header's recorder pill is a live daemon PROBE (typed handshake), never
socket-file presence.

Supervised: arm `plane-view.enroll: true` under `host.jobs` in the HOST's
system.yaml (compose-time dormancy, exactly like `plane-daemon`), regenerate,
enroll. Knobs: `PLANE_VIEW_PORT`; `PLANE_VIEW_HOST` is the raw-bind dev
fallback only.

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

The channel shows message BODIES only for rows recorded under `full`
capture (`state/plane/capture.json`, e.g. `{"*": "full"}` — the F7/F23
knob). Rows recorded under the default `metadata` policy render as
"captured as metadata only (N bytes)" forever — the ledger is append-only;
flipping capture starts words at the flip, never retroactively.

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
- **Unacked reports (chunk K).** `claudlobby brief --ack` records the viewer's read
  position as a plane fact — one `reports_acked` system event on the manager's actor,
  its detail the `ingest_seq` the ack reaches — through the cold emit door (so `--ack`
  is the one brief door that writes, and it runs `migrate()`). A fleet's reports are ONE
  definition (`queries.FLEET_REPORTS_SQL`): report-class communications on its room
  axis, sent by the fleet or addressed to it. The card counts, through the same rule
  the brief lists (`plane-readers.unacked_rows`: terminal or status-less reports past
  the fleet's newest readable ack by any of its actors; a `progress` note never),
  "N unacked · acked by <bot> 2h ago"; a fleet that has never acked reads
  `no ack recorded` (`null` + reason), never a count of everything ever; a
  `reports_acked` row with no readable cursor is skipped, not a reset. No cursor file
  exists any more: a failed emit is a failed ack (rc 1, said on stderr), a spooled one
  is disclosed and takes effect when the spool drains, and `PLANE_EMIT_DISABLED=1`
  refuses to ack.

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
