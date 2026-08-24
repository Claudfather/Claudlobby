"""The observable plane's write substrate (design v2, forks F1-F18).

Append-only event kernel: canonical serialization, minted identity, one
transactional ingest path, filesystem spool. No UI here — the daemon (Phase 4)
and the doors (Phase 2) are consumers of this package, never part of it.
"""

from __future__ import annotations

# Version of the envelope + DDL contract this checkout writes. Bump per
# schema-changing migration; readers accept N and N-1 (spec §15).
PLANE_SCHEMA_VERSION = "1.0.0"
