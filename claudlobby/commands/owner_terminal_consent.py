"""Operator-terminal presenter for local workspace consent.

The terminal passed in was opened by ``host_owner._terminal`` after its
operator-context check; this module performs no admission of its own and
must not be constructed around anything else.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import json

from ..command_result import CommandFailure
from ..plane.owner_consent import TrustedWorkspacePresenter


def approve(terminal, word):
    try:
        terminal.write(f"Type {word} to continue: ")
        terminal.flush()
        answer = terminal.readline(32).strip()
    except OSError as exc:
        # Preserve the confirmation failure before the grant storage handler
        # sees it; a hung-up terminal is not an authority persistence outage.
        raise CommandFailure(
            "conflict", "owner confirmation did not complete; inspect owner status"
        ) from exc
    if answer != word:
        raise CommandFailure("conflict", "owner change cancelled")


class TerminalWorkspacePresenter(TrustedWorkspacePresenter):
    def __init__(self, terminal):
        self._terminal = terminal

    def approve_workspace(self, proposal):
        terminal = self._terminal
        a = proposal.attestation
        # JSON escapes terminal control characters, bidi controls and non-ASCII.
        terminal.write(
            "Previous workspace binding: "
            + json.dumps(
                {
                    "state": proposal.previous_state,
                    "binding": asdict(proposal.previous_binding)
                    if proposal.previous_binding
                    else None,
                },
                ensure_ascii=True,
            )
            + "\n"
        )
        terminal.write(
            "Verified proposal metadata (compare with your intended workspace):\n"
        )
        terminal.write(
            json.dumps(
                {
                    "issuer": a.issuer,
                    "app_user_id": a.subject,
                    "workspace_id": a.workspace_id,
                    "operational_origin": a.operational_origin,
                },
                ensure_ascii=True,
            )
            + "\n"
        )
        terminal.write(
            "Expires: "
            + datetime.fromtimestamp(proposal.expires_at, timezone.utc).isoformat()
            + "\n"
        )
        terminal.write(
            "This binds one workspace locally; it issues no session or fleet action grant.\n"
        )
        approve(terminal, "BIND-WORKSPACE")
        return proposal
