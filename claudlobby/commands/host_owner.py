"""Explicit local-console owner administration, separate from browser authority.

An operator terminal is a usability/accident boundary, not same-UID isolation.
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
from ..plane.owner_access import AccessDenied, AccessUnavailable, OwnerAccess
from ..plane.owner_source import SourceDenied, SourceNeedsBinding, SourceUnavailable

_GENERATED = ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "FLEET_ROOT",
              "FLEET_NAME", "CLAUDLOBBY_FLEET", "CLAUDLOBBY_TIMER_CONTEXT")


@contextmanager
def _terminal():
    if not sys.stdin.isatty():
        raise CommandFailure("conflict", "owner changes require an interactive operator terminal")
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


def _challenge(terminal):
    # getpass must never fall back to echoing a challenge into redirected input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Paste the browser pairing code (hidden): ", stream=terminal)
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


def _message_preview(paths, store, fleet, actor_alias, release):
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
                raise CommandFailure("conflict", "active and recorded message identities differ")
    if (owner.host_uid != bindings["host_uid"] or ctx is not None and (
            ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]
            or {name: bot.uid for name, bot in ctx.bots.items()} != bindings["bots"])):
        raise CommandFailure("conflict", "active and recorded message identities differ")
    return {"owner": owner, "release": release.release_id, "fleet": selected.fleet.name,
            "bindings": bindings, "actor_alias": actor_alias,
            "actor_uid": ctx.caller.uid if ctx is not None else None}


def _describe_binding(terminal, fleet_uid, actor_alias, actor_uid):
    terminal.write("Fleet UID: " + json.dumps(fleet_uid) + "\n")
    terminal.write("Actor: " + json.dumps(actor_alias, ensure_ascii=True) + "\n")
    terminal.write("Actor UID: " + json.dumps(actor_uid) + "\n")


def _describe_messages(terminal, preview):
    _describe(terminal, preview["owner"])
    terminal.write("Fleet: " + json.dumps(preview["fleet"]) + "\n")
    _describe_binding(terminal, preview["bindings"]["fleet_uid"], preview["actor_alias"], preview["actor_uid"])


def _unchanged(before, after):
    if before != after:
        raise CommandFailure("conflict", "displayed owner or message identities changed; inspect and retry")


def _allow_messages(args, paths, store, terminal):
    from ..operation_context import _valid_human_alias, resolve_task_context, resolve_task_mutation_context
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    if not _valid_human_alias(args.actor):
        raise CommandFailure("invalid_argument", "--actor must be a canonical local human: alias")
    with mutation_admission(paths.root, identity=RuntimeIdentity.current()) as release:
        preview = _message_preview(paths, store, args.target_fleet, args.actor, release)
    if preview["actor_uid"] is None:
        if not args.register_actor:
            raise CommandFailure("conflict", "human actor is not registered; explicitly use --register-actor")
        _describe_messages(terminal, preview)
        terminal.write("Register this local actor's first contact. Its UID is allocated only after approval.\n"
                       "Registration alone does not allow owner messages.\n")
        _approve(terminal, "REGISTER")
        with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                                expected_release=preview["release"]) as release:
            _unchanged(preview, _message_preview(paths, store, args.target_fleet, args.actor, release))
            resolve_task_mutation_context(root=paths.root, fleet=preview["fleet"],
                                          operator_alias=args.actor, package=paths.package)
            registered = _message_preview(paths, store, args.target_fleet, args.actor, release)
            if registered["actor_uid"] is None:
                raise CommandFailure("conflict", "approved actor registration is unavailable")
            _unchanged(preview, {**registered, "actor_uid": None})
            preview = registered
    _describe_messages(terminal, preview)
    terminal.write("Allow ordinary messages only as this actor in this fleet.\n"
                   "This grants no task mutations, replies or permission decisions.\n")
    _approve(terminal, "ALLOW")
    with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                            expected_release=preview["release"]) as release:
        _unchanged(preview, _message_preview(paths, store, args.target_fleet, args.actor, release))
        ctx = resolve_task_context(root=paths.root, fleet=preview["fleet"],
                                   operator_alias=args.actor, package=paths.package)
        if (ctx.host_uid != preview["owner"].host_uid or ctx.fleet_uid != preview["bindings"]["fleet_uid"]
                or ctx.caller.uid != preview["actor_uid"] or ctx.caller.alias != preview["actor_alias"]):
            raise CommandFailure("conflict", "approved message identities changed")
        grant = store.allow_messages(expected_owner=preview["owner"], fleet_uid=ctx.fleet_uid,
                                    actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    return CommandOutput({"state": "allowed", "grant": asdict(grant)},
                         lines=("Owner ordinary messages allowed for this actor and fleet.",))


def _revoke_messages(args, store, terminal):
    owner = _active_owner(store)
    grant = store.current_message_grant(expected_owner=owner, fleet_uid=args.fleet_uid)
    _describe(terminal, owner)
    _describe_binding(terminal, grant.fleet_uid, grant.actor_alias, grant.actor_uid)
    terminal.write("Revoke only this retained ordinary-message grant. Owner read access remains.\n")
    _approve(terminal, "REVOKE-MESSAGES")
    if store.current_message_grant(expected_owner=owner, fleet_uid=args.fleet_uid) != grant:
        raise CommandFailure("conflict", "displayed message grant changed; inspect and retry")
    store.revoke_messages(expected_owner=owner, fleet_uid=grant.fleet_uid, expected_grant=grant)
    return CommandOutput({"state": "revoked", "fleet_uid": grant.fleet_uid},
                         lines=("Owner ordinary-message grant revoked. Read access remains.",))


@contextmanager
def _message_errors():
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
        raise CommandFailure(exc.code, "owner message identities could not be bound; verify the active fleet and actor") from exc
    except ActivationError as exc:
        raise CommandFailure("conflict", "active configuration is unavailable; verify the selected installation") from exc
    except AccessDenied as exc:
        if exc.code == "invalid_message_binding":
            raise CommandFailure("invalid_argument", "message grants require canonical fleet and human actor bindings") from exc
        if exc.code == "messages_not_allowed":
            raise CommandFailure("conflict", "no retained message grant for this fleet; inspect host owner status") from exc
        if exc.code == "message_binding_changed":
            raise CommandFailure("conflict", "message grant changed or already belongs to another actor; inspect host owner status "
                                 "and revoke the retained grant before approving a replacement") from exc
        if exc.code == "grant_changed":
            raise CommandFailure("conflict", "owner pairing changed; inspect host owner status before retrying") from exc
        raise
    except (PendingMigrationError, DowngradeError, sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", "message grant could not be bound or persisted; inspect local authority, "
                             "active configuration and Plane storage") from exc


def dispatch(args):
    from ..context import resolve_paths
    from ..paths import InvalidPathSelector
    from ..plane.ids import read_host_uid

    if any(key in os.environ for key in _GENERATED):
        raise CommandFailure("conflict", "owner commands require an operator shell without bot or fleet selectors")
    if args.root is None or args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "owner commands require explicit --root and no fleet or seed")
    if args.owner_action != "status" and args.json:
        raise CommandFailure("invalid_argument", "--json is supported for owner status only")
    try:
        paths = resolve_paths(root=args.root)
        store = OwnerAccess(paths.root)
        if args.owner_action == "serve":
            from ..plane.owner_server import OwnerServerConfigurationError, OwnerServerStartupError, serve

            try:
                serve(paths.root, origin=args.origin, tailscale_binary=args.tailscale,
                      socket_path=args.socket,
                      package=paths.package)
            except OwnerServerStartupError as exc:
                raise CommandFailure("unavailable", "owner server startup failed; inspect local server logs") from exc
            except OwnerServerConfigurationError as exc:
                raise CommandFailure("unavailable", str(exc)) from exc
            except ImportError as exc:
                raise CommandFailure("unavailable", "owner serving requires the optional [plane-ui] dependencies") from exc
            except OSError as exc:
                raise CommandFailure("unavailable", "owner server failed; inspect local logs and configured resources") from exc
            return CommandOutput({"state": "stopped"}, lines=("Owner server stopped.",))
        if args.owner_action == "status":
            grant, messages = store.local_status()
            state = "unpaired" if grant is None else "paired" if grant.active else "revoked"
            lines = [f"Owner access: {state}."]
            for message in messages:
                lines.append("Message grant: " + json.dumps(asdict(message), ensure_ascii=True))
            return CommandOutput({"state": state, "owner": asdict(grant) if grant else None,
                                  "message_grants": [asdict(message) for message in messages]},
                                 lines=tuple(lines))
        with _terminal() as terminal:
            if args.owner_action in {"allow-messages", "revoke-messages"}:
                with _message_errors():
                    if args.owner_action == "allow-messages":
                        return _allow_messages(args, paths, store, terminal)
                    return _revoke_messages(args, store, terminal)
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
    except SourceNeedsBinding as exc:
        raise CommandFailure("unavailable", "Plane source lacks required binding or indexes; verify the selected installation "
            "and re-run host owner bind-source. Binding refuses foreign or mixed-host history.") from exc
    except SourceDenied as exc:
        raise CommandFailure("conflict", "Plane source is foreign or mixed-host; select the correct installation "
            "or investigate its history. Do not rebind this source.") from exc
    except SourceUnavailable as exc:
        raise CommandFailure("unavailable", "Plane source cannot be verified; inspect the selected installation's "
            "database and schema locally. Pairing again cannot repair source state.") from exc
    except AccessDenied as exc:
        raise CommandFailure("conflict", "owner request is no longer valid; inspect status and request pairing again") from exc
    except (AccessUnavailable, ValueError) as exc:
        raise CommandFailure("unavailable", "owner authority is unavailable; verify the selected installation") from exc
