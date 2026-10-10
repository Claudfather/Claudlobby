"""Explicit local-console owner administration, separate from browser authority.

An operator terminal is a usability/accident boundary, not same-UID isolation.
Processes with the operator's privileges can edit the same authority files.
These doors never grant website membership or permission to send bot messages.
"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import io
import json
import os
import sys
import warnings

from ..command_result import CommandFailure, CommandOutput
from ..plane.owner_access import AccessDenied, AccessUnavailable, OwnerAccess

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
    terminal.write(f"Type {word} to continue: ")
    terminal.flush()
    if terminal.readline(32).strip() != word:
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
            from ..plane.owner_server import OwnerServerConfigurationError, serve

            try:
                serve(paths.root, origin=args.origin, tailscale_binary=args.tailscale,
                      socket_path=args.socket,
                      package=paths.package)
            except OwnerServerConfigurationError as exc:
                raise CommandFailure("unavailable", str(exc)) from exc
            except ImportError as exc:
                raise CommandFailure("unavailable", "owner serving requires the optional [plane-ui] dependencies") from exc
            except OSError as exc:
                raise CommandFailure("unavailable", "owner server failed; inspect local logs and configured resources") from exc
            return CommandOutput({"state": "stopped"}, lines=("Owner server stopped.",))
        if args.owner_action == "status":
            grant = store.current_grant()
            state = "unpaired" if grant is None else "paired" if grant.active else "revoked"
            return CommandOutput({"state": state, "owner": asdict(grant) if grant else None},
                                 lines=(f"Owner access: {state}.",))
        with _terminal() as terminal:
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
    except AccessDenied as exc:
        raise CommandFailure("conflict", "owner request is no longer valid; inspect status and request pairing again") from exc
    except (AccessUnavailable, ValueError) as exc:
        raise CommandFailure("unavailable", "owner authority is unavailable; verify the selected installation") from exc
