"""Reusable local workspace consent: read-only preview, then presenter-gated apply.

This is an internal dependency, not an authorization channel. It authenticates
no caller: admission (operator context, a verified principal) stays with
whoever constructs the presenter, and a presenter object proves nothing about
who is in front of it. The storage transaction in ``owner_workspace`` remains
the only writer and keeps every defense it has; nothing here weakens or
repeats its replay, expiry or substitution checks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .owner_access import AccessDenied, OwnerAccess, OwnerGrant
from .owner_workspace import WorkspaceBinding, WorkspaceProposal


class WorkspaceConsentChanged(Exception):
    """The owner or the retained binding is not the one the caller expected.

    Deliberately not an AccessDenied: callers map storage refusals and
    expectation drift differently.
    """

    def __init__(self, subject: str):
        if subject not in ("owner", "binding"):
            raise ValueError("unknown workspace consent subject")
        self.subject = subject
        super().__init__(f"workspace consent {subject} changed")


class WorkspaceConsentDeclined(Exception):
    """The presenter did not return the exact proposal it was given."""


class TrustedWorkspacePresenter(ABC):
    """Shows one proposal to a person and returns it only on explicit approval.

    Subclassing is an explicit declaration by internal code, not evidence of
    identity. ``approve_workspace`` returns the same proposal object to
    approve; it raises to decline. Any other return value is a decline, so a
    presenter that returns None, False or another proposal cannot approve.
    """

    @abstractmethod
    def approve_workspace(self, proposal: WorkspaceProposal) -> WorkspaceProposal: ...


def require_workspace_expectation(
    access: OwnerAccess,
    *,
    expected_owner: OwnerGrant,
    expected_binding: tuple[str, WorkspaceBinding | None],
) -> None:
    """Read-only: the current owner and retained binding are the expected ones."""
    if access.current_grant() != expected_owner:
        raise WorkspaceConsentChanged("owner")
    if access.workspace().status() != expected_binding:
        raise WorkspaceConsentChanged("binding")


def preview_workspace_binding(
    access: OwnerAccess,
    code: str,
    *,
    expected_owner: OwnerGrant,
    expected_binding: tuple[str, WorkspaceBinding | None],
) -> WorkspaceProposal:
    """Read-only retained proposal for ``code``, bound to the expected owner and binding.

    The expectation is checked against current storage before the inspection
    and against the immutable proposal after it. Nothing is initialized,
    written, consumed or issued.
    """
    require_workspace_expectation(
        access, expected_owner=expected_owner, expected_binding=expected_binding
    )
    proposal = access.workspace().inspect(code)
    if proposal.owner != expected_owner:
        raise WorkspaceConsentChanged("owner")
    if (proposal.previous_state, proposal.previous_binding) != expected_binding:
        raise WorkspaceConsentChanged("binding")
    return proposal


def apply_workspace_binding(
    access: OwnerAccess,
    proposal: WorkspaceProposal,
    *,
    presenter: TrustedWorkspacePresenter,
) -> WorkspaceBinding:
    """Present exactly ``proposal``; on approval run the canonical confirmation.

    The presenter's exceptions propagate unchanged and nothing is written.
    ``confirm`` re-reads the retained proposal in its own write transaction, so
    a changed owner, binding or root, an expired or replayed request and a
    substituted proposal still refuse there with their original errors.
    """
    if not isinstance(presenter, TrustedWorkspacePresenter):
        raise TypeError("a TrustedWorkspacePresenter is required")
    if not isinstance(proposal, WorkspaceProposal):
        raise AccessDenied("workspace_proposal_changed")
    if presenter.approve_workspace(proposal) is not proposal:
        raise WorkspaceConsentDeclined()
    return access.workspace().confirm(expected_proposal=proposal)
