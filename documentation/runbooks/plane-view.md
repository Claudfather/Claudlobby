---
title: Operator plane (plane view)
description: Serving the Phase-4 read-only operator plane and fronting it with Tailscale Serve
---

# Operator plane — `claudlobby plane view`

The Phase-4 v1 UI (design walk 2026-08-28): a strictly read-only window over
the plane db — the story-first channel, attention queue, tasks, fleet roster,
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
