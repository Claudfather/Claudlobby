"""Claudron compatibility floor — machine-readable SSOT.

Maps each claudlobby integration surface to the minimum Claudron capability
it needs. ``claudlobby doctor``'s claudron check (``doctor.check_claudron``)
reads this table; ``documentation/integrations/claudron-integration.md`` is the
human-readable rendering and must be updated when this table changes (a unit
test asserts the doc stays in sync).

Claudron's release numbers are ordinal — their roadmap states version numbers
follow ship order if their Gate G1 re-orders epics — so entries key on
*capability*, with the default-order release recorded as an annotation, never
used as a trigger. ``probe`` is what makes that real: a row is met when the
capability answers, not when a version string compares. (``engine_version``
from Claudron's capability probe is reported by doctor as *health*, not as a
floor trigger.) The authoritative install pin itself lives in pyproject.toml's
``[vault]`` extra, not here — and it governs only the *imported* API; the host
CLI is installed out of band.

A ``parked`` row is a surface that is deliberately **not built** — demand-gated
by a recorded decision. Parked rows are never probed and never render "unmet";
warning about the absence of a door nobody shipped is the exact failure this
table's consumer was built to stop doing.
"""

from dataclasses import dataclass

#: Canonical any-agent integration front door — Claudron owns this text; every
#: claudlobby message that tells an operator how bots reach a vault cites it.
CLAUDRON_INTEGRATION_URL = (
    "https://github.com/Claudfather/Claudron/blob/main/docs/INTEGRATION.md"
)

#: ``probe`` vocabulary.
PROBE_NONE = ""  # no automatic probe (parked rows, or capability not probeable)
PROBE_API = "api"  # the ``[vault]`` import seam resolves (paths.vault_api_available)
PROBE_VERB_PREFIX = "verb:"  # ``claudron <verb> --help`` resolves on the host CLI


@dataclass(frozen=True)
class ClaudronCapability:
    feature: str  # the claudlobby surface that consumes it
    requires: str  # what claudron must ship (pure capability — no epic ordinals)
    default_order_release: str  # Claudron's slated release, or "pinned SHA"
    probe: str = PROBE_NONE  # how doctor decides met/unmet — see the vocabulary
    parked: str = ""  # non-empty ⇒ demand-gated; the text names the decision


COMPAT_FLOOR: tuple[ClaudronCapability, ...] = (
    ClaudronCapability(
        feature="vault-based fleet overlay resolution (paths.py .claudron bridge)",
        requires="claudron.vault.detect / Vault.fleets API",
        default_order_release="0.2.0",
        probe=PROBE_API,
    ),
    ClaudronCapability(
        feature="CLI query wedge (dispatch-task.sh preflight)",
        requires="claudron lookup CLI",
        default_order_release="0.2.0",
        probe=PROBE_VERB_PREFIX + "lookup",
    ),
    ClaudronCapability(
        feature="fleet session loop — engine hooks installed per bot (plan L2)",
        requires="claudron hooks install + per-event hook dispatch",
        default_order_release="0.2.0",
        probe=PROBE_VERB_PREFIX + "hooks",
    ),
    ClaudronCapability(
        feature="MCP fragment library/mcp/claudron.json",
        requires="claudron-mcp stdio server",
        default_order_release="unbuilt — demand-gated",
        parked="decision C",
    ),
)
