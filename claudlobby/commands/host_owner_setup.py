"""Finite local preparation using existing authority and foreground serving owners.

No stored workflow, automatic approval, native enrollment or activation. The
browser retains its explicit plan/launch/receipt protocol; the terminal stays open.
"""
from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from ..command_result import CommandFailure, CommandOutput
from ..plane.owner_access import AccessUnavailable
from ..plane.owner_setup import OwnerSetup


def _native_directories(paths):
    from ..supervision_inventory import Adapter, _catalog
    return tuple(_catalog(Adapter(paths.package).read("svc_inventory_catalog"))[2])


def _input(terminal, value, label, default=None, *, optional=False):
    if value is None:
        terminal.write(label + (" [" + json.dumps(str(default), ensure_ascii=True) + "]" if default not in (None, "") else "") + ": ")
        terminal.flush()
        value = terminal.readline(4098)
        if not value or not value.endswith("\n"):
            raise CommandFailure("conflict", "local setup input did not complete")
        value = value.rstrip("\r\n") or default
    if optional and value == "":
        return None
    if value is None or not str(value) or len(str(value)) > 2048 or not str(value).isprintable():
        raise CommandFailure("invalid_argument", "local setup requires explicit printable configuration")
    return str(value)


def _configuration(args, paths, terminal, release):
    from ..plane.owner_service import service_command
    from .setup import _template_config
    config = {
        "origin": _input(terminal, args.origin, "This computer's exact private HTTPS origin"),
        "tailscale": _input(terminal, args.tailscale, "Absolute native Tailscale executable"),
        "workspace_issuer": _input(terminal, args.workspace_issuer, "Exact website issuer"),
        "workspace_origin": _input(terminal, args.workspace_origin, "Exact operational browser origin"),
        "socket": _input(terminal, args.socket, "Private owner socket", str(paths.root / "state/owner/owner.sock")),
        "tailscale_socket": _input(terminal, args.tailscale_socket,
            "Native Tailscale control socket (blank uses its native default)", "", optional=True),
    }
    # The existing service validator validates paths/origins without enrolling it.
    try:
        service_command(paths.root, release.cli_path, config)
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "local serving configuration is invalid; review exact HTTPS origins and absolute native paths") from exc
    fleet = _input(terminal, args.target_fleet, "First team name", "web-app")
    repository = _input(terminal, args.repository, "Approved existing OWNER/REPO")
    _template_config("revenue-web-app", fleet, repository, "Validate selected repository scope")
    directories = _native_directories(paths)
    if not directories:
        raise CommandFailure("unavailable", "native user manager has no installation directory; finish native user setup first")
    default = str(directories[0]) if len(directories) == 1 else None
    if default is None:
        terminal.write("Native installation choices: " + json.dumps([str(p) for p in directories]) + "\n")
    directory = Path(_input(terminal, args.install_directory, "Native installation directory", default))
    if directory not in directories:
        raise CommandFailure("invalid_argument", "setup directory is not an observed native search path")
    terminal.write("Local setup configuration (no credentials): " + json.dumps({**config,
        "fleet": fleet, "repository": repository, "install_directory": str(directory)}, ensure_ascii=True) + "\n")
    terminal.write("Provider/repository login, website login and native Tailscale/Serve access must already be ready.\n"
                   "This guide does not configure them or enroll an owner service.\n")
    return config, SimpleNamespace(template="revenue-web-app", target_fleet=fleet,
        repository=repository, release=release.release_id, install_directory=directory)


def _ready(path, origin):
    connection = http.client.HTTPConnection("localhost", timeout=0.2)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(0.2)
        connection.sock.connect(str(path))
        connection.request("GET", "/owner", headers={"Host": urlsplit(origin).netloc})
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


@contextmanager
def _serving(command, path, origin):
    from ..plane.owner_server import check_listener_available
    # Never take over an unrelated listener; canonical serve owns socket cleanup.
    if path.parent.exists() or path.parent.is_symlink():
        check_listener_available(path)
    with tempfile.TemporaryFile() as log:
        # Preserve the canonical operator-facing failure; never forward it into JSON.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log)
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if _ready(path, origin):
                    yield process
                    return
                time.sleep(0.05)
            raise CommandFailure("unavailable", "local owner server did not become ready; inspect serving configuration and private socket")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CommandFailure("unavailable", "local owner server did not drain; inspect the retained local state before continuing")


def _original_launch(state):
    raise CommandFailure("conflict", "a first-team launch is already retained; inspect its original receipt before continuing",
                         data={"launch_id": state.launch_id, "launch_plan": state.launch_plan,
                               "generation": state.grant.generation})


@contextmanager
def interrupted():
    """A direct SIGTERM follows the same owned cleanup as terminal cancellation."""
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(args, paths, store, terminal):
    from . import host_owner as console
    from .host import _status
    from .releases import _executing_release, _release
    from ..activation_state import read_selection
    from ..config import load_host_jobs
    from ..plane.owner_service import service_command
    from ..plane.owner_source import fresh_owner_setup

    retained = OwnerSetup(store).retained_state() if os.path.lexists(store.path) else None
    selected = read_selection(paths.root)
    if retained and retained.launch_id is not None:
        if selected is None:
            _original_launch(retained)
        status = _status(SimpleNamespace(), paths.root)  # refuses unfinished/indeterminate state
        if (status.data["state"] != "active" or selected["activation_id"] != retained.launch_id
                or selected["plan_id"] != retained.launch_plan):
            _original_launch(retained)
    elif selected is not None:
        raise CommandFailure("conflict", "guided setup cannot replace an existing selected installation")
    if selected is None:
        fresh_owner_setup(paths.root, allow_absent=True)
    release_id = _executing_release(paths.root)
    if release_id is None:
        raise CommandFailure("release_mismatch", "guided setup requires the exact assembled sealed CLI")
    release = _release(paths.root, release_id)
    if load_host_jobs()["plane-owner"].get("enroll", True):
        raise CommandFailure("conflict", "guided setup retains foreground serving; configured owner service enrollment requires separate reviewed handover")
    if selected is not None and (selected["release_id"] != retained.grant.release_id or release_id != selected["release_id"]):
        raise CommandFailure("release_mismatch", "serving continuation requires the original selected release")
    config, approval = _configuration(args, paths, terminal, release)
    # No authority method is reached before the terminal's ancestry check.
    if not os.path.lexists(store.path):
        console._initialize_first_owner(paths, terminal)
    owner = store.current_grant()
    if owner is None or not owner.active:
        direct = {**config, "workspace_issuer": None, "workspace_origin": None}
        terminal.write("Open " + json.dumps(config["origin"] + "/owner") + " and choose Start pairing.\n")
        with _serving(service_command(paths.root, release.cli_path, direct), Path(config["socket"]), config["origin"]):
            console._confirm_owner(paths, store, terminal)
    owner = console._active_owner(store)
    console._describe(terminal, owner)
    if selected is None and store.connected().preparation_state() != "ready":
        console._initialize_connected(store, terminal, prepare_workspace=True)
    with _serving(service_command(paths.root, release.cli_path, config), Path(config["socket"]), config["origin"]) as child:
        if selected is None:
            workspace_state, binding = store.workspace().status()
            if workspace_state == "absent":
                terminal.write("In your signed-in browser choose Prepare local approval for this computer, then paste its code here.\n")
                console._confirm_workspace(store, terminal, owner)
                workspace_state, binding = store.workspace().status()
            if (workspace_state != "active" or binding.issuer != config["workspace_issuer"]
                    or binding.operational_origin != config["workspace_origin"]):
                raise CommandFailure("conflict", "retained workspace differs or is inactive; inspect workspace-status; no binding was replaced")
            terminal.write("Retained workspace: " + json.dumps(asdict(binding), ensure_ascii=True) + "\n")
            retained = OwnerSetup(store).retained_state()
            if retained.launch_id is not None:
                _original_launch(retained)
            if retained.state == "allowed":
                grant = retained.grant
                if (grant.fleet, grant.repository, grant.release_id, grant.release_seal, grant.install_directory) != (
                        approval.target_fleet, approval.repository, release_id, release.seal_sha256, str(approval.install_directory)):
                    raise CommandFailure("conflict", "retained setup approval differs; no grant was replaced")
                terminal.write("Matching setup approval retained without renewal.\n")
            else:
                approval.install_directory.mkdir(parents=True, exist_ok=True)
                console._allow_setup(approval, paths, store, terminal, expected_retained=retained)
        terminal.write("Local access prepared. Review and launch the exact plan in your browser; keep original receipt IDs.\n"
                       "Keep this terminal open. Closing it disconnects browser access, not started bots or approvals.\n")
        terminal.flush()
        try:
            child.wait()
        except KeyboardInterrupt:
            return CommandOutput({"state": "serving_stopped"}, lines=("Local foreground serving stopped; shutdown did not change approvals or stop the fleet.",))
        raise CommandFailure("unavailable", "local foreground owner stopped unexpectedly; no server or launch was retried")
