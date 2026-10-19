"""One Python spelling of the fleet-event row bash `emit_fleet_event` writes
(lib-common.sh emit_fleet_event; parity-pinned by tests/test_plane_fleet_events.py).

Readers select these rows by the `fleet-events:` source_ref prefix and read
payload.data.{source,legacy_ts,data} (plane-readers.py FLEET_EVENTS_PREFIX /
legacy_event_row), so every Python writer builds exactly this row through
`fleet_event_request` (#2145). `event_id` carries a writer's dedup key and
`fleet-events:sha:<hex>` is the only provenance form: no writer adds a
`fleet-events:<name>:…` sub-grammar.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .ids import derive_hex, mint_event_id


def fleet_event_request(event_type: str, *, fleet: str, subject_kind: str, subject: str, source: str,
                        data: dict, bot: str | None = None, occurred_at: str | None = None,
                        observed_at: str | None = None, legacy_ts: str | None = None,
                        event_id: str | None = None, key: str | None = None) -> dict:
    """The row as bash composes it, ready for `emit_batch`.

    `source_ref` is "fleet-events:sha:" + derive_hex(key), where `key` defaults to
    the legacy ledger line {ts, bot, type, source, data} — the content key the
    retired file ledgers used, composed as `emit_fleet_event` composes it; a caller
    with its own idempotency material (a request id) passes it. `bot` is the
    legacy line's bot (bash: the bot id, `fleet` or `host`), derived from the
    anchor when omitted. `observed_at` is the envelope's
    reporter-of-another-system's-fact slot (contracts.EmitRequest); bash never sets
    it, so the key is written only when given and the parity row is unchanged.
    `occurred_at` defaults to now in UTC, in the form bash stamps."""
    now = occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts = legacy_ts or now
    if not key:  # the content key, built only when the caller brought none
        who = bot or ("fleet" if subject_kind == "fleet" else "host" if subject_kind == "host"
                      else subject.rsplit("/", 1)[-1])
        compact = json.dumps(data, separators=(",", ":"))
        key = f'{{"ts":"{ts}","bot":"{who}","type":"{event_type}","source":"{source}","data":{compact}}}'
    row = {"event_id": event_id or mint_event_id(), "event_type": "system", "emitter": source,
           "source_ref": "fleet-events:sha:" + derive_hex(key), "fleet": fleet,
           "occurred_at": now,
           "payload": {"event": event_type, "subject_kind": subject_kind, "subject": subject,
                       "data": {"source": source, "legacy_ts": ts, "data": data}}}
    if observed_at is not None:
        row["observed_at"] = observed_at
    return row
