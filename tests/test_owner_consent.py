"""Reusable workspace consent against real scratch owner authority; no ingress, network or TTY."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import sqlite3

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import host_owner
from claudlobby.commands.owner_terminal_consent import TerminalWorkspacePresenter
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import (
    AccessDenied,
    AccessUnavailable,
    OwnerAccess,
    PrincipalRef,
)
from claudlobby.plane.owner_consent import (
    TrustedWorkspacePresenter,
    WorkspaceConsentChanged,
    WorkspaceConsentDeclined,
    apply_workspace_binding,
    preview_workspace_binding,
)
from claudlobby.plane.owner_workspace import OwnerWorkspace
from claudlobby.plane.workspace_assertion import WorkspaceAttestation

OWNER = PrincipalRef("tailscale:controlplane.tailscale.com", "1001")
ISSUER = "https://accounts.example.test"
ORIGIN = "https://plane.example.test"
USER = "00000000-0000-4000-8000-000000000001"
WORKSPACE = "00000000-0000-4000-8000-000000000002"
OTHER_WORKSPACE = "00000000-0000-4000-8000-000000000004"
JTI = "00000000-0000-4000-8000-000000000003"
ABSENT = ("absent", None)


def installation(root, *, workspace=True):
    root.mkdir()
    ensure_host_uid(root / "state")
    clock = [1800000000.0]
    access = OwnerAccess.initialize(root, clock=lambda: clock[0])
    pairing = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pairing.token, expected_principal=OWNER)
    if workspace:
        access.workspace().initialize(expected_owner=owner)
    return access, owner, clock


@pytest.fixture
def local(tmp_path, monkeypatch):
    for name in host_owner._GENERATED:
        monkeypatch.delenv(name, raising=False)
    return installation(tmp_path / "a")


def proposed(local):
    access, owner, clock = local
    workspace = access.workspace()
    challenge = workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    return workspace.stage(
        WorkspaceAttestation(
            ISSUER,
            USER,
            WORKSPACE,
            owner.host_uid,
            challenge.nonce,
            ORIGIN,
            int(clock[0]),
            int(clock[0]) + 120,
            JTI,
        ),
        OWNER,
    )


def effects(access):
    """Stored authority this slice could touch: bindings, owners, sessions, grants."""
    with closing(sqlite3.connect(access.path)) as conn:
        rows = {
            "bindings": conn.execute(
                "SELECT revision,owner_revision,workspace_id,active "
                "FROM workspace_bindings ORDER BY revision"
            ).fetchall(),
            "owners": conn.execute(
                "SELECT revision,active FROM grants ORDER BY revision"
            ).fetchall(),
            "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "pending": conn.execute(
                "SELECT count(*) FROM workspace_challenges"
            ).fetchone()[0],
        }
    status = access.local_action_status()
    rows["action_grants"] = (
        status.message_grants,
        status.nudge_grants,
        status.feedback_grants,
        status.permission_grants,
    )
    return rows


class SyntheticPresenter(TrustedWorkspacePresenter):
    """A second, non-terminal presenter. It reads no TTY and admits no caller."""

    def __init__(self, answer=lambda proposal: proposal, before_answer=None):
        self.seen = []
        self._answer = answer
        self._before_answer = before_answer

    def approve_workspace(self, proposal):
        self.seen.append(proposal)
        if self._before_answer is not None:
            self._before_answer()
        return self._answer(proposal)


def console(answers):
    output = io.StringIO()
    output.readline = lambda size=-1: next(answers)
    return output


def test_preview_returns_retained_proposal_and_writes_nothing(local):
    access, owner, _ = local
    staged = proposed(local)
    before_bytes, before = access.path.read_bytes(), effects(access)
    preview = preview_workspace_binding(
        access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
    )
    assert preview == staged
    assert access.path.read_bytes() == before_bytes
    assert effects(access) == before
    assert before == {
        "bindings": [],
        "owners": [(1, 1)],
        "sessions": 0,
        "pending": 1,
        "action_grants": ((), (), (), ()),
    }
    # Previewing consumed nothing: the same code still previews.
    assert (
        preview_workspace_binding(
            access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
        )
        == staged
    )


def test_preview_never_initializes_missing_workspace_authority(tmp_path):
    access, owner, _ = installation(tmp_path / "bare", workspace=False)
    before = access.path.read_bytes()
    with pytest.raises(AccessUnavailable):
        preview_workspace_binding(
            access, "A" * 43, expected_owner=owner, expected_binding=ABSENT
        )
    assert access.path.read_bytes() == before
    with closing(sqlite3.connect(access.path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not tables & {
        "workspace_metadata",
        "workspace_challenges",
        "workspace_bindings",
    }


@pytest.mark.parametrize(
    "kind,code",
    [
        ("unknown", "workspace_request_expired"),
        ("malformed", "invalid_credential"),
        ("unstaged", "workspace_proposal_missing"),
    ],
)
def test_preview_keeps_storage_refusal_codes(local, kind, code):
    access, owner, _ = local
    proposed(local)
    nonce = {"unknown": "A" * 43, "malformed": "not a code"}.get(
        kind
    ) or access.workspace().begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN).nonce
    before = access.path.read_bytes()
    with pytest.raises(AccessDenied) as refusal:
        preview_workspace_binding(
            access, nonce, expected_owner=owner, expected_binding=ABSENT
        )
    assert refusal.value.code == code
    assert access.path.read_bytes() == before


def test_preview_refuses_changed_owner_before_inspecting(local, monkeypatch):
    access, owner, _ = local
    staged = proposed(local)
    access.revoke_owner(expected_revision=owner.revision)
    pairing = access.begin_pairing(OWNER)
    replacement = access.confirm_pairing(pairing.token, expected_principal=OWNER)
    assert replacement != owner
    monkeypatch.setattr(
        OwnerWorkspace, "inspect", lambda *a: pytest.fail("inspected after owner drift")
    )
    before = access.path.read_bytes()
    with pytest.raises(WorkspaceConsentChanged) as changed:
        preview_workspace_binding(
            access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
        )
    assert changed.value.subject == "owner"
    assert not isinstance(changed.value, AccessDenied)
    assert access.path.read_bytes() == before


def test_preview_refuses_changed_binding_before_inspecting(local, monkeypatch):
    access, owner, _ = local
    staged = proposed(local)
    competing = access.workspace().confirm(expected_proposal=proposed(local))
    monkeypatch.setattr(
        OwnerWorkspace,
        "inspect",
        lambda *a: pytest.fail("inspected after binding drift"),
    )
    before = access.path.read_bytes()
    with pytest.raises(WorkspaceConsentChanged) as changed:
        preview_workspace_binding(
            access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
        )
    assert changed.value.subject == "binding"
    assert access.path.read_bytes() == before
    assert access.workspace().status() == ("active", competing)


@pytest.mark.parametrize("subject", ["owner", "binding"])
def test_preview_checks_the_inspected_proposal_itself(local, monkeypatch, subject):
    """The one substituted read here: storage cannot return such a proposal
    while the pre-check passes, so the after-inspection guard is shown a
    proposal that disagrees with the expectation."""
    access, owner, _ = local
    staged = proposed(local)
    drifted = (
        replace(staged, owner=replace(owner, revision=owner.revision + 1))
        if subject == "owner"
        else replace(staged, previous_state="revoked")
    )
    monkeypatch.setattr(OwnerWorkspace, "inspect", lambda self, nonce: drifted)
    with pytest.raises(WorkspaceConsentChanged) as changed:
        preview_workspace_binding(
            access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
        )
    assert changed.value.subject == subject


def test_synthetic_presenter_binds_the_exact_previewed_proposal(local):
    access, owner, _ = local
    staged = proposed(local)
    proposed(local)  # a competing pending request is cleared by the one confirmation
    preview = preview_workspace_binding(
        access, staged.nonce, expected_owner=owner, expected_binding=ABSENT
    )
    presenter = SyntheticPresenter()
    binding = apply_workspace_binding(access, preview, presenter=presenter)
    assert len(presenter.seen) == 1 and presenter.seen[0] is preview
    assert binding.revision == 1 and binding.active and binding.owner == owner
    assert (
        binding.issuer,
        binding.subject,
        binding.workspace_id,
        binding.operational_origin,
    ) == (ISSUER, USER, WORKSPACE, ORIGIN)
    assert access.workspace().status() == ("active", binding)
    assert effects(access) == {
        "bindings": [(1, owner.revision, WORKSPACE, 1)],
        "owners": [(1, 1)],
        "sessions": 0,
        "pending": 0,
        "action_grants": ((), (), (), ()),
    }


def test_replayed_proposal_refuses_after_one_binding(local):
    access, owner, _ = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )
    binding = apply_workspace_binding(access, preview, presenter=SyntheticPresenter())
    before = access.path.read_bytes()
    with pytest.raises(AccessDenied, match="workspace_request_expired"):
        apply_workspace_binding(access, preview, presenter=SyntheticPresenter())
    with pytest.raises(WorkspaceConsentChanged):
        preview_workspace_binding(
            access, preview.nonce, expected_owner=owner, expected_binding=ABSENT
        )
    assert access.path.read_bytes() == before
    assert access.workspace().status() == ("active", binding)


def test_presenter_decline_propagates_unchanged_and_writes_nothing(local):
    access, owner, _ = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )

    class NotNow(Exception):
        pass

    def decline(proposal):
        raise NotNow("declined in the presenter")

    presenter = SyntheticPresenter(answer=decline)
    before = access.path.read_bytes()
    with pytest.raises(NotNow, match="declined in the presenter"):
        apply_workspace_binding(access, preview, presenter=presenter)
    assert presenter.seen == [preview]
    assert access.path.read_bytes() == before
    assert access.workspace().status() == ABSENT
    # The request survives a decline and may still be approved once.
    assert (
        apply_workspace_binding(
            access, preview, presenter=SyntheticPresenter()
        ).revision
        == 1
    )


@pytest.mark.parametrize(
    "answer", [None, False, True, "BIND-WORKSPACE", "equal-copy", "other-proposal"]
)
def test_only_the_identical_proposal_object_approves(local, answer):
    access, owner, _ = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )
    returned = {
        "equal-copy": replace(preview),
        "other-proposal": replace(
            preview,
            attestation=replace(preview.attestation, workspace_id=OTHER_WORKSPACE),
        ),
    }.get(answer, answer)
    before = access.path.read_bytes()
    with pytest.raises(WorkspaceConsentDeclined):
        apply_workspace_binding(
            access, preview, presenter=SyntheticPresenter(answer=lambda _: returned)
        )
    assert access.path.read_bytes() == before
    assert access.workspace().status() == ABSENT


def test_undeclared_presenter_and_non_proposal_refuse_before_presenting(local):
    access, owner, _ = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )

    class LooksLikeOne:
        def approve_workspace(self, proposal):
            pytest.fail("an undeclared presenter was asked")

    before = access.path.read_bytes()
    with pytest.raises(TypeError):
        apply_workspace_binding(access, preview, presenter=LooksLikeOne())
    presenter = SyntheticPresenter()
    with pytest.raises(AccessDenied, match="workspace_proposal_changed"):
        apply_workspace_binding(access, {"nonce": preview.nonce}, presenter=presenter)
    assert presenter.seen == []
    assert access.path.read_bytes() == before


@pytest.mark.parametrize(
    "change,code",
    [
        ("owner-revoked", "owner_not_paired"),
        ("owner-replaced", "workspace_binding_changed"),
        ("competing-binding", "workspace_request_expired"),
        ("expired", "workspace_request_expired"),
    ],
)
def test_change_while_presented_refuses_in_the_storage_transaction(local, change, code):
    access, owner, clock = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )
    competing = []

    def drift():
        if change.startswith("owner"):
            access.revoke_owner(expected_revision=owner.revision)
        if change == "owner-replaced":
            pairing = access.begin_pairing(OWNER)
            access.confirm_pairing(pairing.token, expected_principal=OWNER)
        if change == "competing-binding":
            competing.append(
                access.workspace().confirm(expected_proposal=proposed(local))
            )
        if change == "expired":
            clock[0] = preview.expires_at

    presenter = SyntheticPresenter(before_answer=drift)
    with pytest.raises(AccessDenied) as refusal:
        apply_workspace_binding(access, preview, presenter=presenter)
    assert refusal.value.code == code
    assert presenter.seen == [preview]
    after = effects(access)
    # Only the drift itself is stored; the approved-then-refused proposal bound nothing.
    assert after["bindings"] == (
        [(1, owner.revision, WORKSPACE, 1)] if competing else []
    )
    assert after["sessions"] == 0 and after["action_grants"] == ((), (), (), ())
    if competing:
        assert access.workspace().status() == ("active", competing[0])


def test_substituted_proposal_refuses_even_when_approved(local):
    access, owner, _ = local
    preview = preview_workspace_binding(
        access, proposed(local).nonce, expected_owner=owner, expected_binding=ABSENT
    )
    substituted = replace(
        preview, attestation=replace(preview.attestation, workspace_id=OTHER_WORKSPACE)
    )
    before = access.path.read_bytes()
    with pytest.raises(AccessDenied, match="workspace_proposal_changed"):
        apply_workspace_binding(access, substituted, presenter=SyntheticPresenter())
    assert access.path.read_bytes() == before
    assert access.workspace().inspect(preview.nonce) == preview


def test_another_installations_proposal_refuses_here_and_survives_there(
    local, tmp_path
):
    access, owner, _ = local
    proposed(local)
    other = installation(tmp_path / "b")
    foreign = proposed(other)
    before = access.path.read_bytes()
    with pytest.raises(AccessDenied, match="workspace_request_expired"):
        preview_workspace_binding(
            access, foreign.nonce, expected_owner=owner, expected_binding=ABSENT
        )
    with pytest.raises(AccessDenied, match="workspace_request_expired"):
        apply_workspace_binding(access, foreign, presenter=SyntheticPresenter())
    # The other installation's owner as the expectation is drift here, not a match.
    with pytest.raises(WorkspaceConsentChanged):
        preview_workspace_binding(
            access, foreign.nonce, expected_owner=other[1], expected_binding=ABSENT
        )
    assert access.path.read_bytes() == before
    assert access.workspace().status() == ABSENT
    assert other[0].workspace().inspect(foreign.nonce) == foreign
    assert other[0].workspace().status() == ABSENT


def expected_review(proposal, previous='{"state": "absent", "binding": null}'):
    return (
        "Previous workspace binding: " + previous + "\n"
        "Verified proposal metadata (compare with your intended workspace):\n"
        + json.dumps(
            {
                "issuer": ISSUER,
                "app_user_id": USER,
                "workspace_id": WORKSPACE,
                "operational_origin": ORIGIN,
            }
        )
        + "\n"
        "Expires: "
        + datetime.fromtimestamp(proposal.expires_at, timezone.utc).isoformat()
        + "\n"
        "This binds one workspace locally; it issues no session or fleet action grant.\n"
        "Type BIND-WORKSPACE to continue: "
    )


def test_terminal_presenter_text_order_and_answers(local):
    staged = proposed(local)
    approved = console(iter(["BIND-WORKSPACE\n"]))
    assert TerminalWorkspacePresenter(approved).approve_workspace(staged) is staged
    assert approved.getvalue() == expected_review(staged)
    assert staged.nonce not in approved.getvalue()
    for answer in ("bind-workspace\n", "BIND\n", "\n", ""):
        declined = console(iter([answer]))
        with pytest.raises(CommandFailure, match="owner change cancelled") as failure:
            TerminalWorkspacePresenter(declined).approve_workspace(staged)
        assert failure.value.error.code == "conflict"
        assert declined.getvalue() == expected_review(staged)


@pytest.mark.parametrize("operation", ["write", "flush", "readline"])
def test_terminal_presenter_hangup_is_a_confirmation_failure(
    local, monkeypatch, operation
):
    staged = proposed(local)
    tty = console(iter(["BIND-WORKSPACE\n"]))
    original_write = tty.write

    def hung_up(*args):
        if operation == "write" and not args[0].startswith("Type BIND-WORKSPACE"):
            return original_write(*args)
        raise OSError("synthetic terminal hangup")

    monkeypatch.setattr(tty, operation, hung_up)
    with pytest.raises(
        CommandFailure, match="owner confirmation did not complete"
    ) as failure:
        TerminalWorkspacePresenter(tty).approve_workspace(staged)
    assert "synthetic terminal hangup" not in str(failure.value)


def test_terminal_presenter_escapes_hostile_metadata(local):
    staged = proposed(local)
    hostile = replace(
        staged,
        attestation=replace(
            staged.attestation,
            issuer="https://accounts.example.test/\x1b[2J‮\nType BIND-WORKSPACE",
        ),
    )
    tty = console(iter(["NO\n"]))
    with pytest.raises(CommandFailure):
        TerminalWorkspacePresenter(tty).approve_workspace(hostile)
    text = tty.getvalue()
    assert "\x1b" not in text and "‮" not in text
    assert "\\u001b[2J\\u202e\\nType BIND-WORKSPACE" in text
    assert text.count("\n") == 5  # the injected newline did not add a line


def test_console_binds_through_the_shared_consent_with_unchanged_text(
    local, monkeypatch
):
    access, owner, _ = local
    staged = proposed(local)
    monkeypatch.setattr(host_owner, "_challenge", lambda *a, **k: staged.nonce)
    tty = console(iter(["BIND-WORKSPACE\n"]))
    result = host_owner._confirm_workspace(access, tty, owner)
    assert tty.getvalue() == expected_review(staged)
    state, binding = access.workspace().status()
    assert (
        state == "active"
        and result.data["binding"]["revision"] == binding.revision == 1
    )
    assert result.lines == ("Local workspace bound. No connected session was issued.",)
    assert effects(access) == {
        "bindings": [(1, owner.revision, WORKSPACE, 1)],
        "owners": [(1, 1)],
        "sessions": 0,
        "pending": 0,
        "action_grants": ((), (), (), ()),
    }


def test_console_decline_keeps_cancellation_and_the_pending_request(local, monkeypatch):
    access, owner, _ = local
    staged = proposed(local)
    monkeypatch.setattr(host_owner, "_challenge", lambda *a, **k: staged.nonce)
    before = access.path.read_bytes()
    with pytest.raises(CommandFailure, match="^owner change cancelled$"):
        host_owner._confirm_workspace(
            access, console(iter(["NO\n"])), owner, recover=True
        )
    assert access.path.read_bytes() == before
    assert access.workspace().inspect(staged.nonce) == staged


@pytest.mark.parametrize("subject", ["owner", "binding"])
def test_console_refuses_drift_during_hidden_code_entry(local, monkeypatch, subject):
    access, owner, _ = local
    staged = proposed(local)
    competing = []

    def read_code(*args, **kwargs):
        if subject == "owner":
            access.revoke_owner(expected_revision=owner.revision)
        else:
            competing.append(
                access.workspace().confirm(expected_proposal=proposed(local))
            )
        return staged.nonce

    monkeypatch.setattr(host_owner, "_challenge", read_code)
    tty = console(iter([]))
    with pytest.raises(CommandFailure, match=f"{subject} changed") as failure:
        host_owner._confirm_workspace(access, tty, owner, recover=True)
    assert failure.value.error.code == "conflict"
    assert tty.getvalue() == ""  # nothing was presented and no retry was offered
    assert access.workspace().status() == (
        ("active", competing[0]) if competing else ABSENT
    )
    assert effects(access)["sessions"] == 0
