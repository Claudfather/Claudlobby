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
}
