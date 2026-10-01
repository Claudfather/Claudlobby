#!/bin/bash
# tests/plane_capture_cli.sh — batch renderer for hermetic emission-boundary
# fixtures. This is not a cold-CLI fallback and does not prove commitment.
# The fixture hands its captured batch file as the LAST
# argument; this renders every event in it as the LEGACY ledger row the file
# once held — {"ts","bot","type","source","data"} for a fleet event, the raw
# payload otherwise — and appends it to $PLANE_CAPTURE (one row per line).
# Exit 0: the shim reads that as "recorded". Runs under macOS /bin/bash 3.2.
f="${@: -1}"
[ -n "${PLANE_CAPTURE:-}" ] || { echo "plane_capture_cli: PLANE_CAPTURE unset" >&2; exit 4; }
[ -f "$f" ] || exit 0
python3 - "$f" >> "$PLANE_CAPTURE" <<'PY'
import json, sys
try:
    batch = json.load(open(sys.argv[1]))
except Exception as exc:
    print(json.dumps({"type": "capture_error", "error": str(exc)}))
    sys.exit(0)
for ev in batch.get("events", []):
    p = ev.get("payload") or {}
    if ev.get("event_type") == "system" and isinstance(p.get("data"), dict) and "legacy_ts" in p["data"]:
        d = p["data"]
        subj = p.get("subject") or ""
        kind = p.get("subject_kind")
        bot = "fleet" if kind == "fleet" else ("host" if kind == "host" else subj.rsplit("/", 1)[-1])
        row = {"ts": d.get("legacy_ts"), "bot": bot, "type": p.get("event"),
               "source": d.get("source"), "data": d.get("data") if isinstance(d.get("data"), dict) else {}}
    else:
        row = {"event_type": ev.get("event_type"), **p}
    print(json.dumps(row, separators=(",", ":")))
PY
exit 0
