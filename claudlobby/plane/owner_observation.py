"""Pure observations over existing owner facts, never admission or repair.

Internal versioning does not extend the legacy HTTP/CLI schemas. Adapters retain
those shapes; a local pairing observation never claims a live session or read.
"""
from dataclasses import dataclass, field
from typing import Literal

from .owner_access import OwnerGrant
from .owner_source import SourceDenied, SourceNeedsBinding, SourceUnavailable


def owner_pairing_state(owner: OwnerGrant | None) -> Literal['unpaired', 'paired', 'revoked']:
    return 'unpaired' if owner is None else 'paired' if owner.active else 'revoked'


def owner_read_profile(host_uid: str) -> dict:
    """The unchanged strict profile, only after the caller's fresh admission."""
    return {'version': 1, 'profile': 'direct-owner-read-v1', 'host_uid': host_uid}


@dataclass(frozen=True)
class SourceRefusal:
    state: Literal['unavailable', 'denied']
    reason: Literal['source_binding_required', 'source_denied', 'source_unavailable']
    next_operation: Literal['bind_source', 'inspect_source']
    version: int = field(default=1, init=False)


def source_refusal(error: SourceUnavailable | SourceDenied) -> SourceRefusal:
    """Classify typed source failures without treating them as pairing failures."""
    if isinstance(error, SourceNeedsBinding):
        return SourceRefusal('unavailable', 'source_binding_required', 'bind_source')
    if isinstance(error, SourceDenied):
        return SourceRefusal('denied', 'source_denied', 'inspect_source')
    if isinstance(error, SourceUnavailable):
        return SourceRefusal('unavailable', 'source_unavailable', 'inspect_source')
    raise TypeError('typed owner source failure required')
