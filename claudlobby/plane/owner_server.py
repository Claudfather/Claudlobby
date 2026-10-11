"""Explicit private Unix-socket owner server, intended only behind Tailscale Serve.

This does not configure Serve, initialize authority, choose an identity or add
message grants. Other processes with the owner's OS privileges are outside the
boundary; other users cannot connect to the private socket. There is no TCP
fallback, even if the local Tailscale daemon cannot reach this socket.
"""

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import signal
import threading


class OwnerServerConfigurationError(ValueError):
    """Actionable local configuration refusal, never a browser response."""


class OwnerServerStartupError(RuntimeError):
    """The server run ended before completing startup."""


def _private_directory(path: Path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise OwnerServerConfigurationError("owner socket requires an existing owner-only directory") from exc
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()):
        raise OwnerServerConfigurationError("owner socket requires an existing owner-only directory")
    return info.st_dev, info.st_ino


@contextmanager
def _listener_lease(path: Path, parent_identity, *, create=True):
    # Never unlink the lock: a second inode would permit two simultaneous owners.
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(lock_path, flags | (os.O_CREAT if create else 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > 512):
            raise OwnerServerConfigurationError("owner socket lock is not a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OwnerServerConfigurationError("owner socket already has a running server") from None
        current = lock_path.lstat()
        if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or _private_directory(path.parent) != parent_identity):
            raise OwnerServerConfigurationError("owner socket lock changed during acquisition")
        raw = os.read(fd, 513)
        try:
            previous = json.loads(raw) if raw else None
            # Only our exact encoding is accepted, including no duplicate keys.
            if raw and json.dumps(previous).encode() != raw:
                raise ValueError()
            if previous is not None and (not isinstance(previous, dict)
                    or set(previous) != {"version", "parent", "socket"}
                    or type(previous["version"]) is not int or previous["version"] != 1
                    or any(not isinstance(previous[key], list) or len(previous[key]) != 2
                           or any(type(value) is not int or value < 0 for value in previous[key])
                           for key in ("parent", "socket"))):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise OwnerServerConfigurationError("owner socket lock record is invalid; inspect it locally") from None
        yield fd, previous
    finally:
        os.close(fd)


def _reclaim_listener(path: Path, previous, parent_identity, *, remove=True):
    info = path.lstat()
    identity = info.st_dev, info.st_ino
    if (not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid() or previous is None
            or previous["socket"] != list(identity) or previous["parent"] != list(parent_identity)):
        raise OwnerServerConfigurationError("owner socket path exists; inspect the previous server")
    # A legacy or noncooperating live server must also be preserved. Only a
    # definite refused connection permits reclaiming our recorded socket.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise OwnerServerConfigurationError("owner socket liveness is uncertain; inspect the previous server") from None
        else:
            raise OwnerServerConfigurationError("owner socket already has a running server")
    current = path.lstat()
    if ((current.st_dev, current.st_ino) != identity or not stat.S_ISSOCK(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600 or current.st_uid != os.geteuid()
            or _private_directory(path.parent) != parent_identity):
        raise OwnerServerConfigurationError("owner socket changed during recovery")
    if remove:
        path.unlink()


def check_listener_available(path: Path):
    """Noncreating activation preflight; startup repeats ownership and liveness."""
    identity = _private_directory(path.parent)
    if not os.path.lexists(path.with_name(path.name + ".lock")):
        if os.path.lexists(path):
            raise OwnerServerConfigurationError("owner socket is unrecorded; inspect it locally")
        return
    with _listener_lease(path, identity, create=False) as (_, previous):
        if os.path.lexists(path):
            _reclaim_listener(path, previous, identity, remove=False)


@contextmanager
def private_listener(path: Path):
    """Recover only our recorded dead socket while holding its private lease."""
    path = Path(path)
    if not path.is_absolute():
        raise OwnerServerConfigurationError("owner socket path must be absolute")
    parent_identity = _private_directory(path.parent)
    with _listener_lease(path, parent_identity) as (lease, previous):
        if os.path.lexists(path):
            _reclaim_listener(path, previous, parent_identity)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            listener.bind(str(path))
            info = path.lstat()
            identity = info.st_dev, info.st_ino
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                    or _private_directory(path.parent) != parent_identity):
                raise OwnerServerConfigurationError("owner socket changed during creation")
            path.chmod(0o600)
            record = json.dumps({"version": 1, "parent": parent_identity, "socket": identity}).encode()
            os.lseek(lease, 0, os.SEEK_SET)
            os.ftruncate(lease, 0)
            if os.write(lease, record) != len(record):
                raise OwnerServerConfigurationError("owner socket ownership could not be recorded")
            os.fsync(lease)
            listener.listen(32)
            yield listener
        finally:
            listener.close()
            if identity is not None:
                try:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) == identity and stat.S_ISSOCK(info.st_mode):
                        path.unlink()
                except FileNotFoundError:
                    pass
                os.ftruncate(lease, 0)
                os.fsync(lease)


def serve(root: Path, *, origin: str, tailscale_binary: Path, socket_path: Path | None = None,
          package=None, workspace_issuer=None, workspace_origin=None):
    """Foreground server; caller explicitly selects root and local authority."""
    import uvicorn

    from .owner_access import AccessUnavailable, OwnerAccess
    from .owner_browser import create_owner_browser_app
    from .owner_ingress import ServePrincipalVerifier
    from .owner_source import inspect_source
    from .view import begin_shutdown

    OwnerAccess(root).current_grant()  # validate existing authority, no initialization
    inspect_source(root)  # explicit local source attestation must already exist
    try:
        workspace_bootstrap = None
        if workspace_issuer is not None or workspace_origin is not None:
            from .owner_workspace_http import WorkspaceBootstrapConfig
            workspace_bootstrap = WorkspaceBootstrapConfig(workspace_issuer, workspace_origin)
        app = create_owner_browser_app(root, expected_origin=origin,
            verify_principal=ServePrincipalVerifier(tailscale_binary=Path(tailscale_binary)),
            package=package, workspace_bootstrap=workspace_bootstrap)
    except AccessUnavailable:
        if workspace_bootstrap is not None:
            raise OwnerServerConfigurationError(
                "workspace bootstrap authority unavailable; inspect local workspace authority "
                "and run host owner initialize-workspace if not initialized") from None
        raise
    except ValueError as exc:
        raise OwnerServerConfigurationError(str(exc)) from exc
    socket_path = socket_path or Path(root) / "state/plane/owner.sock"

    class OwnerServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # Let the CLI unwind and remove its owned socket after shutdown.
            # Uvicorn's default re-raises SIGTERM before that outer cleanup.
            if threading.current_thread() is not threading.main_thread():
                raise OwnerServerConfigurationError("owner server must run in the main thread")
            previous = {sig: signal.signal(sig, self.handle_exit)
                        for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                yield
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

        def handle_exit(self, sig, frame):
            begin_shutdown(app)
            super().handle_exit(sig, frame)

    with private_listener(socket_path) as listener:
        # Trust comes only from the socket's filesystem boundary. In particular,
        # proxy middleware must not turn an attacker-supplied forwarding header
        # into scope.client or alter which headers the identity verifier sees.
        config = uvicorn.Config(app, log_level="warning", access_log=False,
                                proxy_headers=False, timeout_graceful_shutdown=5)
        server = OwnerServer(config)
        try:
            server.run(sockets=[listener])
        except SystemExit as exc:
            # Recent uvicorn exits during failed startup; older versions return.
            # Both paths must produce the same actionable local CLI failure.
            if not server.started:
                raise OwnerServerStartupError("owner server startup failed; inspect local server logs") from exc
            raise
        if not server.started:
            raise OwnerServerStartupError("owner server startup failed; inspect local server logs")
