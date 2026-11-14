"""Finite local preparation using existing authority and foreground serving owners.

No stored workflow, automatic approval, native enrollment or activation. The
browser retains its explicit plan/launch/receipt protocol; the terminal stays open.
"""
from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
import hashlib
import os
from pathlib import Path
import socket
import signal
import shlex
import subprocess
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from ..command_result import CommandFailure, CommandOutput
from ..plane.owner_access import AccessDenied, AccessUnavailable
from ..plane.owner_setup import OwnerSetup


def _native_directories(paths):
    from ..supervision_inventory import Adapter, _catalog
    return tuple(_catalog(Adapter(paths.package).read("svc_inventory_catalog"))[2])


def _default_install_directory(directories):
    """Offer persistent user units only when the native catalog includes them."""
    if sys.platform == "darwin":
        directory = Path.home() / "Library/LaunchAgents"
    elif sys.platform.startswith("linux"):
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not config_home.is_absolute():
            return None
        directory = config_home / "systemd/user"
    else:
        return None
    return directory if directory in directories else None


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


def _owner_policy_content(before, owner):
    """Replace only the approved YAML entry; preserve unrelated source bytes."""
    import yaml
    text = before.decode("utf-8") if before else ""
    tree = yaml.compose(text)
    def indented(value, indent):
        return "".join(" "*indent+line for line in yaml.safe_dump(value, sort_keys=False, allow_unicode=True).splitlines(True))
    if tree is None:
        return (text + ("\n" if text and not text.endswith("\n") else "")
                + "host:\n  jobs:\n" + indented({"plane-owner": owner}, 4)).encode()
    def pair(node, name):
        return next(((key,value) for key,value in node.value if key.value == name), None)
    def block(node):
        return isinstance(node, yaml.MappingNode) and not node.flow_style
    def last_token(node):
        if isinstance(node, yaml.MappingNode):
            return max((last_token(child) for entry in node.value for child in entry), default=node.end_mark.index)
        if isinstance(node, yaml.SequenceNode):
            return max((last_token(child) for child in node.value), default=node.end_mark.index)
        return node.end_mark.index
    def replace_entry(key, node, value):
        start = key.start_mark.index-key.start_mark.column
        end = last_token(node)
        if end < key.start_mark.index:
            raise CommandFailure("invalid_argument", "aliased owner policy must be made explicit locally")
        if end and text[end-1:end] != "\n":
            newline = text.find("\n",end); end = len(text) if newline < 0 else newline+1
        return (text[:start]+indented(value,key.start_mark.column)+text[end:]).encode()
    host_pair = pair(tree,"host")
    if host_pair is None:
        return (text[:tree.start_mark.index]+"host:\n  jobs:\n"+indented({"plane-owner":owner},4)+text[tree.end_mark.index:]).encode()
    host_key,host = host_pair
    if not block(host):
        if host.value in (None,"",[]) or host.value == "null":
            return replace_entry(host_key,host,{"host":{"jobs":{"plane-owner":owner}}})
        raise CommandFailure("invalid_argument", "managed owner requires a block-style private host policy")
    jobs_pair = pair(host,"jobs")
    if jobs_pair is None:
        at = host.end_mark.index-host.end_mark.column
        return (text[:at]+indented({"jobs":{"plane-owner":owner}},host_key.start_mark.column+2)+text[at:]).encode()
    jobs_key,jobs = jobs_pair
    if not block(jobs):
        if jobs.value in (None,"",[]) or jobs.value == "null":
            return replace_entry(jobs_key,jobs,{"jobs":{"plane-owner":owner}})
        raise CommandFailure("invalid_argument", "managed owner requires block-style host jobs")
    selected = pair(jobs,"plane-owner")
    if selected:
        return replace_entry(*selected,{"plane-owner":owner})
    at = jobs.end_mark.index-jobs.end_mark.column
    return (text[:at]+indented({"plane-owner":owner},jobs_key.start_mark.column+2)+text[at:]).encode()


def _owner_override_plan(root, config):
    """Plan one locally approved private override; never edit the shared default."""
    import yaml
    from ..config import HOST_OVERRIDE_ENV, host_override_path, load_host_jobs
    from ..plane.owner_setup import private_owner_override_parent
    path = host_override_path()
    if (not os.environ.get(HOST_OVERRIDE_ENV) or not path.is_absolute() or path.resolve() != path
            or path == Path.home()/".config/claudlobby/system.yaml"
            or path.is_relative_to(root/"state") or path.parent.stat().st_uid != os.getuid()):
        raise CommandFailure("invalid_argument", "managed connection requires an explicit owned private host override")
    try:
        private_owner_override_parent(path)
    except AccessDenied as exc:
        raise CommandFailure("invalid_argument", "host override requires a private owned directory and trusted ancestors") from exc
    before, identity = None, None
    if os.path.lexists(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 1048576):
            raise CommandFailure("conflict", "private host override is redirected or not exclusively owned")
        before = path.read_bytes()
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    # Validate all existing policy, preserving every unrelated job and prefix.
    existing = load_host_jobs()["plane-owner"]
    if existing.get("unit") != "service" or existing.get("script") != "$CLAUDLOBBY_CLI":
        raise CommandFailure("conflict", "configured owner execution is not canonical")
    if any(existing.get(key) is not None and existing.get(key) != selected for key, selected in config.items()):
        raise CommandFailure("conflict", "configured owner belongs to different ingress; no policy was replaced")
    document = yaml.safe_load(before) if before else {}
    document = document or {}
    host = document.get("host") or {}; document["host"] = host
    jobs = host.get("jobs") or {}; host["jobs"] = jobs
    owner = dict(jobs.get("plane-owner") or {})
    owner.update(config)
    owner["enroll"] = True
    jobs["plane-owner"] = owner
    content = _owner_policy_content(before, owner)
    try:
        valid = yaml.safe_load(content) == document
    except yaml.YAMLError:
        valid = False
    if not valid:
        raise CommandFailure("conflict", "owner policy cannot preserve unrelated YAML bindings; no file changed")
    return {"binding": {"override": str(path), "sha256": hashlib.sha256(content).hexdigest(), "config": dict(config)},
            "before": before, "identity": identity, "content": content}


def _commit_owner_override(plan):
    """Compare before publishing this consented authoring change, not runtime output."""
    path = Path(plan["binding"]["override"])
    from ..plane.owner_setup import private_owner_override_parent
    private_owner_override_parent(path)
    if plan["before"] is None:
        unchanged = not os.path.lexists(path)
    else:
        info = path.lstat()
        unchanged = (not path.is_symlink() and info.st_nlink == 1
                     and (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns) == plan["identity"]
                     and path.read_bytes() == plan["before"])
    if not unchanged:
        raise CommandFailure("conflict", "private host policy changed after approval; no replacement attempted")
    fd, name = tempfile.mkstemp(prefix=".owner-policy-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(plan["content"]); stream.flush(); os.fchmod(stream.fileno(), 0o600); os.fsync(stream.fileno())
        if plan["before"] is None:
            os.link(temporary, path)  # Create-only, never overwrite an unexpected node.
        else:
            info = path.lstat()
            if ((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns) != plan["identity"]
                    or path.is_symlink() or info.st_nlink != 1 or path.read_bytes() != plan["before"]):
                raise CommandFailure("conflict", "private host policy changed before publication")
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _configuration(args, paths, terminal, cli):
    from ..plane.owner_service import service_command
    from .setup import _template_config, _local_project, _account_directory
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
        service_command(paths.root, cli, config)
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "local serving configuration is invalid; review exact HTTPS origins and absolute native paths") from exc
    fleet = _input(terminal, args.target_fleet, "First team name", "web-app")
    template = getattr(args, "template", "revenue-web-app")
    project = getattr(args, "project_directory", None)
    repository = args.repository
    if template == "local-project":
        project = _input(terminal, project, "Approved existing local project directory (read/write)")
    else:
        repository = _input(terminal, repository, "Approved existing OWNER/REPO")
    account = getattr(args, "account_directory", None)
    if template == "local-project" and account is None:
        account = os.environ.get("CLAUDE_CONFIG_DIR") or None
    if account is not None:
        account = str(_account_directory(account))
    _template_config(template, fleet, repository, "Validate selected project scope", project_directory=project,
                     **({"account_directory": account} if account is not None else {}))
    directories = _native_directories(paths)
    if not directories:
        raise CommandFailure("unavailable", "native user manager has no installation directory; finish native user setup first")
    default = _default_install_directory(directories)
    if default is None:
        terminal.write("Native installation choices: " + json.dumps([str(p) for p in directories]) + "\n")
    directory = Path(_input(terminal, args.install_directory, "Native installation directory", default))
    if directory not in directories:
        raise CommandFailure("invalid_argument", "setup directory is not an observed native search path")
    if template == "local-project":
        _local_project(project, forbidden=(paths.root, Path(cli).parent.parent, directory))
    terminal.write("Local setup configuration (no credentials): " + json.dumps({**config,
        "fleet": fleet, "template": template, "repository": repository, "project_directory": project,
        "account_directory": account, "install_directory": str(directory)}, ensure_ascii=True) + "\n")
    if getattr(args,"provider_signin",None) is not None:
        terminal.write("Claude sign-in can be completed in the connected setup after separate local approval. Project/repository access, website login and native Tailscale/Serve access must already be ready.\n")
    else:
        terminal.write(("Provider login and local project read/write approval" if template == "local-project" else "Provider/repository login") + ", website login and native Tailscale/Serve access must already be ready.\n"
                       "This guide does not configure those prerequisites.\n")
    terminal.write("Managed owner-service enrollment will require explicit local approval and the original browser Launch.\n"
                   if getattr(args, "managed_owner", False) else "Owner serving stays foreground-only; no owner service is enrolled.\n")
    provider=None
    if getattr(args,"provider_signin",None) is not None:
        from ..plane.provider_signin import approved_scope, SigninRefused
        try: provider=approved_scope(args.provider_signin,paths.root,account)
        except (SigninRefused,OSError):
            raise CommandFailure("invalid_argument","Claude sign-in requires the reviewed provider artifact and private account/data paths; existing token setup remains available") from None
    plan = _owner_override_plan(paths.root, config) if getattr(args, "managed_owner", False) else None
    return config, SimpleNamespace(template=template, target_fleet=fleet,
        repository=repository, project_directory=project, account_directory=account, install_directory=directory,
        managed_owner=plan["binding"] if plan else None, managed_owner_plan=plan, provider_signin=provider)


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
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, start_new_session=True)
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
                print("Stopping local owner serving; waiting for already-started work to finish. Keep this terminal open.", file=sys.stderr)
                process.terminate()
            # The canonical app drains admitted synchronous work. Never kill it
            # on an invented deadline or repeated terminal interrupt.
            while True:
                try:
                    process.wait()
                    break
                except KeyboardInterrupt:
                    continue


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
    from ..active_config import read_current_config
    from ..git_update import read_update
    from ..plane.owner_setup import managed_setup, _continuation
    from ..config import load_host_jobs
    from ..plane.owner_service import service_command
    from ..plane.owner_source import fresh_owner_setup

    retained = OwnerSetup(store).retained_state() if os.path.lexists(store.path) else None
    if retained and retained.state == "legacy_recovery_required":
        raise CommandFailure("conflict", "historical sealed setup requires local recovery; no Git operation was inferred",
                             data=asdict(retained))
    package = managed_setup(paths.root)
    installation = package.installation
    record = read_update(installation)
    selected = os.path.lexists(paths.root / "state/config.json")
    if record is not None:
        if (not retained or retained.grant is None or record.get("request_id") != retained.launch_id
                or record.get("config_fingerprint") != retained.launch_plan or record.get("phase") != "done"):
            raise CommandFailure("conflict", "a first-team operation is retained; inspect its original receipt and continue locally",
                data={"request_id":record["request_id"]},
                hint=_continuation(paths.root,retained.grant) if retained and retained.grant else "host update status")
        snapshot=read_current_config(paths.root,installation=installation,revision=record["target"])
        if snapshot!=record.get("candidate_config"):
            raise CommandFailure("conflict", "current configuration differs from the original setup")
    elif selected or retained and retained.launch_id is not None:
        raise CommandFailure("conflict", "guided setup cannot replace existing or unknown installation state")
    if not selected:
        fresh_owner_setup(paths.root, allow_absent=True)
    cli = installation.cli
    config, approval = _configuration(args, paths, terminal, cli)
    if load_host_jobs()["plane-owner"].get("enroll", True) and approval.managed_owner is None:
        raise CommandFailure("conflict", "configured owner enrollment requires explicit --managed-owner handover")
    # No authority method is reached before the terminal's ancestry check.
    if not os.path.lexists(store.path):
        console._initialize_first_owner(paths, terminal)
    owner = store.current_grant()
    if owner is None or not owner.active:
        direct = {**config, "workspace_issuer": None, "workspace_origin": None}
        terminal.write("Open " + json.dumps(config["origin"] + "/owner") + " and choose Start pairing.\n")
        with _serving(service_command(paths.root, cli, direct), Path(config["socket"]), config["origin"]):
            console._confirm_owner(paths, store, terminal)
    owner = console._active_owner(store)
    console._describe(terminal, owner)
    if not selected and store.connected().preparation_state() != "ready":
        console._initialize_connected(store, terminal, prepare_workspace=True)
    with _serving(service_command(paths.root, cli, config), Path(config["socket"]), config["origin"]) as child:
        if not selected:
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
            # A browser may retain this invocation's approved Launch as soon as
            # serving is ready. Entry checks already refused pre-existing work;
            # compare the same grant below, then use the normal handoff loop.
            if retained.state == "allowed":
                grant = retained.grant
                if (grant.template, grant.project_directory, grant.account_directory, grant.managed_owner, grant.provider_signin, grant.fleet, grant.repository, grant.installation, grant.source_revision, grant.install_directory) != (
                        approval.template, approval.project_directory, approval.account_directory, approval.managed_owner, approval.provider_signin, approval.target_fleet, approval.repository, str(installation.directory), package.source_revision, str(approval.install_directory)):
                    raise CommandFailure("conflict", "retained setup approval differs; no grant was replaced")
                terminal.write("Matching setup approval retained without renewal.\n")
            else:
                approval.install_directory.mkdir(parents=True, exist_ok=True)
                console._allow_setup(approval, paths, store, terminal, expected_retained=retained)
        terminal.write("Local access prepared. Review and launch the exact prepared request in your browser; keep original receipt IDs.\n"
                       "Keep this terminal open through Preview. After Launch is retained, the guide drains and exits so the updater can proceed.\n")
        terminal.flush()
        setup = OwnerSetup(store)
        approved = setup.local() if not selected else None
        try:
            while True:
                if approved is not None:
                    retained = setup.retained_state()
                    if retained.launch_id is not None:
                        if retained.grant != approved:
                            raise CommandFailure("conflict", "setup approval changed before handoff; inspect the original receipt")
                        record = setup.operation(approved)
                        if (record is None or record["request_id"] != retained.launch_id
                                or record["config_fingerprint"] != retained.launch_plan):
                            raise CommandFailure("conflict", "original setup operation changed before handoff")
                        # Returning leaves _serving through its normal drain. Both
                        # the child and this guide must exit to release reader
                        # leases; never let imported owner code survive cutover.
                        status = shlex.join([str(cli), "--root", str(paths.root), "host", "update", "status", "--json"])
                        reconnect = ("The approved managed connection will return at the same address; keep the original browser view open."
                                     if approved.managed_owner else "After DONE, reconnect without another setup: " + shlex.join(service_command(paths.root, cli, config)))
                        return CommandOutput({"state": "launch_handoff", "request_id": retained.launch_id,
                            "config_fingerprint": retained.launch_plan, "generation": approved.generation,
                            "source_revision": approved.source_revision}, lines=(
                                "Original Launch retained. Local owner serving drained; this guide exits so the updater can proceed.",
                                "Inspect the original operation: " + status,
                                "If interrupted, continue that same operation locally; do not launch again.",
                                reconnect))
                try:
                    child.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            return CommandOutput({"state": "serving_stopped"}, lines=("Local foreground serving stopped; shutdown did not change approvals or stop the fleet.",))
        raise CommandFailure("unavailable", "local foreground owner stopped unexpectedly; no server or launch was retried")
