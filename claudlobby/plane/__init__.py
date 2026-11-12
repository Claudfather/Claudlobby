"""The observable plane's write substrate (design v2, forks F1-F18).

Append-only event kernel: canonical serialization, minted identity, one
transactional ingest path, filesystem spool. No UI here — the daemon (Phase 4)
and the doors (Phase 2) are consumers of this package, never part of it.
"""

from __future__ import annotations

# Envelope readability is independent of SQL migration state. The version
# owner lists only formats with an implemented decoder, not an assumed N-1.
from ..runtime_versions import (
    PLANE_SCHEMA_VERSION,
    SUPPORTED_PLANE_SCHEMA_VERSIONS as SUPPORTED_SCHEMA_VERSIONS,
)
