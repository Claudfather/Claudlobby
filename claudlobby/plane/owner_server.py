"""Explicit private Unix-socket owner server, intended only behind Tailscale Serve.

This does not configure Serve, initialize authority, choose an identity or add
message grants. Other processes with the owner's OS privileges are outside the
boundary; other users cannot connect to the private socket. There is no TCP
fallback, even if the local Tailscale daemon cannot reach this socket.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat
import signal
import threading


class OwnerServerConfigurationError(ValueError):
    """Actionable local configuration refusal, never a browser response."""


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
def private_listener(path: Path):
    """Bind only a new socket; never remove an existing or replacement path."""
    path = Path(path)
    if not path.is_absolute():
        raise OwnerServerConfigurationError("owner socket path must be absolute")
    parent_identity = _private_directory(path.parent)
    if os.path.lexists(path):
        raise OwnerServerConfigurationError("owner socket path exists; inspect the previous server")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        # The containing directory is already private throughout creation.
        listener.bind(str(path))
        info = path.lstat()
        identity = info.st_dev, info.st_ino
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or _private_directory(path.parent) != parent_identity):
            raise OwnerServerConfigurationError("owner socket changed during creation")
        path.chmod(0o600)
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


def serve(root: Path, *, origin: str, tailscale_binary: Path, socket_path: Path | None = None,
          package=None):
    """Foreground server; caller explicitly selects root and local authority."""
    import uvicorn

    from .owner_access import OwnerAccess
    from .owner_browser import create_owner_browser_app
    from .owner_ingress import ServePrincipalVerifier
    from .owner_source import inspect_source
    from .view import begin_shutdown

    OwnerAccess(root).current_grant()  # validate existing authority, no initialization
    inspect_source(root)  # explicit local source attestation must already exist
    try:
        app = create_owner_browser_app(root, expected_origin=origin,
            verify_principal=ServePrincipalVerifier(tailscale_binary=Path(tailscale_binary)),
            package=package)
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
        OwnerServer(config).run(sockets=[listener])
