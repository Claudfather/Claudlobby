"""Finite, non-PTY owner guide protocol for an admitted local operator.

Framing correlates explicit consent; it authenticates nobody. The local SSH
account, installed source and operator-context checks remain the authority.
Only core-authored proposals enter this channel; no commands or paths do.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import time
import threading
import signal
from contextlib import contextmanager
from uuid import uuid4

FRAME_BYTES = 16384
OUTPUT_BYTES = 32768
MAX_FRAMES = 128
TOTAL_BYTES = 262144
IDLE_SECONDS = 300
ADMISSION_SECONDS = 1800
_ACTIONS = frozenset(("prepare_directories", "initialize_owner", "pair_owner",
    "initialize_workspace", "initialize_connected", "bind_workspace", "provider_signin",
    "allow_setup", "actor_alias", "register_actor", "allow_messages", "install_provider",
    "start_workspace", "initialize_link_storage"))
_BASE = {"version", "type", "seq", "session_id"}
_FIELDS = {
    "start": {"profile", "expected_uid", "source_revision", "binding_sha256", "finish_setup"},
    "submit_pairing": {"challenge"}, "submit_workspace": {"nonce"},
    "decide": {"proposal_id", "digest", "approve"}, "cancel": set(),
}


class StdioRefused(Exception):
    """Finite redacted refusal, never raw input, configuration or child output.

    ``data`` is the computer run's ``start_failed`` only: core's line and the
    record's request id, sent in the error frame beside the code."""
    def __init__(self, code="invalid_request", *, data=None):
        self.code = code
        self.data = dict(data or {})
        super().__init__(code)


@contextmanager
def _deferred_stops(stopped):
    """Record stop signals across owned startup/drain without throwing through it."""
    previous = {}
    try:
        if threading.current_thread() is threading.main_thread():
            def stop(_signal, _frame):
                stopped.set()
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _wire(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


class OwnerStdio:
    """One process-local session, one outstanding immutable proposal at a time."""
    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.session_id = uuid4().hex
        self._input_seq, self._output_seq = 0, 0
        self._input_bytes, self._output_bytes = 0, 0
        self._buffer = bytearray()
        self._began = time.monotonic()
        self._closed = False
        self._pending = None
        self._named = False
        self._partial_since = None
        self._extra = {}

    def emit(self, kind, **body):
        if kind not in {"hello", "proposal", "state", "result", "error"}:
            raise ValueError("unknown owner frame")
        self._output_seq += 1
        raw = _wire({**body, "version": 1, "type": kind, "seq": self._output_seq,
                     "session_id": self.session_id})
        self._output_bytes += len(raw)
        if (len(raw) > OUTPUT_BYTES or self._output_bytes > TOTAL_BYTES
                or self._output_seq > MAX_FRAMES):
            raise StdioRefused("output_limit")
        end = time.monotonic() + 5
        try:
            while raw:
                remaining = end - time.monotonic()
                if remaining <= 0 or not select.select([], [self.outgoing], [], remaining)[1]:
                    raise StdioRefused("output_unavailable")
                count = os.write(self.outgoing, raw[:512])
                if not count:
                    raise StdioRefused("output_unavailable")
                raw = raw[count:]
        except OSError:
            raise StdioRefused("output_unavailable") from None

    def hello(self):
        self.emit("hello", profile="owner-setup-v1")

    def _read(self, allowed, *, expires_at=None, admission=True):
        if self._closed:
            raise StdioRefused("session_closed")
        end = min(time.monotonic() + IDLE_SECONDS, self._began + ADMISSION_SECONDS) if admission else None
        try:
            while b"\n" not in self._buffer:
                if len(self._buffer) >= FRAME_BYTES:
                    raise StdioRefused()
                remaining = None if end is None else end - time.monotonic()
                if expires_at is not None:
                    expiry = expires_at - time.time()
                    remaining = expiry if remaining is None else min(remaining, expiry)
                if remaining is not None and remaining <= 0:
                    raise StdioRefused("expired")
                if not select.select([self.incoming], [], [], remaining)[0]:
                    raise StdioRefused("expired")
                part = os.read(self.incoming, min(4096, FRAME_BYTES - len(self._buffer)))
                if not part:
                    raise StdioRefused("disconnected")
                self._buffer.extend(part)
            raw, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            self._partial_since = time.monotonic() if rest and b"\n" not in rest else None
            self._input_bytes += len(raw) + 1
            if (len(raw) + 1 > FRAME_BYTES or self._input_bytes > TOTAL_BYTES
                    or any(c < 32 or c > 126 for c in raw)):
                raise StdioRefused()
            value = json.loads(raw, object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            kind = value.get("type") if isinstance(value, dict) else None
            fields = _BASE | _FIELDS.get(kind, set()) | self._extra.get(kind, set())
            if kind == "decide" and (self._pending == "actor_alias" or self._named):
                fields |= {"actor_alias"}
            if (kind not in _FIELDS or set(value) != fields or _wire(value) != raw + b"\n"
                    or type(value["version"]) is not int or value["version"] != 1
                    or type(value["seq"]) is not int or value["seq"] != self._input_seq + 1
                    or value["seq"] > MAX_FRAMES or value["session_id"] != self.session_id):
                raise StdioRefused()
            self._input_seq = value["seq"]
            if kind == "cancel":
                raise StdioRefused("cancelled")
            if kind not in allowed:
                raise StdioRefused()
            if expires_at is not None and time.time() >= expires_at:
                raise StdioRefused("expired")
            if end is not None and time.monotonic() >= end:
                raise StdioRefused("expired")
            return value
        except (ValueError, TypeError, RecursionError, OSError):
            self._closed = True
            raise StdioRefused() from None
        except StdioRefused:
            self._closed = True
            raise

    def start(self, *, computer=False):
        """``computer``: the computer run's start, which also carries ``update_record``:
        null, or the request id and target of the update record the client read."""
        if computer:
            self._extra = {"start": {"update_record"}}
        value = self._read({"start"})
        if (value["profile"] != "owner-setup-v1" or type(value["expected_uid"]) is not int
                or not 0 < value["expected_uid"] < 2**32 - 1
                or not isinstance(value["source_revision"], str)
                or re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]) is None
                or not isinstance(value["binding_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", value["binding_sha256"]) is None):
            raise StdioRefused()
        if value["finish_setup"] is not None:
            from ..installation import _setup_binding, InstallationError
            try:
                if type(value["finish_setup"]) is not list:
                    raise InstallationError("invalid completion")
                _setup_binding(value["finish_setup"])
            except (InstallationError, TypeError):
                raise StdioRefused() from None
        if computer:
            known = value["update_record"]
            if value["finish_setup"] is not None or known is not None and (
                    type(known) is not list or len(known) != 2 or not all(isinstance(v, str) for v in known)
                    or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                                    known[0]) is None
                    or re.fullmatch(r"[0-9a-f]{40}", known[1]) is None):
                raise StdioRefused()
        return value

    def approve(self, action, scope, *, original=True, expires_at=None):
        if action not in _ACTIONS or self._pending is not None:
            raise ValueError("unknown or overlapping guide proposal")
        expiry = min(time.time() + IDLE_SECONDS, expires_at) if expires_at is not None else time.time() + IDLE_SECONDS
        if expiry <= time.time():
            raise StdioRefused("expired")
        proposal = {"action": action, "scope": scope, "expires_at": expiry}
        digest = hashlib.sha256(_wire(proposal).rstrip(b"\n")).hexdigest()
        identifier = uuid4().hex
        self._pending = action
        # The team card with the messages mark: its answer carries the name the team will see.
        self._named = (action == "allow_setup" and isinstance(scope, dict) and isinstance(scope.get("grant"), dict)
                       and scope["grant"].get("messages") == {"sender": "card"})
        try:
            self.emit("proposal", proposal_id=identifier, digest=digest, proposal=proposal)
            value = self._read({"decide"}, expires_at=expiry)
            if (value["proposal_id"] != identifier or value["digest"] != digest
                    or type(value["approve"]) is not bool):
                raise StdioRefused()
            if not value["approve"]:
                raise StdioRefused("declined")
            if action == "actor_alias":
                from ..operation_context import _valid_human_alias
                if not isinstance(value["actor_alias"], str) or not _valid_human_alias(value["actor_alias"]):
                    raise StdioRefused()
                return value["actor_alias"]
            if self._named:
                from ..operation_context import sender_alias_refusal
                if sender_alias_refusal(value["actor_alias"]) is not None:
                    raise StdioRefused("sender_name_refused")
                return value["actor_alias"]
            return original
        finally:
            self._pending = None
            self._named = False

    def token(self, kind):
        if kind not in {"pairing", "workspace"}:
            raise ValueError("unknown guide input")
        self.emit("state", state="awaiting_" + kind)
        value = self._read({"submit_" + kind})
        token = value["challenge" if kind == "pairing" else "nonce"]
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token) is None:
            raise StdioRefused()
        return token

    def check_cancel(self):
        """Serving has no admission timer; only EOF/cancel may arrive here."""
        if self._buffer and self._partial_since is None:
            self._partial_since = time.monotonic()
        if select.select([self.incoming], [], [], 0)[0]:
            part = os.read(self.incoming, min(4096, FRAME_BYTES - len(self._buffer)))
            if not part:
                raise StdioRefused("disconnected")
            self._buffer.extend(part)
            if self._partial_since is None:
                self._partial_since = time.monotonic()
        if b"\n" in self._buffer:
            self._read(set(), admission=False)
        if len(self._buffer) >= FRAME_BYTES or self._partial_since is not None and time.monotonic() - self._partial_since > 5:
            raise StdioRefused()

    def install_provider(self, operation):
        """One approved public-artifact helper; EOF/cancel drains its own child."""
        self.check_cancel()
        cancel = threading.Event()
        result, errors = [], []
        def work():
            try:
                result.append(operation(cancel))
            except BaseException:
                errors.append(True)
        thread = threading.Thread(target=work, name="owner-provider-install")
        interrupted = None
        try:
            # Signal handlers record cancellation immediately. In particular,
            # no Python signal exception can escape Thread.start after its
            # native thread exists but before Python finishes its bookkeeping.
            with _deferred_stops(cancel):
                try:
                    thread.start()
                    while thread.is_alive() and not cancel.is_set():
                        self.check_cancel()
                        thread.join(.05)
                except BaseException as exc:
                    interrupted = exc
                    cancel.set()
                finally:
                    # The helper owns its finite download child and cleanup.
                    # Never abandon it or invent a force-kill deadline.
                    while thread.ident is not None and thread.is_alive():
                        try:
                            thread.join(.05)
                        except KeyboardInterrupt as exc:
                            interrupted = exc
                            cancel.set()
        except BaseException as exc:
            interrupted = exc
            cancel.set()
        if (cancel.is_set() or interrupted is not None or errors or not result
                or getattr(result[0], "state", None) == "unknown"):
            raise StdioRefused("provider_install_unknown") from interrupted
        return result[0]


def _selectors(args):
    """Reject inherited selectors before input, environment scrubbing or reads."""
    from .host_owner import _GENERATED
    if any(name in os.environ for name in _GENERATED):
        raise StdioRefused("operator_required")
    if (args.root is None or args.fleet or args.seed or args.json
            or os.isatty(0) or os.isatty(1)
            or any(getattr(args, key, None) is not None for key in (
                "origin", "tailscale", "tailscale_socket", "socket", "workspace_issuer", "workspace_origin",
                "target_fleet", "repository", "project_directory", "account_directory", "install_directory",
                "provider_signin", "finish_setup")) or getattr(args, "managed_owner", False)
            or args.template != "revenue-web-app"):
        raise StdioRefused()


def _admit(args, request):
    """Ground the account/root, then check native ancestry before scrubbing."""
    import pwd
    import sys
    from pathlib import Path
    from .operator_context import require_operator_context
    from ..context import resolve_paths
    from ..plane.owner_source import fresh_owner_setup
    _selectors(args)
    uid = os.getuid()
    if uid == 0 or uid != os.geteuid() or uid != request["expected_uid"] or not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise StdioRefused("account_changed")
    root = Path(pwd.getpwuid(uid).pw_dir) / ".local/share/claudlobby-browser/data"
    if not root.is_absolute() or root.resolve() != root or Path(args.root) != root:
        raise StdioRefused("installation_changed")
    paths = resolve_paths(root=args.root)
    if paths.package.installation is not None and not os.path.lexists(paths.root / "state/config.json"):
        fresh_owner_setup(paths.root, allow_absent=True)
        require_operator_context()
    else:
        require_operator_context(paths.root)
    return paths


def _status(paths, store, *, profile=None):
    from dataclasses import asdict
    from ..git_update import read_update
    from ..plane.owner_setup import OwnerSetup, retained_fields
    retained = OwnerSetup(store).retained_state() if os.path.lexists(store.path) else None
    record = read_update(paths.package.installation)
    owner = store.current_grant() if os.path.lexists(store.path) else None
    operation = None if record is None else {key: record.get(key) for key in (
        "phase", "request_id", "config_fingerprint", "target", "maintenance", "config_committed")}
    value = {"state": "status", "owner": asdict(owner) if owner else None,
            "setup": retained_fields(retained) if retained else {"state": "absent"}, "operation": operation}
    from .host_owner_setup import card_status
    card = card_status(store, retained) if retained else None
    if card is not None:
        # A marked approval: its finish answer (None before the finish) and the launched goal.
        value["setup"].update(card)
    if profile is not None:
        from ..provider_install import status
        value["provider"] = asdict(status(profile.account_name, profile.uid))
    return value


def dispatch(args):
    """Own stdout completely; the terminal result wrapper must not run here."""
    from types import SimpleNamespace
    from ..command_result import CommandFailure
    from ..plane.owner_access import OwnerAccess, AccessDenied, AccessUnavailable
    from .owner_setup_consent import FixedOwnerProfile, StdioGuideConsent
    from .host_owner_setup import run, run_computer, interrupted
    channel = OwnerStdio(0, 1)
    computer = getattr(args, "stdio_computer", False) is True
    data = {}
    try:
        channel.hello()
        _selectors(args)
        request = channel.start(computer=True) if computer else channel.start()
        paths = _admit(args, request)
        profile = FixedOwnerProfile(paths, request, computer=True) if computer else FixedOwnerProfile(paths, request)
        # The managed Mac runtime has no admitted HTTPS ingress: its guide is
        # the explicit SSH-controlled lifecycle. Capture the server tuple here,
        # before the fixed profile replaces the environment; a missing or
        # ambiguous one refuses rather than falling back to another transport.
        ssh = None
        if profile.runtime is not None and not args.stdio_status:
            from ..plane.owner_ssh import SSHSessionSource
            try:
                ssh = SSHSessionSource.capture()
            except AccessDenied:
                raise StdioRefused("owner_ssh_source_refused") from None
        with profile.environment(), interrupted():
            store = OwnerAccess(paths.root)
            if args.stdio_status:
                channel.emit("result", **_status(paths, store, profile=profile))
                return 0
            configured = SimpleNamespace(**vars(args))
            configured.finish_setup = request["finish_setup"]
            consent = StdioGuideConsent(channel)
            if computer:
                configured.update_record = request["update_record"]
                result = run_computer(configured, paths, store, consent=consent, profile=profile,
                                      **({"ssh": ssh} if ssh is not None else {}))
            else:
                result = run(configured, paths, store, None, consent=consent, profile=profile,
                             **({"ssh": ssh} if ssh is not None else {}))
            channel.emit("result", **result.data)
        return 0
    except StdioRefused as exc:
        code, data = exc.code, exc.data
    except CommandFailure as exc:
        code = exc.error.code
    except (AccessDenied, AccessUnavailable, OSError, ValueError, ImportError, TimeoutError):
        code = "unavailable"
    except (EOFError, KeyboardInterrupt):
        code = "cancelled"
    except Exception:
        code = "internal_error"
    try:
        channel.emit("error", code=code, state="stopped", retry=False, **data,
            **({"next": "inspect_status"} if code.startswith("provider_install_") or code == "interrupted_unknown" else {}))
    except (StdioRefused, OSError):
        pass
    return 4
