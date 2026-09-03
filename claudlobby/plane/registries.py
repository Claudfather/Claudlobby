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
}


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
