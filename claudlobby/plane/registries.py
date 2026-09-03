"""Package-owned registries (design §9b census + §11 field policy).

Phase 1 ships FIELD_POLICY (the classification registry — the ENFORCEMENT
source of truth: contracts read caps from here, the capture door reads
CONTENT membership from here; editing a cap HERE changes behavior).
SYSTEM_EVENT_TYPES and METRIC_NAMES join in Phase 2b.
"""

from __future__ import annotations

# (family, field) -> {class: CONTENT|SENSITIVE|DIAGNOSTIC|METADATA,
#                     cap: bytes, proof: keep sha/bytes triple on drop}
FIELD_POLICY: dict[tuple[str, str], dict] = {
    ("communication", "body"): {"class": "CONTENT", "cap": 16_384, "proof": True},
    ("communication", "recipient_raw"): {"class": "SENSITIVE"},
    ("work_item", "body"): {"class": "CONTENT", "cap": 16_384},
    ("task", "summary"): {"class": "CONTENT", "cap": 4_096},
    ("workstream_event", "note"): {"class": "CONTENT", "cap": 4_096},
    ("workstream_event", "next_step"): {"class": "CONTENT", "cap": 4_096},
    ("transmission", "destination"): {"class": "SENSITIVE"},   # rides detail
    ("system", "data"): {"class": "DIAGNOSTIC", "cap": 16_384},
}

CONTENT_FIELDS: dict[str, tuple[str, ...]] = {}
for (_family, _field), _pol in FIELD_POLICY.items():
    if _pol["class"] == "CONTENT":
        CONTENT_FIELDS[_family] = CONTENT_FIELDS.get(_family, ()) + (_field,)


def cap_for(family: str, field: str) -> int:
    return FIELD_POLICY[(family, field)]["cap"]


# kind=system severity is REGISTRY-OWNED (§9b: "ingest stamps it from the
# package-owned seed module; callers cannot set it; unknown type => null").
# A caller-supplied severity is a caller bug (ContractViolation via the strict
# wire model). Phase 2b grows this seed into the full SYSTEM_EVENT_TYPES
# registry (F19: unknown tokens still INGEST — they just carry NULL severity
# until the registry learns them).
SYSTEM_EVENT_SEVERITY: dict[str, str] = {
    "daemon_started": "notice",
    "daemon_stopping": "notice",
    "spool_drain_completed": "notice",
    # cutover chunk 3 — the shadow primitive's record (J4): a clean
    # comparison is routine; a divergence is the flip's stop signal.
    "shadow_parity_clean": "notice",
    "shadow_parity_diverged": "critical",
    # cutover chunk 5 — the epoch, recorded when it happens: a reader flipped
    # to the plane (with the gate evidence, or a --force reason) is a fact the
    # ledger must carry; the flag alone is invisible history.
    "cutover_declared": "notice",
    # cutover chunk 6b — the legacy writes retired: the END of shadowing for
    # the ledgers those writes fed, a fact the ledger must carry.
    "legacy_write_retired": "notice",
    # cutover chunk 7a — a report whose status reached no task event (a terminal
    # note that resolved nothing): the status the idle-worker check reads.
    "report_status": "notice",
    # cutover Phase B — the bot-events ledger (data/events/fleet-*.jsonl) on the
    # plane: every `emit_fleet_event` type the estate emits, registered with the
    # severity `claudlobby events`' CRITICAL_TYPES implies (critical pages the
    # operator through fleet-pulse's escalation; notice is the record). An
    # unregistered type still ingests with NULL severity (F19).
    "session_missing": "critical",
    "service_down": "critical",
    "activity_stuck": "critical",
    "script_error": "critical",
    "overdue_dispatch": "critical",
    "bridge_down": "critical",
    "reload_failed": "critical",
    "restart_failed": "critical",
    "rc_timeout": "critical",
    "alert_delivery_failed": "notice",
    "dispatch_orphaned": "notice",
    "worker_unassigned": "notice",
    "pane_stuck": "notice",
    "wip_uncommitted": "notice",
    "send_miss": "notice",
    "send_retry": "notice",
    "send_blind": "notice",
    "send_blind_recovered": "notice",
    "resume_skipped": "notice",
    "plugin_marketplace_failed": "notice",
    "briefing_deferred": "notice",
    "briefing_dispatched": "notice",
    "briefing_failed": "notice",
    "audit_selected": "notice",
    "audit_dispatched": "notice",
    "audit_deferred": "notice",
    "audit_failed": "notice",
    "sweep_repo_unreachable": "notice",
    "bot_teardown_started": "notice",
    "handoff_skipped": "notice",
    "fleet_rescue": "notice",
}

# The chatty types (Phase B, J-B5): high-volume, machinery-only — the retention
# lane may age these past the incident-join window; a critical type is never
# aged (the `metric_samples` precedent: never the ledger, only a family's rows).
CHATTY_SYSTEM_EVENTS: frozenset[str] = frozenset({
    "send_miss", "send_retry", "send_blind", "send_blind_recovered", "script_error",
    "shadow_parity_clean", "pane_stuck",
})


# ---------------------------------------------------------------------------
# Phase 2b: the metric-name registry (§9b MetricSample — open registry,
# warn-on-unknown at ingest, additions by PR). Units live HERE, never on
# rows. Seed = the spec's walked list (§9b Emitters paragraph).
# ---------------------------------------------------------------------------

METRIC_NAMES: dict[str, dict] = {
    "host.load": {"unit": "load", "description": "1/5/15-min load triplet"},
    "host.mem_available_mb": {"unit": "MB", "description": "available RAM"},
    "host.disk_free_gb": {"unit": "GB", "description": "free disk"},
    "host.thermal_flags": {"unit": "flags", "description": "Pi vcgencmd thermal"},
    "host.undervoltage": {"unit": "bool", "description": "Pi undervoltage flag"},
    "host.boot_time": {"unit": "iso8601", "description": "last boot instant"},
    "host.job_ran": {"unit": "run", "description": "one sample per machinery run"},
    "vault.behind": {"unit": "commits", "description": "behind upstream"},
    "vault.ahead": {"unit": "commits", "description": "ahead of upstream"},
    "vault.last_fetch_age_s": {"unit": "s", "description": "age of last fetch"},
    "vault.fetch_failed": {"unit": "bool",
                           "description": "a failed fetch must never render"
                                          " as up-to-date"},
    "bot.session_up": {"unit": "bool", "description": "tmux session alive"},
    "bot.bridge_up": {"unit": "bool", "description": "telegram poller alive"},
    "bot.rc_ok": {"unit": "bool", "description": "remote control live"},
    "bot.pane_last_change_age_s": {"unit": "s", "description": "pane activity age"},
    "bot.heartbeat": {"unit": "run", "description": "keepalive heartbeat"},
    "bot.rss_mb": {"unit": "MB", "description": "resident set size"},
    "env.key_state": {"unit": "state",
                      "description": "creds-check key state (names never"
                                     " values; the #1213 present-but-empty"
                                     " class)"},
}
