"""Explicit local-console owner administration, separate from browser authority.

An operator terminal is a usability/accident boundary, not same-UID isolation.
Before it opens, a change runs the operator-context check that the other
operator-only commands run (native unit ancestry on an activated host).
Processes with the operator's privileges can edit the same authority files.
These doors never grant website membership. Ordinary messages require an
explicit local actor/fleet approval separate from read pairing.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import io
import json
import os
import sqlite3
import sys
import warnings

from ..command_result import CommandFailure, CommandOutput
from ..plane.owner_access import ACTION_DENIALS, AccessDenied, AccessUnavailable, OwnerAccess
from ..plane.owner_source import SourceDenied, SourceUnavailable
from ..plane.owner_observation import owner_pairing_state, source_refusal

_GENERATED = ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "FLEET_ROOT",
              "FLEET_NAME", "CLAUDLOBBY_FLEET", "CLAUDLOBBY_TIMER_CONTEXT")
_ACTION_LABELS = {
    "message": ("ordinary messages", "ordinary-message", "ALLOW", "REVOKE-MESSAGES"),
    "nudge": ("task nudges", "task-nudge", "ALLOW-NUDGES", "REVOKE-NUDGES"),
    "feedback": ("task feedback", "task-feedback", "ALLOW-FEEDBACK", "REVOKE-FEEDBACK"),
    "permission": ("permission decisions", "permission-decision", "ALLOW-PERMISSIONS", "REVOKE-PERMISSIONS"),
}


@contextmanager
def _terminal(root):
    if not sys.stdin.isatty():
        raise CommandFailure("conflict", "owner changes require an interactive operator terminal")
    from .operator_context import require_operator_context
    require_operator_context(root)
    try:
        # A terminal is not seekable: buffered r+ would require seeks between
        # reading and writing. FileIO keeps this duplex console unbuffered.
        with io.TextIOWrapper(io.FileIO("/dev/tty", "r+"), encoding="utf-8",
                              line_buffering=True) as terminal:
            if not terminal.isatty():
                raise OSError
            yield terminal
    except (OSError, EOFError, KeyboardInterrupt) as exc:
        raise CommandFailure("conflict", "owner confirmation did not complete; inspect owner status") from exc


def _approve(terminal, word):
    try:
        terminal.write(f"Type {word} to continue: ")
        terminal.flush()
        answer = terminal.readline(32).strip()
    except OSError as exc:
        # Preserve the confirmation failure before the grant storage handler
        # sees it; a hung-up terminal is not an authority persistence outage.
        raise CommandFailure("conflict", "owner confirmation did not complete; inspect owner status") from exc
    if answer != word:
        raise CommandFailure("conflict", "owner change cancelled")


def _challenge(terminal, *, label="browser pairing"):
    # getpass must never fall back to echoing a challenge into redirected input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(f"Paste the {label} code (hidden): ", stream=terminal)
        except getpass.GetPassWarning as exc:
            raise CommandFailure("conflict", "a terminal with hidden input is required") from exc


def _describe(terminal, grant):
    # JSON escapes terminal control characters, bidi controls and non-ASCII.
    terminal.write("Host: " + json.dumps(grant.host_uid) + "\n")
    terminal.write("Principal: " + json.dumps(asdict(grant.principal), ensure_ascii=True) + "\n")
    terminal.write(f"Owner revision: {grant.revision}\n")


def _active_owner(store):
    owner = store.current_grant()
    if owner is None or not owner.active:
        raise CommandFailure("conflict", "an active owner pairing is required")
    return owner


def _grant_preview(paths, store, fleet, actor_alias, release):
    """Read current frozen/recorded identities; never register at preview."""
    from ..activation_identity import read_selected_identity_bindings
    from ..operation_context import _MissingHumanIdentity, bind_task_context, resolve_operation_scope

    owner = _active_owner(store)
    selected, origin = resolve_operation_scope(root=paths.root, fleet=fleet, package=paths.package)
    if origin is not None:
        raise CommandFailure("conflict", "owner grants require a local operator")
    bindings = read_selected_identity_bindings(paths.root, selected.fleet.name, package=paths.package)
    ctx = None
    try:
        ctx = bind_task_context(selected, operator_alias=actor_alias)
    except _MissingHumanIdentity:
        # The binder checked host/fleet/bots before this sole absence. Compare
        # recorded IDs to activation too before approving any registration.
        from ..plane.db import connect_ro, db_file
        from ..plane.identity import lookup
        from ..plane.schema_state import require_current_schema
        with closing(connect_ro(db_file(paths.root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            if (lookup(conn, "fleet", selected.fleet.name) != bindings["fleet_uid"]
                    or any(lookup(conn, "actor", f"bot:{selected.fleet.name}/{name}") != uid
                           for name, uid in bindings["bots"].items())):
                raise CommandFailure("conflict", "active and recorded owner identities differ")
    if (owner.host_uid != bindings["host_uid"] or ctx is not None and (
            ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]
            or {name: bot.uid for name, bot in ctx.bots.items()} != bindings["bots"])):
        raise CommandFailure("conflict", "active and recorded owner identities differ")
    return {"owner": owner, "release": release.release_id, "fleet": selected.fleet.name,
            "bindings": bindings, "actor_alias": actor_alias,
            "actor_uid": ctx.caller.uid if ctx is not None else None}


def _describe_binding(terminal, fleet_uid, actor_alias, actor_uid):
    terminal.write("Fleet UID: " + json.dumps(fleet_uid) + "\n")
    terminal.write("Actor: " + json.dumps(actor_alias, ensure_ascii=True) + "\n")
    terminal.write("Actor UID: " + json.dumps(actor_uid) + "\n")


def _describe_action(terminal, preview):
    _describe(terminal, preview["owner"])
    terminal.write("Fleet: " + json.dumps(preview["fleet"]) + "\n")
    _describe_binding(terminal, preview["bindings"]["fleet_uid"], preview["actor_alias"], preview["actor_uid"])


def _unchanged(before, after):
    if before != after:
        raise CommandFailure("conflict", "displayed owner or actor identities changed; inspect and retry")


def _allow_action(args, paths, store, terminal, action):
    from ..operation_context import _valid_human_alias, resolve_task_context, resolve_task_mutation_context
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    if not _valid_human_alias(args.actor):
        raise CommandFailure("invalid_argument", "--actor must be a canonical local human: alias")
    with mutation_admission(paths.root, identity=RuntimeIdentity.current()) as release:
        preview = _grant_preview(paths, store, args.target_fleet, args.actor, release)
    if preview["actor_uid"] is None:
        if not args.register_actor:
            raise CommandFailure("conflict", "human actor is not registered; explicitly use --register-actor")
        _describe_action(terminal, preview)
        terminal.write("Register this local actor's first contact. Its UID is allocated only after approval.\n"
                       "Registration alone does not grant owner actions.\n")
        _approve(terminal, "REGISTER")
        with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                                expected_release=preview["release"]) as release:
            _unchanged(preview, _grant_preview(paths, store, args.target_fleet, args.actor, release))
            resolve_task_mutation_context(root=paths.root, fleet=preview["fleet"],
                                          operator_alias=args.actor, package=paths.package)
            registered = _grant_preview(paths, store, args.target_fleet, args.actor, release)
            if registered["actor_uid"] is None:
                raise CommandFailure("conflict", "approved actor registration is unavailable")
            _unchanged(preview, {**registered, "actor_uid": None})
            preview = registered
    _describe_action(terminal, preview)
    terminal.write("This capability may be used through direct owner access or its explicitly bound connected workspace.\n"
                   "Workspace approval alone grants no fleet action.\n")
    if action == "message":
        terminal.write("Allow ordinary messages only as this actor in this fleet.\n"
                       "This grants no task mutations, replies or permission decisions.\n")
    elif action == "nudge":
        terminal.write("Allow selected-task nudges only as this actor in this fleet.\n"
                       "Nudges record a task event and ask its fleet manager to act.\n"
                       "This grants no ordinary messages, other task mutations or permission decisions.\n")
    elif action == "feedback":
        terminal.write("Allow task-linked comments as this actor to this fleet's configured lead.\n"
                       "Comments may concern open or completed work.\n"
                       "This grants no ordinary messages, nudges, task state changes or permission decisions.\n")
    else:
        terminal.write("Allow this actor to approve, decline and revoke exact task permission requests.\n"
                       "Requests can concern merges, deployments, spending or deletion.\n"
                       "Each decision must name the recorded request and its exact scope.\n"
                       "This capability records authority; it does not execute those effects.\n"
                       "Revoking this capability prevents new decisions; existing approvals remain until individually revoked.\n")
    label, _, allow_word, _ = _ACTION_LABELS[action]
    _approve(terminal, allow_word)
    with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                            expected_release=preview["release"]) as release:
        _unchanged(preview, _grant_preview(paths, store, args.target_fleet, args.actor, release))
        ctx = resolve_task_context(root=paths.root, fleet=preview["fleet"],
                                   operator_alias=args.actor, package=paths.package)
        if (ctx.host_uid != preview["owner"].host_uid or ctx.fleet_uid != preview["bindings"]["fleet_uid"]
                or ctx.caller.uid != preview["actor_uid"] or ctx.caller.alias != preview["actor_alias"]):
            raise CommandFailure("conflict", "approved actor identities changed")
        allow = {"message": store.allow_messages, "nudge": store.allow_nudges,
                 "feedback": store.allow_feedback, "permission": store.allow_permissions}[action]
        grant = allow(expected_owner=preview["owner"], fleet_uid=ctx.fleet_uid,
                                    actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    return CommandOutput({"state": "allowed", "grant": asdict(grant)},
                         lines=(f"Owner {label} allowed for this actor and fleet.",))


def _revoke_action(args, store, terminal, action):
    owner = _active_owner(store)
    current, revoke = {
        "message": (store.current_message_grant, store.revoke_messages),
        "nudge": (store.current_nudge_grant, store.revoke_nudges),
        "feedback": (store.current_feedback_grant, store.revoke_feedback),
        "permission": (store.current_permission_grant, store.revoke_permissions),
    }[action]
    grant = current(expected_owner=owner, fleet_uid=args.fleet_uid)
    _describe(terminal, owner)
    _describe_binding(terminal, grant.fleet_uid, grant.actor_alias, grant.actor_uid)
    _, label, _, revoke_word = _ACTION_LABELS[action]
    terminal.write(f"Revoke only this retained {label} grant. Owner read access remains.\n")
    _approve(terminal, revoke_word)
    if current(expected_owner=owner, fleet_uid=args.fleet_uid) != grant:
        raise CommandFailure("conflict", f"displayed {action} grant changed; inspect and retry")
    revoke(expected_owner=owner, fleet_uid=grant.fleet_uid, expected_grant=grant)
    return CommandOutput({"state": "revoked", "fleet_uid": grant.fleet_uid},
                         lines=(f"Owner {label} grant revoked. Read access remains.",))


@contextmanager
def _grant_errors(action):
    """Translate grant-authoring failures before they leave the terminal body."""
    from ..activation_state import ActivationError
    from ..operation_context import OperationContextError
    from ..runtime_admission import ReleaseMismatch
    from ..plane.schema_state import PendingMigrationError
    from ..plane.migrations import DowngradeError

    try:
        yield
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "owner grant requires this installation's active sealed runtime") from exc
    except OperationContextError as exc:
        raise CommandFailure(exc.code, f"owner {action} identities could not be bound; verify the active fleet and actor") from exc
    except ActivationError as exc:
        raise CommandFailure("conflict", "active configuration is unavailable; verify the selected installation") from exc
    except AccessDenied as exc:
        if exc.code == f"invalid_{action}_binding":
            raise CommandFailure("invalid_argument", f"{action} grants require canonical fleet and human actor bindings") from exc
        if exc.code == ACTION_DENIALS[action]:
            raise CommandFailure("conflict", f"no retained {action} grant for this fleet; inspect host owner status") from exc
        if exc.code == f"{action}_binding_changed":
            raise CommandFailure("conflict", f"{action} grant changed or already belongs to another actor; inspect host owner status "
                                 "and revoke the retained grant before approving a replacement") from exc
        if exc.code == "grant_changed":
            raise CommandFailure("conflict", "owner pairing changed; inspect host owner status before retrying") from exc
        raise
    except (PendingMigrationError, DowngradeError, sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", f"{action} grant could not be bound or persisted; inspect local authority, "
                             "active configuration and Plane storage") from exc


def dispatch(args):
    from ..context import resolve_paths
    from ..paths import InvalidPathSelector
    from ..plane.ids import read_host_uid

    if any(key in os.environ for key in _GENERATED):
        raise CommandFailure("conflict", "owner commands require an operator shell without bot or fleet selectors")
    if args.root is None or args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "owner commands require explicit --root and no fleet or seed")
    if args.owner_action not in {"status", "workspace-status"} and args.json:
        raise CommandFailure("invalid_argument", "--json is supported for owner status and workspace-status only")
    try:
        paths = resolve_paths(root=args.root)
        store = OwnerAccess(paths.root)
        if args.owner_action == "serve":
            from ..plane.owner_server import OwnerServerConfigurationError, OwnerServerStartupError, serve

            if (args.workspace_issuer is None) != (args.workspace_origin is None):
                raise CommandFailure("invalid_argument", "workspace issuer and origin must be supplied together")
            try:
                serve(paths.root, origin=args.origin, tailscale_binary=args.tailscale,
                      socket_path=args.socket, tailscale_socket=args.tailscale_socket,
                      package=paths.package, workspace_issuer=args.workspace_issuer,
                      workspace_origin=args.workspace_origin)
            except OwnerServerStartupError as exc:
                raise CommandFailure("unavailable", "owner server startup failed; inspect local server logs") from exc
            except OwnerServerConfigurationError as exc:
                raise CommandFailure("unavailable", str(exc)) from exc
            except ImportError as exc:
                raise CommandFailure("unavailable", "owner serving requires the optional [plane-ui] dependencies") from exc
            except OSError as exc:
                raise CommandFailure("unavailable", "owner server failed; inspect local logs and configured resources") from exc
            return CommandOutput({"state": "stopped"}, lines=("Owner server stopped.",))
        if args.owner_action == "workspace-status":
            state, binding = store.workspace().status()
            return CommandOutput({"state": state, "effective_active": state == "active",
                                  "binding": asdict(binding) if binding else None},
                                 lines=(f"Local workspace binding: {state}. No connected session is issued.",))
        if args.owner_action == "status":
            status = store.local_action_status()
            grant, messages, nudges, feedbacks = (status.owner, status.message_grants,
                                                  status.nudge_grants, status.feedback_grants)
            state = owner_pairing_state(grant)
            lines = [f"Owner access: {state}."]
            for message in messages:
                lines.append("Message grant: " + json.dumps(asdict(message), ensure_ascii=True))
            for nudge in nudges:
                lines.append("Nudge grant: " + json.dumps(asdict(nudge), ensure_ascii=True))
            for feedback in feedbacks:
                lines.append("Feedback grant: " + json.dumps(asdict(feedback), ensure_ascii=True))
            for permission in status.permission_grants:
                lines.append("Permission grant: " + json.dumps(asdict(permission), ensure_ascii=True))
            return CommandOutput({"state": state, "owner": asdict(grant) if grant else None,
                                  "message_grants": [asdict(message) for message in messages],
                                  "nudge_grants": [asdict(nudge) for nudge in nudges],
                                  "feedback_grants": [asdict(feedback) for feedback in feedbacks],
                                  "permission_grants": [asdict(p) for p in status.permission_grants]},
                                 lines=tuple(lines))
        with _terminal(paths.root) as terminal:
            if args.owner_action == "initialize-connected":
                owner = _active_owner(store)
                _describe(terminal, owner)
                terminal.write("Prepare optional connected read-session storage in this authority database.\n"
                               "Workspace authority must already be initialized. No binding, session or action grant is issued.\n")
                _approve(terminal, "INITIALIZE-CONNECTED")
                store.connected().initialize(expected_owner=owner)
                return CommandOutput({"state": "initialized"}, lines=("Connected read-session storage initialized.",))
            if args.owner_action in {"allow-messages", "revoke-messages", "allow-nudges", "revoke-nudges",
                                     "allow-feedback", "revoke-feedback", "allow-permissions", "revoke-permissions"}:
                action = {"messages": "message", "nudges": "nudge", "feedback": "feedback", "permissions": "permission"}[
                    args.owner_action.split("-", 1)[1]]
                with _grant_errors(action):
                    if args.owner_action.startswith("allow-"):
                        return _allow_action(args, paths, store, terminal, action)
                    return _revoke_action(args, store, terminal, action)
            if args.owner_action in {"initialize-workspace", "confirm-workspace", "revoke-workspace"}:
                workspace = store.workspace()
                owner = _active_owner(store)
                _describe(terminal, owner)
                if args.owner_action == "initialize-workspace":
                    terminal.write("Prepare optional local workspace authority. No workspace or session is approved.\n")
                    _approve(terminal, "INITIALIZE-WORKSPACE")
                    workspace.initialize(expected_owner=owner)
                    return CommandOutput({"state": "initialized"}, lines=("Local workspace authority initialized.",))
                if args.owner_action == "confirm-workspace":
                    proposal = workspace.inspect(_challenge(terminal, label="workspace approval"))
                    if proposal.owner != owner:
                        raise CommandFailure("conflict", "owner changed; inspect and retry")
                    a = proposal.attestation
                    terminal.write("Previous workspace binding: " + json.dumps({
                        "state": proposal.previous_state,
                        "binding": asdict(proposal.previous_binding) if proposal.previous_binding else None},
                        ensure_ascii=True) + "\n")
                    terminal.write("Verified proposal metadata (compare with your intended workspace):\n")
                    terminal.write(json.dumps({"issuer": a.issuer, "app_user_id": a.subject,
                        "workspace_id": a.workspace_id, "operational_origin": a.operational_origin},
                        ensure_ascii=True) + "\n")
                    terminal.write("Expires: " + datetime.fromtimestamp(proposal.expires_at, timezone.utc).isoformat() + "\n")
                    terminal.write("This binds one workspace locally; it issues no session or fleet action grant.\n")
                    _approve(terminal, "BIND-WORKSPACE")
                    binding = workspace.confirm(expected_proposal=proposal)
                    return CommandOutput({"state": "active", "binding": asdict(binding)},
                                         lines=("Local workspace bound. No connected session was issued.",))
                _, binding = workspace.status()
                if binding is None or not binding.active:
                    raise CommandFailure("conflict", "no retained active workspace binding to revoke")
                terminal.write(json.dumps(asdict(binding), ensure_ascii=True) + "\n")
                terminal.write("Revoke only this workspace binding. Direct owner access and fleet grants remain.\n")
                _approve(terminal, "REVOKE-WORKSPACE")
                revoked = workspace.revoke(expected_binding=binding, expected_owner=owner)
                return CommandOutput({"state": "revoked", "binding": asdict(revoked)},
                                     lines=("Local workspace binding revoked. Direct owner access remains.",))
            if args.owner_action == "bind-source":
                from ..plane.owner_source import bind_source

                store.current_grant()  # require initialized local authority
                host_uid = read_host_uid(paths.root / "state")
                terminal.write("Bind the existing Plane database to host " + json.dumps(host_uid) + ".\n"
                               "Confirm that all its history, including imported records, belongs to this installation.\n"
                               "This writes ownership metadata and indexes; it does not copy, migrate or remove records.\n")
                _approve(terminal, "BIND")
                bind_source(paths.root, expected_host_uid=host_uid)
                return CommandOutput({"state": "bound"}, lines=("Plane source bound to this installation.",))
            if args.owner_action == "initialize":
                host_uid = read_host_uid(paths.root / "state")
                terminal.write("Prepare owner access for host " + json.dumps(host_uid) + ".\n"
                               "This does not pair anyone or expose a network service.\n")
                _approve(terminal, "INITIALIZE")
                OwnerAccess.initialize(paths.root)
                return CommandOutput({"state": "initialized"}, lines=("Owner authority is initialized.",))
            if args.owner_action == "confirm":
                challenge = store.inspect_pairing(_challenge(terminal))
                terminal.write("Compare this principal with the request shown in your browser:\n")
                terminal.write(json.dumps(asdict(challenge.principal), ensure_ascii=True) + "\n")
                terminal.write("Host: " + json.dumps(read_host_uid(paths.root / "state")) + "\n")
                terminal.write("Expires: " + datetime.fromtimestamp(challenge.expires_at, timezone.utc).isoformat() + "\n")
                terminal.write("Approval permits private reads across this host's fleets.\n")
                _approve(terminal, "PAIR")
                grant = store.confirm_pairing(challenge.token, expected_principal=challenge.principal)
                return CommandOutput({"state": "paired", "revision": grant.revision},
                                     lines=("Owner paired. Return to your browser and sign in.",))
            if args.owner_action == "revoke":
                grant = store.current_grant()
                if grant is None or not grant.active:
                    raise CommandFailure("conflict", "no active owner pairing to revoke")
                _describe(terminal, grant)
                terminal.write("This revokes this pairing and its browser sessions. Fleets keep running.\n")
                _approve(terminal, "REVOKE")
                revoked = store.revoke_owner(expected_revision=grant.revision)
                return CommandOutput({"state": "revoked", "revision": revoked.revision},
                                     lines=("Owner pairing revoked. Existing sessions are no longer authorized.",))
            raise CommandFailure("invalid_argument", "unsupported owner command")
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid host root selector") from exc
    except (SourceUnavailable, SourceDenied) as exc:
        observation = source_refusal(exc)
        code, message = {
            'source_binding_required': ('unavailable',
                "Plane source lacks required binding or indexes; verify the selected installation "
                "and re-run host owner bind-source. Binding refuses foreign or mixed-host history."),
            'source_denied': ('conflict',
                "Plane source is foreign or mixed-host; select the correct installation "
                "or investigate its history. Do not rebind this source."),
            'source_unavailable': ('unavailable',
                "Plane source cannot be verified; inspect the selected installation's "
                "database and schema locally. Pairing again cannot repair source state."),
        }[observation.reason]
        raise CommandFailure(code, message) from exc
    except AccessDenied as exc:
        if args.owner_action == "initialize-connected":
            raise CommandFailure("conflict", "connected initialization requires the current native paired owner; inspect host owner status") from exc
        if "workspace" in args.owner_action:
            raise CommandFailure("conflict", "workspace request or binding is no longer valid; inspect host owner "
                                 "workspace-status and obtain a fresh verified proposal if needed") from exc
        raise CommandFailure("conflict", "owner request is no longer valid; inspect status and request pairing again") from exc
    except (AccessUnavailable, ValueError) as exc:
        if args.owner_action == "initialize-connected":
            raise CommandFailure("unavailable", "connected storage or workspace authority is unavailable; inspect local schemas "
                                 "and initialize workspace authority first; partial schemas are never repaired") from exc
        if "workspace" in args.owner_action:
            raise CommandFailure("unavailable", "local workspace authority is unavailable; inspect the selected root, "
                                 "owner status and optional schema; use host owner initialize-workspace only "
                                 "when the optional schema has not been initialized") from exc
        raise CommandFailure("unavailable", "owner authority is unavailable; verify the selected installation") from exc
