"""Finite local preparation using existing authority and foreground serving owners.

No stored workflow, automatic approval, native enrollment or activation, with one
exception: after a Launch whose team card was approved with the messages mark, the
finish may run later on the stored card, with no one present (``_unattended_finish``
in ``host_owner``). The browser retains its explicit plan/launch/receipt protocol;
the terminal stays open.
"""
from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
import hashlib
import os
from pathlib import Path
import platform
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

#: The fixed workspace issuer and operational origin of both typed guides.
_WORKSPACE_ORIGINS = ("https://claudfather.ai", "https://plane.claudfather.ai")
#: The native Tailscale the typed Linux guides admit.
LINUX_TAILSCALE = Path("/usr/bin/tailscale")
#: How long a started computer's lasting service has to answer; it is already running.
READY_SECONDS = 15
#: Core's existing line for an owner listener that did not answer.
_NOT_READY = "local owner server did not become ready; inspect serving configuration and private socket"


def _say(terminal, text):
    if terminal is not None:
        terminal.write(text)


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


def _serving_configuration(args, paths, terminal, cli):
    """The owner listener's exact settings, validated by the service's own builder."""
    from ..plane.owner_service import service_command
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
    return config


def _configuration(args, paths, terminal, cli):
    from .setup import _template_config, _local_project, _account_directory
    config = _serving_configuration(args, paths, terminal, cli)
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
        _say(terminal, "Native installation choices: " + json.dumps([str(p) for p in directories]) + "\n")
    directory = Path(_input(terminal, args.install_directory, "Native installation directory", default))
    if directory not in directories:
        raise CommandFailure("invalid_argument", "setup directory is not an observed native search path")
    if template == "local-project":
        _local_project(project, forbidden=(paths.root, Path(cli).parent.parent, directory))
    _say(terminal, "Local setup configuration (no credentials): " + json.dumps({**config,
        "fleet": fleet, "template": template, "repository": repository, "project_directory": project,
        "account_directory": account, "install_directory": str(directory)}, ensure_ascii=True) + "\n")
    if getattr(args,"provider_signin",None) is not None:
        _say(terminal, "Claude sign-in can be completed in the connected setup after separate local approval. Project/repository access, website login and native Tailscale/Serve access must already be ready.\n")
    else:
        _say(terminal, ("Provider login and local project read/write approval" if template == "local-project" else "Provider/repository login") + ", website login and native Tailscale/Serve access must already be ready.\n"
                       "This guide does not configure those prerequisites.\n")
    _say(terminal, "Managed owner-service enrollment will require explicit local approval and the original browser Launch.\n"
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
def _serving(command, path, origin, *, consent=None):
    from ..plane.owner_server import check_listener_available
    # Never take over an unrelated listener; canonical serve owns socket cleanup.
    if path.parent.exists() or path.parent.is_symlink():
        check_listener_available(path)
    with tempfile.TemporaryFile() as log:
        reader = writer = None
        if consent is not None:
            reader, writer = os.pipe()
            command = [*command, "--_parent-liveness-fd", str(reader)]
        # Preserve the canonical operator-facing failure; never forward it into JSON.
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, start_new_session=True,
                **({"pass_fds": (reader,), "stderr": log} if consent is not None else {}))
        except BaseException:
            if writer is not None:
                os.close(writer)
            raise
        finally:
            if reader is not None:
                os.close(reader)
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if consent is not None:
                    consent.check_cancel()
                if _ready(path, origin):
                    yield process
                    return
                time.sleep(0.05)
            raise CommandFailure("unavailable", "local owner server did not become ready; inspect serving configuration and private socket")
        finally:
            if consent is not None:
                import threading
                from .owner_stdio import _deferred_stops, StdioRefused
                stopped = threading.Event()
                with _deferred_stops(stopped):
                    try:
                        if writer is not None:
                            os.close(writer)
                        if process.poll() is None:
                            process.terminate()
                    finally:
                        while True:
                            try:
                                process.wait()
                                break
                            except KeyboardInterrupt:
                                stopped.set()
                if stopped.is_set():
                    raise StdioRefused("interrupted_unknown")
            else:
                if process.poll() is None:
                    print("Stopping local owner serving; waiting for already-started work to finish. Keep this terminal open.", file=sys.stderr)
                    process.terminate()
                # Preserve terminal behavior; the canonical app drains work.
                while True:
                    try:
                        process.wait()
                        break
                    except KeyboardInterrupt:
                        continue


@contextmanager
def _ssh_waiting():
    """No listener: typed operations arrive on their own admitted SSH channels."""
    class Waiting:
        @staticmethod
        def wait(timeout):
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("owner-ssh-guide", timeout)
    yield Waiting()


def _ssh_origin(source):
    """The server's own logical authority key, bound to the captured destination."""
    import asyncio
    from ..plane.owner_ssh import MAC_TAILSCALE, SSHSessionVerifier
    from ..plane.owner_ingress import NativePrincipalResolver
    from .owner_stdio import StdioRefused
    try:
        verifier = SSHSessionVerifier(source, NativePrincipalResolver(tailscale_binary=MAC_TAILSCALE))
        async def inspect():
            return await asyncio.wait_for(verifier.origin(), 15)
        return asyncio.run(inspect())
    except (AccessDenied, AccessUnavailable, ValueError, OSError, asyncio.TimeoutError):
        raise StdioRefused("ssh_identity_prerequisite_missing") from None


def _operation_retained(root, retained, record):
    from ..plane.owner_setup import _continuation
    return CommandFailure("conflict", "a first-team operation is retained; inspect its original receipt and continue locally",
        data={"request_id":record["request_id"]},
        hint=_continuation(root,retained.grant) if retained and retained.grant else "host update status")


def _original_launch(state):
    raise CommandFailure("conflict", "a first-team launch is already retained; inspect its original receipt before continuing",
                         data={"launch_id": state.launch_id, "launch_plan": state.launch_plan,
                               "generation": state.grant.generation})


@contextmanager
def interrupted():
    """A direct SIGTERM follows the same owned cleanup as terminal cancellation."""
    def stop(_signal, _frame):
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# The setup finish for a team card approved with the messages mark (``grant.messages``).
# Its effect is fixed by the stored card, whoever starts it: messages on for that one team under
# the card's name (numbered if taken), and the launched goal sent to that team's lead once.
# Fixed: the goal's request id is derived from the Launch's, so a second finish finds the first send.
_GOAL_REQUEST_LABEL = "claudlobby:setup-goal:"


def goal_request_id(launch_request_id):
    """The setup goal's message request id, derived from the Launch's request id."""
    from uuid import NAMESPACE_URL, uuid5
    return str(uuid5(NAMESPACE_URL, _GOAL_REQUEST_LABEL + launch_request_id))


def numbered_name(base, taken):
    """The card's name if free, else the first free ``<base>-2`` .. ``<base>-9`` (the base cut to 38
    characters, a trailing hyphen removed, so the numbered name still passes the name rule), else None."""
    if base not in taken:
        return base
    stem = base[:38].rstrip("-")
    return next((f"{stem}-{n}" for n in range(2, 10) if f"{stem}-{n}" not in taken), None)


class _Halt(Exception):
    """A check before the goal's send failed: the goal is not sent, for ``reason``."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@contextmanager
def _finish_lock(root, request_id):
    """One finish at a time per Launch: an exclusive flock on a private file in ``state/``, released
    when the holder closes it or dies. Yields False, at once, when another process holds it."""
    from ..plane.owner_setup import _uuid
    import fcntl
    state = Path(root) / "state"
    if not _uuid(request_id) or state.resolve() != state.absolute():
        raise AccessUnavailable("setup finish lock is unavailable")
    fd = os.open(state / f"setup-finish-{request_id}.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise AccessUnavailable("setup finish lock is not a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


class _CardEffects:
    """The finish's reach beyond the authority database: names, registration, the lead, receipts, the send."""

    def __init__(self, paths, store, grant):
        self.paths, self.store, self.grant = paths, store, grant

    def _selected(self):
        from ..operation_context import resolve_operation_scope
        selected, origin = resolve_operation_scope(root=self.paths.root, fleet=self.grant.fleet,
                                                   package=self.paths.package)
        if origin is not None:
            raise CommandFailure("conflict", "the setup finish requires a local operator")
        return selected

    def taken(self):
        """Every short name already used here: recorded actors (people and bots) and this team's bots."""
        from contextlib import closing
        from ..plane.db import connect_ro, db_file
        from ..plane.identity import aliases_of_kind
        from ..plane.inventory import short_alias
        from ..plane.schema_state import require_current_schema
        with closing(connect_ro(db_file(self.paths.root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            names = {short_alias(alias) for alias in aliases_of_kind(conn, "actor")}
        return names | set(self._selected().fleet.bots)

    def register(self, alias, check):
        """Register the card's name (the card was the consent) and read the ids the finish binds.
        ``check()`` runs inside the registration's admission, just before the name is written."""
        from .host_owner import _grant_preview, _register_actor
        from ..runtime_admission import RuntimeIdentity, mutation_admission
        with mutation_admission(self.paths.root, identity=RuntimeIdentity.current()) as release:
            preview = _grant_preview(self.paths, self.store, self.grant.fleet, alias, release)
        if preview["actor_uid"] is None:
            preview = _register_actor(self.paths, self.store, self.grant.fleet, alias, preview, check)
        lead = self._selected().fleet.manager
        return {"owner": preview["owner"], "fleet_uid": preview["bindings"]["fleet_uid"],
                "actor_uid": preview["actor_uid"], "lead_uid": preview["bindings"]["bots"].get(lead)}

    def receipt(self, fleet_uid, request_id):
        """The retained request under ``request_id``, or None when none exists."""
        from ..request_queries import RequestNotFoundError, read_request
        try:
            return read_request(self.paths.root, fleet_uid, request_id)
        except RequestNotFoundError:
            return None

    def send(self, alias, request_id, text, check):
        from .message_write import send_to_lead
        send_to_lead(self.paths.root, package=self.paths.package, fleet=self.grant.fleet, alias=alias,
                     request_id=request_id, text=text, check=check)


def _preview_payload(conn, grant):
    row = conn.execute("SELECT payload FROM setup_previews WHERE generation=?", (grant.generation,)).fetchone()
    if row is None:
        raise AccessUnavailable("the launched Preview is not recorded")
    return json.loads(row["payload"])


def _keep(store, grant, value, *, key="messages"):
    """Write the finish's record (or, as ``finish``, who will finish) into the approval's Preview row,
    in its own transaction."""
    with store._connection(write=True) as conn:
        OwnerSetup._schema(conn)
        payload = _preview_payload(conn, grant)
        conn.execute("UPDATE setup_previews SET payload=? WHERE generation=?",
                     (json.dumps({**payload, key: value}), grant.generation))
    return value


_REASONS = {"revoked": "setup_withdrawn", "stale": "stale_binding"}


def _approval_stands(store, conn, grant):
    """Read-only: None while this approval stands for an active owner; else ``revoked`` or ``stale``."""
    from ..plane.owner_setup import setup_still_stands
    row = conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
    if row is None or OwnerSetup._grant(row) != grant:
        return "stale"
    standing = setup_still_stands(store, conn, row["active"], grant)
    if standing is not None:
        return "revoked" if standing == "revoked" else "stale"
    owner = store._grant(conn)
    return "stale" if owner is None or not owner.active else None


def _still_stands(store, grant):
    """A check that raises ``_Halt`` unless the approval still stands (read-only)."""
    def check():
        with store._connection() as conn:
            OwnerSetup._schema(conn)
            reason = _approval_stands(store, conn, grant)
        if reason is not None:
            raise _Halt(reason)
    return check


def _stands(store, conn, grant, value):
    """None while the approval stands, the owner is the one the finish bound, and (once granted) the
    grant row holds the recorded actor and name; else the reason: ``revoked`` or ``stale``."""
    reason = _approval_stands(store, conn, grant)
    if reason is not None:
        return reason
    if store._grant(conn).revision != value["owner_revision"]:
        return "stale"
    if value.get("state") == "allowed" and not _grant_holds(store, conn, value):
        return "revoked"
    return None


def _grant_holds(store, conn, value):
    """The message grant the finish wrote still exists, for the current owner, with its actor and name."""
    from ..plane.owner_access import OwnerAccess
    owner = store._grant(conn)
    if owner is None or not owner.active or owner.revision != value["owner_revision"]:
        return False
    held = (conn.execute("SELECT actor_uid, actor_alias FROM message_grants WHERE owner_revision=? AND fleet_uid=?",
                         (owner.revision, value["fleet_uid"])).fetchone()
            if OwnerAccess._has_grants(conn, "message") else None)
    return held is not None and (held["actor_uid"], held["actor_alias"]) == (value["actor_uid"], value["alias"])


def _admit(store, grant, ids, value):
    """Re-check and write in one immediate transaction: the grant and the record, or neither."""
    from ..plane.owner_access import OwnerAccess
    value = {**value, "fleet_uid": ids["fleet_uid"], "actor_uid": ids["actor_uid"],
             "lead_uid": ids["lead_uid"], "owner_revision": ids["owner"].revision}
    with store._connection(write=True) as conn:
        OwnerSetup._schema(conn)
        payload = _preview_payload(conn, grant)
        reason = _stands(store, conn, grant, value)
        if reason is None and store._grant(conn) != ids["owner"]:
            reason = "stale"
        held = (conn.execute("SELECT actor_uid, actor_alias FROM message_grants WHERE owner_revision=? AND fleet_uid=?",
                             (value["owner_revision"], value["fleet_uid"])).fetchone()
                if reason is None and OwnerAccess._has_grants(conn, "message") else None)
        if reason is not None:
            value.update(state=reason, reason=_REASONS[reason], goal={"state": "not_sent", "reason": reason})
        elif held is not None and (held["actor_uid"], held["actor_alias"]) != (value["actor_uid"], value["alias"]):
            # Approved at the terminal under another name: left as it is.
            value.update(state="kept", goal={"state": "not_sent", "reason": "other_grant"})
        else:
            store._allow_action_in(conn, "message", ids["owner"], value["fleet_uid"], value["actor_uid"], value["alias"])
            value.update(state="allowed", goal={"request_id": value["goal"]["request_id"], "state": "pending"}
                         if value["lead_uid"] is not None else {"state": "not_sent", "reason": "no_lead"})
        conn.execute("UPDATE setup_previews SET payload=? WHERE generation=?",
                     (json.dumps({**payload, "messages": value}), grant.generation))
    return value


def _sent(view, value, text):
    """``sent`` only for a receipt whose sender, recipient and body all match, with a submitted transport."""
    from ..message_payload import MessageBody
    from ..request_receipts import semantic_digest
    expected = semantic_digest({"body": MessageBody.from_input(text).text.encode("utf-8"), "kind": "chat"})
    if (view.operation != "message.send" or view.caller_uid != value["actor_uid"]
            or view.recipient_uid != value["lead_uid"] or view.semantic_sha256 != expected):
        return {"state": "unknown", "reason": "request_id_taken"}
    if any(item.transport is not None and item.transport.status == "submitted" for item in view.transmissions):
        return {"state": "sent"}
    return {"state": "unknown", "reason": "send_uncertain"}


def _goal(paths, store, grant, record, value, effects):
    """Send the launched goal once: look its id up first (never resend), then check and send."""
    goal = value["goal"]
    request_id = goal["request_id"]
    with store._connection() as conn:
        OwnerSetup._schema(conn)
        text = _preview_payload(conn, grant)["goal"]
    try:
        view = effects.receipt(value["fleet_uid"], request_id)
    except Exception:
        return {**goal, "reason": "receipt_unavailable"}
    if view is not None:
        return {"request_id": request_id, **_sent(view, value, text)}

    def check(ctx, route):
        """Immediately before the send, inside runtime admission."""
        from ..git_update import healthy_done, read_update
        from ..plane.owner_setup import managed_setup
        if (ctx.caller.uid, ctx.caller.alias, route.peer.uid) != (value["actor_uid"], value["alias"], value["lead_uid"]):
            raise _Halt("stale")
        # The Launch is still the computer's current healthy update: no later update finished since.
        package = managed_setup(paths.root)
        current = read_update(package.installation)
        if current != record or not healthy_done(current) or package.source_revision != record["target"]:
            raise _Halt("stale")
        with store._connection() as conn:
            OwnerSetup._schema(conn)
            reason = _stands(store, conn, grant, value)
            if reason is not None:
                raise _Halt(reason)
            if _preview_payload(conn, grant)["goal"] != text:
                raise _Halt("goal_changed")
        try:
            OwnerSetup(store).check_frozen(grant, record, text)
        except (AccessDenied, AccessUnavailable, OSError, ValueError, KeyError):
            raise _Halt("goal_changed") from None

    try:
        effects.send(value["alias"], request_id, text, check)
    except _Halt as halt:
        return {"request_id": request_id, "state": "not_sent", "reason": halt.reason}
    except Exception:
        pass  # An error can follow an effect: only the receipt says what happened.
    try:
        view = effects.receipt(value["fleet_uid"], request_id)
    except Exception:
        return {"request_id": request_id, "state": "unknown", "reason": "receipt_unavailable"}
    if view is None:
        return {"request_id": request_id, "state": "pending", "reason": "send_unavailable"}
    return {"request_id": request_id, **_sent(view, value, text)}


def card_answer(store, grant, value):
    """The finish's answer from its record; ``allowed`` only while the grant row still holds."""
    answer = {key: value[key] for key in ("state", "reason", "by", "alias", "renamed") if key in value}
    if "goal" in value:
        answer["goal"] = {key: value["goal"][key] for key in ("state", "reason") if key in value["goal"]}
    if value.get("state") == "allowed":
        with store._connection() as conn:
            OwnerSetup._schema(conn)
            if not _grant_holds(store, conn, value):
                answer.update(state="revoked", reason="messages_revoked")
    return answer


def card_status(store, retained):
    """For the status run and ``setup-status``: the marked approval's finish answer and launched goal."""
    grant = retained.grant if retained is not None else None
    if grant is None or grant.messages is None:
        return None
    with store._connection() as conn:
        OwnerSetup._schema(conn)
        row = conn.execute("SELECT payload FROM setup_previews WHERE generation=?", (grant.generation,)).fetchone()
        payload = json.loads(row["payload"]) if row is not None else {}
    value = payload.get("messages")
    finish = payload.get("finish")
    if value is not None and value.get("state") == "in_progress":
        # Stale is exact: in progress, and no process holds the finish's lock (every finish holds it from
        # before it reads the record until it ends, and the kernel frees it when the process dies).
        finish = {**(finish or {}), "running": finish_running(store.root, retained.launch_id)}
    return {"messages": card_answer(store, grant, value) if value is not None else None, "goal": payload.get("goal"),
            "finish": finish}


def card_line(answer):
    """One key=value line for a terminal: never the goal text."""
    if answer is None:
        return "setup_messages state=none"
    goal = answer.get("goal") or {}
    fields = (("state", answer.get("state")), ("reason", answer.get("reason")), ("by", answer.get("by")),
              ("alias", answer.get("alias")), ("goal", goal.get("state")), ("goal_reason", goal.get("reason")))
    return "setup_messages " + " ".join(f"{key}={value}" for key, value in fields if value is not None)


# Who finishes a marked Launch (04b2). The service managers whose computers start the finish by
# themselves: the one place that names a platform. Keyed as ``update_job`` keys its manager. A Mac
# joins later through this same check, the same waiter argument and the same record (D23).
COMPUTER_FINISH_MANAGERS = frozenset({"Linux"})
#: A starter's environment never carries these: no bot or fleet selector, no sealed release.
_STARTER_REFUSED = ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID")


def computer_can_finish(root):
    """Whether this computer's own starter can finish a marked Launch: its service manager is one in
    ``COMPUTER_FINISH_MANAGERS``, the guide is not on the managed Mac runtime, and the stable launcher
    is the one ``setup_handoff_argv`` accepts. The Preview's ``finish`` and the guide's start both ask
    this, and only this. Reads only; anything it cannot tell is False."""
    try:
        from .. import installation as module
        from ..plane.owner_setup import managed_setup
        installation = managed_setup(Path(root)).installation
        if platform.system() not in COMPUTER_FINISH_MANAGERS or module.managed_browser_runtime(installation) is not None:
            return False
        _host, body = module._entry_python(installation)
        return body == Path(module.__file__).read_bytes()
    except Exception:
        return False


def _finish_log(root, request_id):
    """``state/setup-finish-<launch request id>.log``, beside the finish's lock; never under ``runtime/``."""
    from ..plane.owner_setup import _uuid
    if not _uuid(request_id):
        raise AccessUnavailable("setup finish log is unavailable")
    return Path(root) / "state" / f"setup-finish-{request_id}.log"


def finish_log_status(root, request_id):
    """For ``setup-status`` only: the log's path and whether it exists. Nothing reads the log, and the
    status run sent to a page never carries the path."""
    try:
        path = _finish_log(root, request_id)
    except AccessUnavailable:
        return None
    return {"path": str(path), "exists": os.path.lexists(path)}


def _open_finish_log(root, request_id):
    """Create the log once, private, in a private ``state/``; refuse an existing file or a symlink."""
    path = _finish_log(root, request_id)
    state = path.parent
    info = state.lstat()
    if (state.resolve() != state.absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise AccessUnavailable("setup finish log directory is not private")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _start_waiter(paths, data):
    """Start the terminal's setup waiter for this Launch, detached, once. True when started.

    The waiter (``setup_handoff_argv``) watches the update record and, when the Launch is healthy and
    DONE, runs ``host owner setup --_finish-setup`` with no terminal (``_unattended_finish``). Its own
    session; working directory ``/``; umask 077; stdin ``/dev/null``; stdout and stderr the log; no
    other descriptor, so it holds neither the page's SSH channel open nor a runtime reader lease
    against the cutover. The guide's fixed profile environment, as it is. Any failure is False."""
    fd = None
    try:
        from ..installation import setup_handoff_argv
        from .host_owner import _GENERATED
        environment = dict(os.environ)
        if any(key in environment for key in (*_GENERATED, *_STARTER_REFUSED)):
            return False
        bound = {key: data[key] for key in ("state", "generation", "request_id", "config_fingerprint",
                                            "source_revision")}
        argv = setup_handoff_argv(paths.package.installation, bound)
        fd = _open_finish_log(paths.root, data["request_id"])
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd="/", env=environment,
                         start_new_session=True, close_fds=True, umask=0o077)
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def start_computer_finish(paths, store, data):
    """After the guide's ``launch_handoff``: for a stored approval with the messages mark, start the
    computer's own finish (``_start_waiter``) once, and record who will finish as ``payload["finish"]``.

    The mark is read from the stored approval, never from a frame. The record is ``{"starter":
    "computer"}``; ``{"starter": "page", "reason": "not_supported"}`` where ``computer_can_finish`` is
    false; ``{"starter": "page", "reason": "waiter_unavailable"}`` when the start failed. It is a display
    hint: nothing decides from it. Never raises and never waits; the Launch result is the caller's,
    unchanged. Returns the record, or None for anything else (an unmarked approval starts and records
    nothing)."""
    try:
        if not isinstance(data, dict) or data.get("state") != "launch_handoff":
            return None
        retained = OwnerSetup(store).retained_state()
        grant = retained.grant
        if (grant is None or grant.messages is None or retained.launch_id != data.get("request_id")
                or grant.generation != data.get("generation")):
            return None
        if not computer_can_finish(paths.root):
            value = {"starter": "page", "reason": "not_supported"}
        elif _start_waiter(paths, data):
            value = {"starter": "computer"}
        else:
            value = {"starter": "page", "reason": "waiter_unavailable"}
    except Exception:
        return None
    try:
        _keep(store, grant, value, key="finish")
    except Exception:
        pass  # The status then shows no ``finish``; the page reads that as its own finish.
    return value


def finish_running(root, request_id):
    """Whether a process holds this Launch's finish lock now: a non-creating open and a shared,
    non-blocking probe, released at once. A missing file, or one this open refuses, is not held: a
    finish opens the same file the same way, so it cannot hold one this open refuses."""
    import fcntl
    from ..plane.owner_setup import _uuid
    if not _uuid(request_id):
        return False
    try:
        fd = os.open(Path(root) / "state" / f"setup-finish-{request_id}.lock",
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def _card_messages(paths, store, grant, record, *, by, effects=None):
    """The finish for a marked approval, after a healthy Launch: lock, record, label, registration,
    one checked transaction, then the goal's checked send and receipt match. Safe to run again."""
    effects = effects or _CardEffects(paths, store, grant)
    with _finish_lock(paths.root, record["request_id"]) as held:
        if not held:
            return {"state": "in_progress", "by": by}
        with store._connection() as conn:
            OwnerSetup._schema(conn)
            value = _preview_payload(conn, grant).get("messages")
        if value is None or value.get("state") == "in_progress":
            stands = _still_stands(store, grant)
            try:
                # Before any name is read, and again inside the registration's admission just before
                # the name is written: a withdrawn or re-paired approval registers nothing.
                stands()
                if value is None:
                    base = grant.messages["alias"].removeprefix("human:")
                    name = numbered_name(base, effects.taken())
                    if name is None:
                        return card_answer(store, grant, _keep(store, grant, {
                            "state": "name_taken", "by": by, "alias": grant.messages["alias"], "renamed": False,
                            "goal": {"state": "not_sent", "reason": "name_taken"}}))
                    # Written before anything is registered: a finish that dies resumes with this label.
                    value = _keep(store, grant, {"state": "in_progress", "by": by, "alias": "human:" + name,
                        "renamed": name != base, "goal": {"request_id": goal_request_id(record["request_id"])}})
                ids = effects.register(value["alias"], stands)
            except _Halt as halt:
                start = value or {"by": by, "alias": grant.messages["alias"], "renamed": False}
                return card_answer(store, grant, _keep(store, grant, {**start, "state": halt.reason,
                    "reason": _REASONS[halt.reason], "goal": {"state": "not_sent", "reason": halt.reason}}))
            value = _admit(store, grant, ids, value)
        if value["state"] == "allowed" and value["goal"]["state"] in ("pending", "unknown"):
            value = _keep(store, grant, {**value, "goal": _goal(paths, store, grant, record, value, effects)})
        return card_answer(store, grant, value)


def _completed(args, paths, store, terminal, retained, record, package, *, consent=None, unattended=False):
    """``unattended``: started with no terminal (``_unattended_finish``). Only a card-approved Launch may
    finish so; anything else is refused here, before any output or write."""
    from ..plane.owner_setup import OwnerSetup
    from ..git_update import healthy_done
    from ..installation import _setup_binding
    from .host_owner import _setup_messages
    grant = retained.grant
    if unattended and (consent is not None or terminal is not None or grant is None or grant.messages is None):
        raise CommandFailure("conflict", "owner changes require an interactive operator terminal")
    actual = (grant.generation,record["request_id"], record["config_fingerprint"], record["target"])
    expected = getattr(args, "finish_setup", None)
    if (consent is not None and expected is None
            or expected is not None and _setup_binding(expected) != actual
            or not healthy_done(record)
            or package.source_revision != record["target"] or OwnerSetup(store).operation(grant) != record
            or getattr(args, "target_fleet", None) not in (None, grant.fleet)):
        raise CommandFailure("conflict", "the original setup is not current healthy DONE; inspect its original receipt")
    if grant.messages is not None:
        # The team card carried the messages mark: its finish, never the three optional cards.
        by = "computer" if unattended else "page" if consent is not None else "terminal"
        messages = _card_messages(paths, store, grant, record, by=by)
        # Unattended, the line goes to the finish's log: never the name (nor, as ever, the goal).
        shown = {key: value for key, value in messages.items() if key != "alias"} if unattended else messages
        return CommandOutput({"state": "setup_complete", "request_id": record["request_id"], "messages": messages},
                             lines=(card_line(shown),))
    _say(terminal, "Your original team setup completed. Configuration and serving are not repeated. Provider/setup approval did not grant messages.\n")
    messages = _setup_messages(paths, store, terminal, grant.fleet,
        **({"consent": consent} if consent is not None else {}))
    return CommandOutput({"state": "setup_complete", "request_id": record["request_id"], "messages": messages},
        lines=("Original setup finished; no Launch or message was sent again.",))


def run(args, paths, store, terminal, *, consent=None, profile=None, ssh=None, unattended=False):
    """``ssh`` is the captured server SSH source of an explicit SSH-controlled
    guide: no listener, no owner service, and typed operations on sibling
    channels. It is never inferred from a frame or from the platform alone.
    ``unattended`` is ``--_finish-setup`` with no terminal: it reaches only
    ``_completed`` or a refusal."""
    from . import host_owner as console
    from ..active_config import read_current_config
    from ..git_update import read_update
    from ..plane.owner_setup import managed_setup, _continuation
    from ..config import load_host_jobs
    from ..plane.owner_service import service_command
    from ..plane.owner_source import fresh_owner_setup

    retained = OwnerSetup(store).retained_state() if os.path.lexists(store.path) else None
    if retained and retained.state == "legacy_recovery_required":
        from ..plane.owner_setup import retained_fields
        raise CommandFailure("conflict", "historical sealed setup requires local recovery; no Git operation was inferred",
                             data=retained_fields(retained))
    package = managed_setup(paths.root)
    installation = package.installation
    record = read_update(installation)
    selected = os.path.lexists(paths.root / "state/config.json")
    # The one record a typed guide over the browser carrier may start again over: this approval's own
    # prepared Preview with nothing launched (its page closed before Launch). It goes on as a returning
    # run does. The SSH guide never needed a run alive between Preview and Launch.
    reopened = (record is not None and consent is not None and ssh is None
                and getattr(args, "finish_setup", None) is None
                and OwnerSetup(store).kept_preview(retained, record))
    if record is not None and not reopened:
        if (not retained or retained.grant is None or record.get("request_id") != retained.launch_id
                or record.get("config_fingerprint") != retained.launch_plan or record.get("phase") != "done"):
            raise _operation_retained(paths.root, retained, record)
        snapshot = read_current_config(paths.root, installation=installation, revision=record["target"])
        if snapshot != record.get("candidate_config"):
            raise CommandFailure("conflict", "current configuration differs from the original setup")
        return _completed(args, paths, store, terminal, retained, record, package,
                          **({"consent": consent} if consent is not None else {}),
                          **({"unattended": True} if unattended else {}))
    elif selected or retained and retained.launch_id is not None:
        raise CommandFailure("conflict", "guided setup cannot replace existing or unknown installation state")
    if getattr(args, "finish_setup", None) is not None or unattended:
        raise CommandFailure("conflict", "the original completed setup is unavailable; no replacement will be prepared")
    if not selected:
        fresh_owner_setup(paths.root, allow_absent=True)
    cli = installation.cli
    typed = {"consent": consent} if consent is not None else {}
    if ssh is not None and consent is None:
        raise CommandFailure("invalid_argument", "SSH-controlled setup is the typed guide only")
    carrier = {}
    if ssh is not None:
        from ..plane.owner_ssh import TRANSPORT
        carrier = {"transport": TRANSPORT}
    if consent is not None:
        if profile is None:
            raise CommandFailure("invalid_argument", "typed guide requires its fixed local profile")
        if ssh is not None and profile.runtime is None:
            raise CommandFailure("invalid_argument", "SSH-controlled setup requires the managed Mac runtime profile")
        profile.prepare_directories(consent, paths, **({"ssh_policy": True} if ssh is not None else {}))
        profile.prepare_provider(consent, paths)
        if ssh is not None:
            from ..plane.owner_ssh import MAC_TAILSCALE
            args.origin = _ssh_origin(ssh)  # logical key; never a claim of reachable HTTPS
            args.tailscale, args.tailscale_socket = MAC_TAILSCALE, ""
        else:
            args.origin = profile.origin()
            args.tailscale, args.tailscale_socket = LINUX_TAILSCALE, ""
        args.workspace_issuer, args.workspace_origin = _WORKSPACE_ORIGINS
        args.socket = paths.root / "state/owner/owner.sock"
        args.template, args.target_fleet, args.repository = "local-project", "web-app", None
        args.project_directory, args.account_directory = str(profile.project), str(profile.account)
        args.install_directory, args.provider_signin, args.managed_owner = profile.units, profile.provider, ssh is None
    config, approval = _configuration(args, paths, terminal, cli)
    if load_host_jobs()["plane-owner"].get("enroll", True) and approval.managed_owner is None:
        raise CommandFailure("conflict", "configured owner enrollment requires explicit --managed-owner handover")
    # No authority method is reached before the terminal's ancestry check.
    if not os.path.lexists(store.path):
        console._initialize_first_owner(paths, terminal, **typed)
    owner = store.current_grant()
    if (owner is None or not owner.active) and ssh is not None:
        # The browser begins pairing through its own typed SSH operation; the
        # same local confirmation as every other transport follows here.
        consent.state("pairing_ready", origin=config["origin"], **carrier)
        console._confirm_owner(paths, store, terminal, **typed)
    elif owner is None or not owner.active:
        direct = {**config, "workspace_issuer": None, "workspace_origin": None}
        _say(terminal, "Open " + json.dumps(config["origin"] + "/owner") + " and choose Start pairing.\n")
        command = service_command(paths.root, cli, direct)
        if consent is not None:
            command += ["--bootstrap-pairing-origin", config["workspace_origin"]]
        with _serving(command, Path(config["socket"]), config["origin"], **typed):
            if consent is not None:
                consent.state("pairing_ready", origin=config["origin"])
            console._confirm_owner(paths, store, terminal, **typed)
    owner = console._active_owner(store)
    console._describe(terminal, owner)
    if not selected and store.connected().preparation_state() != "ready":
        console._initialize_connected(store, terminal, prepare_workspace=True, **typed)
    with (_ssh_waiting() if ssh is not None else
          _serving(service_command(paths.root, cli, config), Path(config["socket"]), config["origin"], **typed)) as child:
        if not selected:
            workspace_state, binding = store.workspace().status()
            if workspace_state == "absent":
                _say(terminal, "In your signed-in browser choose Prepare local approval for this computer, then paste its code here.\n")
                if consent is not None:
                    consent.state("workspace_ready", origin=config["origin"], **carrier)
                console._confirm_workspace(store, terminal, owner, recover=True, **typed)
                workspace_state, binding = store.workspace().status()
            if (workspace_state != "active" or binding.issuer != config["workspace_issuer"]
                    or binding.operational_origin != config["workspace_origin"]):
                raise CommandFailure("conflict", "retained workspace differs or is inactive; inspect workspace-status; no binding was replaced")
            _say(terminal, "Retained workspace: " + json.dumps(asdict(binding), ensure_ascii=True) + "\n")
            entry, retained = retained, OwnerSetup(store).retained_state()
            if reopened and retained.state != "allowed":
                # The kept Preview's approval lapsed after the entry check: never ask to allow again.
                raise _operation_retained(paths.root, retained if retained.grant else entry, record)
            # A browser may retain this invocation's approved Launch as soon as
            # serving is ready. Entry checks already refused pre-existing work;
            # compare the same grant below, then use the normal handoff loop.
            if retained.state == "allowed":
                grant = retained.grant
                if (grant.template, grant.project_directory, grant.account_directory, grant.managed_owner, grant.provider_signin, grant.fleet, grant.repository, grant.installation, grant.source_revision, grant.install_directory) != (
                        approval.template, approval.project_directory, approval.account_directory, approval.managed_owner, approval.provider_signin, approval.target_fleet, approval.repository, str(installation.directory), package.source_revision, str(approval.install_directory)):
                    raise CommandFailure("conflict", "retained setup approval differs; no grant was replaced")
                _say(terminal, "Matching setup approval retained without renewal.\n")
            else:
                approval.install_directory.mkdir(parents=True, exist_ok=True)
                console._allow_setup(approval, paths, store, terminal, expected_retained=retained, **typed)
        _say(terminal, "Local access prepared. Review and launch the exact prepared request in your browser; keep original receipt IDs.\n"
                       "Keep this terminal open through Preview. After Launch is retained, the guide drains and exits so the updater can proceed.\n")
        if terminal is not None:
            terminal.flush()
        setup = OwnerSetup(store)
        approved = setup.local() if not selected else None
        if consent is not None:
            consent.state("preview_launch_ready", origin=config["origin"], generation=approved.generation, **carrier)
        try:
            while True:
                if consent is not None:
                    consent.check_cancel()
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
                        reconnect = ("No owner service is enrolled. After DONE, open a fresh authenticated SSH connection to this computer; do not launch again."
                                     if ssh is not None else
                                     "The approved managed connection will return at the same address; keep the original browser view open."
                                     if approved.managed_owner else "After DONE, reconnect without another setup: " + shlex.join(service_command(paths.root, cli, config)))
                        return CommandOutput({"state": "launch_handoff", "request_id": retained.launch_id,
                            "config_fingerprint": retained.launch_plan, "generation": approved.generation,
                            "source_revision": approved.source_revision, **carrier}, lines=(
                                "Original Launch retained. Local owner serving drained; this guide exits so the updater can proceed.",
                                "Inspect the original operation: " + status,
                                "If interrupted, continue that same operation locally; do not launch again.",
                                reconnect))
                    if consent is not None and (retained.grant != approved or not retained.effective_active):
                        raise CommandFailure("conflict", "setup approval expired or changed; no grant was renewed")
                try:
                    child.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            if consent is not None:
                from .owner_stdio import StdioRefused
                raise StdioRefused("interrupted_unknown") from None
            return CommandOutput({"state": "serving_stopped"}, lines=("Local foreground serving stopped; shutdown did not change approvals or stop the fleet.",))
        raise CommandFailure("unavailable", "local foreground owner stopped unexpectedly; no server or launch was retried")


def _computer_handoff(outcome, ids):
    """``computer_starting`` for a start under way; ``start_failed`` with core's line for one that stopped."""
    from .owner_stdio import StdioRefused
    if ids is None:  # A record gone since it was read: nothing to name.
        raise StdioRefused("unavailable")
    if outcome == "starting":
        return CommandOutput({"state": "computer_starting", "request_id": ids["request_id"],
                              "config_fingerprint": ids["config_fingerprint"], "target": ids["target"]})
    if outcome == "failed":
        raise StdioRefused("start_failed", data={"line": ids["line"], "request_id": ids["request_id"]})
    raise StdioRefused("unavailable")


def _computer_ready(paths, installation, profile, ssh, carrier, consent):
    """``computer_ready`` once the started computer answers, never from a guide's own child.

    Over SSH nothing listens: the address is the server's own logical key. On
    Linux the lasting service must answer ``/owner`` on the socket its settings
    name, for the origin native Tailscale reads for this computer now, within
    ``READY_SECONDS``; otherwise ``start_failed``.
    """
    from ..config import load_host_jobs
    from ..git_update import read_update
    from .owner_stdio import StdioRefused
    if ssh is not None:
        return CommandOutput({"state": "computer_ready", "origin": _ssh_origin(ssh), **carrier})
    origin = profile.origin()
    owner = load_host_jobs()["plane-owner"]
    socket = owner.get("socket")
    if owner.get("enroll") is True and owner.get("origin") == origin and isinstance(socket, str):
        deadline = time.monotonic() + READY_SECONDS
        while True:
            consent.check_cancel()
            if _ready(Path(socket), origin):
                return CommandOutput({"state": "computer_ready", "origin": origin})
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    record = read_update(installation)
    raise StdioRefused("start_failed", data={"line": _NOT_READY,
                                             "request_id": record["request_id"] if record else None})


def run_computer(args, paths, store, *, consent, profile, ssh=None):
    """The computer run: the cards that add a computer, then its start; and the way back into it.

    By phase 1's ``start_state``: ``other``, or a first-team approval or Launch
    retained by today's guide, refuses ``use_full_guide`` and writes nothing.
    ``started`` asks nothing and answers ``computer_ready``. ``starting`` asks
    nothing and dispatches nothing: it reads the start and answers
    ``computer_starting`` or ``start_failed``. ``fresh`` asks the folders card,
    pairing, one storage card and the workspace link, each only if an earlier
    run has not, drains its own listener, then asks the start card and
    dispatches. It never waits for the updater and never continues a record:
    its exit is what lets the updater proceed, so it never answers
    ``computer_ready`` in the process that dispatched a start. No Claude and no
    team card is asked. ``ssh`` is as for ``run``.
    """
    from . import host_owner as console
    from .owner_setup_consent import admitted_update
    from .owner_stdio import StdioRefused
    from ..config import load_host_jobs
    from ..plane import owner_start
    from ..plane.owner_service import service_command
    from ..plane.owner_setup import managed_setup
    from ..plane.owner_source import start_state
    if consent is None or profile is None:
        raise CommandFailure("invalid_argument", "the computer run is the typed guide only")
    if ssh is not None and profile.runtime is None:
        raise CommandFailure("invalid_argument", "SSH-controlled setup requires the managed Mac runtime profile")
    package = managed_setup(paths.root)
    installation = package.installation
    # Admission at the installed revision: the profile checked it; the run checks it again.
    if profile.receipt_revision != package.source_revision:
        admitted_update(installation, profile.receipt_revision, package.source_revision,
                        getattr(args, "update_record", None))
    retained = OwnerSetup(store).retained_state() if os.path.lexists(store.path) else None
    if retained is not None and (retained.state != "absent" or retained.launch_id is not None):
        raise StdioRefused("use_full_guide")
    state = start_state(paths.root, installation)
    if state == "other":
        raise StdioRefused("use_full_guide")
    carrier = {}
    if ssh is not None:
        from ..plane.owner_ssh import TRANSPORT
        carrier = {"transport": TRANSPORT}
    if state == "started":
        return _computer_ready(paths, installation, profile, ssh, carrier, consent)
    if state == "starting":
        # Core's read-only reader: it asks no card and dispatches nothing, even
        # when the record is gone by the time it reads.
        outcome, ids = owner_start._progress(paths.root, installation)
        if outcome == "started":  # Finished since it was read; this run dispatched nothing.
            return _computer_ready(paths, installation, profile, ssh, carrier, consent)
        return _computer_handoff(outcome, ids)
    typed = {"consent": consent}
    profile.prepare_directories(consent, paths, **({"ssh_policy": True} if ssh is not None else {}))
    if ssh is not None:
        from ..plane.owner_ssh import MAC_TAILSCALE
        args.origin = _ssh_origin(ssh)  # logical key; never a claim of reachable HTTPS
        args.tailscale, args.tailscale_socket = MAC_TAILSCALE, ""
    else:
        args.origin = profile.origin()
        args.tailscale, args.tailscale_socket = LINUX_TAILSCALE, ""
    args.workspace_issuer, args.workspace_origin = _WORKSPACE_ORIGINS
    args.socket = paths.root / "state/owner/owner.sock"
    cli = installation.cli
    config = _serving_configuration(args, paths, None, cli)
    # On Linux the lasting service's settings; built fresh by every run, so a
    # run after a failed start never reuses an old plan.
    plan = _owner_override_plan(paths.root, config) if ssh is None else None
    if load_host_jobs()["plane-owner"].get("enroll", True) and plan is None:
        raise CommandFailure("conflict", "configured owner enrollment requires explicit --managed-owner handover")
    if not os.path.lexists(store.path):
        console._initialize_first_owner(paths, None, **typed)
    owner = store.current_grant()
    if (owner is None or not owner.active) and ssh is not None:
        consent.state("pairing_ready", origin=config["origin"], **carrier)
        console._confirm_owner(paths, store, None, **typed)
    elif owner is None or not owner.active:
        direct = {**config, "workspace_issuer": None, "workspace_origin": None}
        command = service_command(paths.root, cli, direct) + ["--bootstrap-pairing-origin", config["workspace_origin"]]
        with _serving(command, Path(config["socket"]), config["origin"], **typed):
            consent.state("pairing_ready", origin=config["origin"])
            console._confirm_owner(paths, store, None, **typed)
    owner = console._active_owner(store)
    if store.connected().preparation_state() != "ready":
        console._initialize_link_storage(store, None, **typed)
    workspace_state, binding = store.workspace().status()
    if workspace_state == "absent":
        with (_ssh_waiting() if ssh is not None else
              _serving(service_command(paths.root, cli, config), Path(config["socket"]), config["origin"], **typed)):
            consent.state("workspace_ready", origin=config["origin"], **carrier)
            console._confirm_workspace(store, None, owner, recover=True, **typed)
        workspace_state, binding = store.workspace().status()
    if (workspace_state != "active" or binding.issuer != config["workspace_issuer"]
            or binding.operational_origin != config["workspace_origin"]):
        raise CommandFailure("conflict", "retained workspace differs or is inactive; inspect workspace-status; no binding was replaced")
    # The guide's own listener is drained above; the lasting service may now take the socket.
    outcome, ids = owner_start.start(paths, store, None, consent=consent, owner_plan=plan,
                                     directory=profile.units)
    if outcome == "started":  # Started by another run meanwhile; this one dispatched nothing.
        raise StdioRefused("unavailable")
    return _computer_handoff(outcome, ids)
