"""Fixed internal action carrier selection; browser inputs never choose it."""
from dataclasses import asdict, dataclass
from pathlib import Path

from .owner_access import AccessDenied, OwnerAccess
from .workspace_assertion import _origin


@dataclass(frozen=True)
class ConnectedActionAuthorization:
    access: OwnerAccess
    issuer: str
    operational_origin: str

    def __post_init__(self):
        if type(self.access) is not OwnerAccess or not _origin(self.issuer, False) or not _origin(self.operational_origin, False):
            raise ValueError("explicit connected action authority required")

    @property
    def root(self):
        return self.access.root

    def _binding(self, admission):
        if admission.binding.issuer != self.issuer or admission.binding.operational_origin != self.operational_origin:
            raise AccessDenied("connected_session_denied")

    def authorize_read(self, token, principal, *, host_uid):
        admission = self.access.connected().authorize(token, principal,
            host_uid=host_uid, operational_origin=self.operational_origin)
        self._binding(admission)
        return admission.owner

    def _action(self, kind, token, principal, *, host_uid, fleet_uid):
        admission, grant = self.access.connected().authorize_action(kind, token, principal,
            host_uid=host_uid, fleet_uid=fleet_uid, operational_origin=self.operational_origin)
        self._binding(admission)
        return grant

    def authorize_message(self, token, principal, **target):
        return self._action("message", token, principal, **target)

    def authorize_nudge(self, token, principal, **target):
        return self._action("nudge", token, principal, **target)

    def authorize_feedback(self, token, principal, **target):
        return self._action("feedback", token, principal, **target)

    def scope_binding(self, reader, host_uid):
        admission = self.access.connected().authorize(reader.token, reader.principal,
            host_uid=host_uid, operational_origin=self.operational_origin)
        self._binding(admission)
        return {"carrier": "connected", "binding": asdict(admission.binding)}


def action_authorization(root, authorization=None):
    """Only the two core-owned policy owners may be injected by a factory."""
    if authorization is None:
        return OwnerAccess(root)
    if type(authorization) not in (OwnerAccess, ConnectedActionAuthorization) or authorization.root != Path(root):
        raise ValueError("action authority must own the selected root")
    return authorization


def scope_inputs(values, authorization, reader, host_uid):
    # Direct contexts retain their exact pre-existing hashes and wire shape.
    if type(authorization) is ConnectedActionAuthorization:
        return [*values, authorization.scope_binding(reader, host_uid)]
    return values
