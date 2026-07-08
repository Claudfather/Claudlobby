"""Claudron compatibility floor — machine-readable SSOT.

Maps each claudlobby integration surface to the minimum Claudron capability
it needs. ``claudlobby doctor``'s claudron check (plan P2d, issue #511) reads
this table; ``documentation/integrations/claudron-integration.md`` is the
human-readable rendering and must be updated when this table changes (a unit
test asserts the doc stays in sync).

Claudron's release numbers are ordinal — their roadmap states version numbers
follow ship order if their Gate G1 re-orders epics — so entries key on
*capability*, with the default-order release recorded as an annotation, never
used as a trigger. The authoritative install pin itself lives in
pyproject.toml's ``[vault]`` extra, not here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudronCapability:
    feature: str  # the claudlobby surface that consumes it
    requires: str  # what claudron must ship
    default_order_release: str  # Claudron's slated release, or "pinned SHA"
    shipped_at_pin: bool  # True when the pinned SHA already provides it


COMPAT_FLOOR: tuple[ClaudronCapability, ...] = (
    ClaudronCapability(
        feature="vault-based fleet overlay resolution (paths.py .claudron bridge)",
        requires="claudron.vault.detect / Vault.fleets API",
        default_order_release="pinned SHA",
        shipped_at_pin=True,
    ),
    ClaudronCapability(
        feature="interim CLI query wedge (dispatch-task.sh preflight, plan P1e)",
        requires="claudron lookup CLI",
        default_order_release="pinned SHA",
        shipped_at_pin=True,
    ),
    ClaudronCapability(
        feature="MCP fragment library/mcp/claudron.json (plan P2)",
        requires="claudron-mcp stdio server (their E3)",
        default_order_release="0.3.0",
        shipped_at_pin=False,
    ),
    ClaudronCapability(
        feature="librarian review sweep (plan P5)",
        requires="claudron review --json (their E5)",
        default_order_release="0.5.0",
        shipped_at_pin=False,
    ),
)
