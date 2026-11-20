"""One locally approved Claude CLI setup-token sign-in; secrets never leave this host.

The recognizer is pinned to inspected public distribution bytes, not a private
OAuth API. PTY contents, pasted codes and tokens are memory-only. A persisted
interrupted attempt is uncertain and can never be dispatched again.

One attempt is current at a time. A new attempt ID replaces it only after an
ending that proves its code never reached the CLI (``RETRYABLE``), so at most one
attempt per approval ever receives a code, and at most one token can be minted.
"""
from __future__ import annotations

import codecs
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import signal
import stat
import struct
import subprocess
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .owner_access import AccessDenied, AccessUnavailable

# Only this inspected public native artifact is supported. A version string is
# not sufficient; additions require independent distribution/source inspection.
SUPPORTED = {
    "84590fefe6338be9087f5814b61f9a54c5f21524163a4c127cb5a808e31009f2": "2.1.325",  # Darwin arm64
    "7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67": "2.1.325",  # Linux arm64 glibc
    "0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf": "2.1.325",  # Linux x86_64 glibc
}
# Attempt length; codes are taken until CODE_MARGIN before it. CONVERSATION_SECONDS must cover
# it, and SIGNIN_LIFETIME and CALL_LIMIT follow from that (a relation test checks all three).
SECONDS = 600
ENTER_DELAY = 0.25  # seconds between delivering the code and pressing Enter
# Each attempt stages its own verified executable copy (roughly 350 MiB). Its worker
# removes it once its own child is reaped (every ending but unknown), and Try again
# removes a replaced attempt's copy that is still there (discard_binary), so no copy
# normally outlives its attempt. Ten attempts per setup approval give a slow
# sign-in room to finish, and bound the copies to about 3.5 GiB if every removal failed.
MAX_ATTEMPTS = 10
# The (state, reason) endings after which a new attempt may replace the current one,
# and then only when the persisted record also says no code was delivered
# (code_delivered is 0; a NULL from the single-attempt table never qualifies).
# The worker writes a terminal row as its final act, after reaping its child; a reap
# it cannot prove records unknown/child_quiet_unproven instead. needs_attention/timeout
# is raised in the wait loop, before any token is read or stored, and with no code
# delivered the CLI never had what it needs to mint one. cancelled/effect_uncertain
# (Cancel, or a conversation that drained) has the same proof: with no code delivered
# no token can exist, and the worker reaped its child before writing it; a cancel
# after a code has code_delivered 1. Every other ending keeps the single attempt:
# provider_failed and the output refusals would recur; changed files and
# credential_already_present need the computer; storage_uncertain and environment_*
# can follow a minted token; boundary_or_process_failure names no cause; unknown
# (child_quiet_unproven, interrupted) cannot rule out a live child.
RETRYABLE = frozenset({("needs_attention", "timeout"), ("cancelled", "effect_uncertain")})
# A code is taken only while at least this many seconds of the attempt remain, so a
# late code cannot be cut off mid-exchange by the timeout (a token the provider may
# have minted but this host never stored); the reported expires_at is that cutoff. An
# attempt with no code taken by then ends as a timeout at once, so Try again is
# offered without a dead wait.
CODE_MARGIN = 20
# A begin needs at least this long for a code inside the approval's remaining time,
# or it would only spend an attempt (and its executable copy).
MIN_CODE_WINDOW = 30
_LEGACY_COLUMNS = ["attempt_id","generation","state","reason"]
_COLUMNS = _LEGACY_COLUMNS + ["code_delivered","retired"]
MAX_OUTPUT = 131072
_ROUTING = ("PATH","TMPDIR","XDG_CONFIG_HOME","XDG_CACHE_HOME","XDG_STATE_HOME","XDG_DATA_HOME",
    "HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","http_proxy","https_proxy","all_proxy","no_proxy",
    "SSL_CERT_FILE","SSL_CERT_DIR","NODE_EXTRA_CA_CERTS")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{40,4096}\Z")
_SCOPE_FIELDS = {"binary", "sha256", "version", "home", "account_directory", "environment"}
_TERMINAL = {"connected", "needs_attention", "cancelled", "unknown"}


class SigninRefused(AccessDenied):
    """Finite redacted failure category, never a provider error or output."""


def _node(info):
    return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode),
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private_directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise SigninRefused("private_path_changed")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SigninRefused("private_path_changed")


def _open_file(path, *, executable=False, public_source=False):
    """Public software may be hard-linked; callers must hash it before use.

    Credential files and the private executable copy always remain single-link.
    The complete source node, including link count, is still bound through copy.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink < 1
                or info.st_nlink != 1 and not (executable and public_source)
                or info.st_uid not in ({0, os.getuid()} if executable else {os.getuid()})
                or (info.st_mode & 0o022 if executable else stat.S_IMODE(info.st_mode) != 0o600)
                or executable and not info.st_mode & 0o111 or not executable and info.st_size > 65536
                or _node(info) != _node(path.lstat())):
            raise SigninRefused("file_binding_changed")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def approved_scope(binary, root, account_directory):
    """Local metadata/software read only; no invocation or credential-body read."""
    root = Path(root)
    _private_directory(root)
    home = Path.home()
    profile = Path(account_directory or home / ".claude")
    if not profile.is_absolute() or profile.resolve() != profile or not profile.is_dir():
        raise SigninRefused("profile_changed")
    path = Path(binary).resolve(strict=True)
    fd, before = _open_file(path, executable=True, public_source=True)
    try:
        with os.fdopen(os.dup(fd), "rb") as stream:
            hasher=hashlib.sha256()
            for chunk in iter(lambda:stream.read(1024*1024),b""): hasher.update(chunk)
            digest=hasher.hexdigest()
        if _node(before) != _node(os.fstat(fd)) or _node(before) != _node(path.lstat()) or digest not in SUPPORTED:
            raise SigninRefused("unsupported_provider")
    finally:
        os.close(fd)
    env = root / ".env"
    fd, _ = _open_file(env)
    os.close(fd)
    return {"binary": str(path), "sha256": digest, "version": SUPPORTED[digest], "home": str(home),
            "account_directory": str(profile), "environment": str(env)}


def scope_shape(value):
    return (isinstance(value, dict) and set(value) == _SCOPE_FIELDS
            and all(isinstance(v, str) and v and len(v) <= 4096 for v in value.values())
            and value["sha256"] in SUPPORTED and value["version"] == SUPPORTED[value["sha256"]]
            and all(Path(value[k]).is_absolute() for k in ("binary", "home", "account_directory", "environment")))


def check_scope(scope, root):
    if not scope_shape(scope) or scope["environment"] != str(root / ".env") or scope["home"] != str(Path.home()):
        raise SigninRefused("scope_changed")
    if approved_scope(scope["binary"], root, scope["account_directory"]) != scope:
        raise SigninRefused("scope_changed")


def manual_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return False
    try:
        url = urlsplit(value)
        fields = parse_qs(url.query, strict_parsing=True)
        required = {"code", "client_id", "response_type", "redirect_uri", "scope", "code_challenge", "code_challenge_method", "state"}
        return (url.scheme == "https" and url.netloc == "claude.com" and url.path == "/cai/oauth/authorize"
                and not url.fragment and set(fields) in (required, required | {"orgUUID"})
                and all(len(v) == 1 for v in fields.values()) and fields["code"] == ["true"]
                and fields["response_type"] == ["code"] and fields["scope"] == ["user:inference"]
                and fields["redirect_uri"] == ["https://platform.claude.com/oauth/code/callback"]
                and fields["code_challenge_method"] == ["S256"]
                and re.fullmatch(r"[A-Za-z0-9_-]{43}", fields["code_challenge"][0]) is not None
                and re.fullmatch(r"[A-Za-z0-9_-]{16,256}", fields["state"][0]) is not None
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fields["client_id"][0]) is not None)
    except (ValueError, KeyError):
        return False


# Presentation-only DEC private modes Claude 2.1.325 sets/resets: cursor
# visibility, focus reporting, alternate screen, bracketed paste, theme reports.
_PRIVATE_MODES = frozenset({"25", "1004", "1049", "2004", "2031"})
# Unanswered queries (kitty keyboard, XTVERSION, primary device attributes)
# and the full-screen scroll-region reset; none changes the modelled screen.
_IGNORED_CSI = frozenset({("?", "u"), (">", "q"), (">0", "q"), ("", "c"), ("0", "c"), ("", "r")})


class TokenScreen:
    """Bounded private screen for the inspected Ink output, never a terminal API."""
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._escape = ""
        self._rows = [[] for _ in range(64)]
        self._row = self._col = self._bytes = 0
        self._saved = (0, 0)
        self.url = None
        self._candidate = None

    def __repr__(self):
        return "<private Claude sign-in screen>"

    def _csi(self, value):
        match = re.fullmatch(r"\x1b\[([?>]?[0-9;]*)([A-Za-z])", value)
        if not match:
            raise SigninRefused("unsupported_output")
        raw, final = match.groups()
        if (raw, final) in _IGNORED_CSI:
            return
        if raw[:1] == "?" and final in "hl" and set(raw[1:].split(";")) <= _PRIVATE_MODES:
            return
        if raw[:1] in {"?", ">"}:
            raise SigninRefused("unsupported_output")
        numbers = [int(v or 0) for v in raw.split(";")]
        if any(v > 4096 for v in numbers):
            raise SigninRefused("unsupported_output")
        n = numbers[0] or 1
        if final == "m": return  # Color only; never expose the underlying bytes.
        if final == "A": self._row = max(0, self._row - n)
        elif final == "B": self._row = min(63, self._row + n)
        elif final == "C": self._col = min(4095, self._col + n)
        elif final == "D": self._col = max(0, self._col - n)
        elif final == "G": self._col = min(4095, n - 1)
        elif final in {"H", "f"}: self._row, self._col = min(63, n-1), min(4095, (numbers[1] if len(numbers)>1 else 1)-1)
        elif final == "K":
            if numbers[0] == 2: self._rows[self._row] = []
            elif numbers[0] == 0: self._rows[self._row] = self._rows[self._row][:self._col]
            elif numbers[0] == 1:
                row = self._rows[self._row]
                row[:self._col+1] = [" "] * min(len(row), self._col+1)
            else: raise SigninRefused("unsupported_output")
        elif final == "J":
            if numbers[0] == 2: self._rows = [[] for _ in range(64)]
            elif numbers[0] == 0:
                self._rows[self._row] = self._rows[self._row][:self._col]
                self._rows[self._row+1:] = [[] for _ in range(63-self._row)]
            else: raise SigninRefused("unsupported_output")
        elif final not in {"h", "l"}: raise SigninRefused("unsupported_output")

    def feed(self, data):
        self._bytes += len(data)
        if self._bytes > MAX_OUTPUT: raise SigninRefused("output_limit")
        try: text = self._decoder.decode(data)
        except UnicodeError: raise SigninRefused("unsupported_output") from None
        for c in text:
            if self._escape:
                self._escape += c
                if len(self._escape) > 8192: raise SigninRefused("output_limit")
                if self._escape.startswith("\x1b]"):
                    if c == "\x07" or self._escape.endswith("\x1b\\"):
                        payload = self._escape[2:-1] if c == "\x07" else self._escape[2:-2]
                        link = re.fullmatch(r"8;(?:id=[A-Za-z0-9_.-]{1,128})?;(.*)", payload, re.S)
                        url = link.group(1) if link else None
                        if url and manual_url(url):
                            if self.url is not None and self.url != url: raise SigninRefused("ambiguous_output")
                            self.url = url
                        self._escape = ""
                elif self._escape.startswith("\x1b["):
                    if c.isalpha() or c == "~": self._csi(self._escape); self._escape = ""
                elif self._escape == "\x1b7": self._saved = (self._row, self._col); self._escape = ""  # DECSC
                elif self._escape == "\x1b8": self._row, self._col = self._saved; self._escape = ""  # DECRC
                elif self._escape == "\x1b(": pass  # Awaiting the G0 character set designator.
                elif self._escape == "\x1b(B": self._escape = ""  # G0 = US-ASCII; nothing to model.
                else: raise SigninRefused("unsupported_output")
                continue
            if c == "\x1b": self._escape = c
            elif c == "\r": self._col = 0
            elif c == "\n":
                self._row += 1
                if self._row == 64: self._rows.pop(0); self._rows.append([]); self._row = 63
            elif c == "\b": self._col = max(0, self._col-1)
            elif c in "\x07\x0f": pass  # Bell; shift-in to the (US-ASCII) G0 set.
            elif ord(c) < 32: raise SigninRefused("unsupported_output")
            else:
                row = self._rows[self._row]
                while len(row) <= self._col: row.append(" ")
                row[self._col] = c
                self._col += 1
                if self._col >= 4096: self._col = 0; self._row = min(63, self._row+1)
        candidate = self.token()
        if candidate:
            if self._candidate and candidate != self._candidate: raise SigninRefused("ambiguous_output")
            self._candidate = candidate

    def token(self):
        text = "\n".join("".join(row).rstrip() for row in self._rows)
        marker = "Long-lived authentication token created successfully!"
        if marker not in text: return self._candidate
        matches = re.findall(r"Long-lived authentication token created successfully!\s*Your OAuth token \(valid for[^\n]*\):\s*([A-Za-z0-9_.\-\s]+?)\s*Store this token securely\. You won't be able to see it again\.", text)
        values = {re.sub(r"\s", "", item) for item in matches}
        if len(values) > 1: raise SigninRefused("ambiguous_output")
        if values:
            value = values.pop()
            if not _TOKEN.fullmatch(value): raise SigninRefused("unsupported_output")
            return value
        return self._candidate


def _environment_format(text):
    if any(ord(c)<32 and c not in "\r\n" for c in text): raise SigninRefused("environment_format")
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        match = re.fullmatch(r"(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(?:'[^'\r\n]*'|\"[^\"$`\\\r\n]*\"|[^\s'\"`$\\;|&()<>#\r\n]*)", line)
        if not match: raise SigninRefused("environment_format")
        if match[1] == "CLAUDE_CODE_OAUTH_TOKEN": raise SigninRefused("credential_already_present")


def environment_ready(root):
    fd,before=_open_file(root/".env")
    try:
        raw=os.read(fd,65537)
        try: _environment_format(raw.decode("utf-8"))
        except UnicodeError: raise SigninRefused("environment_format") from None
        if _node(before)!=_node(os.fstat(fd)) or _node(before)!=_node((root/".env").lstat()):
            raise SigninRefused("environment_changed")
    finally: os.close(fd)


def store_token(root, token, admit):
    """Append exactly one literal token, preserving private routing bytes/inode."""
    if not _TOKEN.fullmatch(token): raise SigninRefused("unsupported_output")
    _private_directory(root)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    directory_before=os.fstat(parent)
    directory_binding=lambda i:(i.st_dev,i.st_ino,i.st_uid,stat.S_IMODE(i.st_mode))
    try:
        fd = os.open(".env", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 65536
                or _node(before) != _node(os.stat(".env", dir_fd=parent, follow_symlinks=False))):
            raise SigninRefused("environment_changed")
        raw = os.read(fd, 65537)
        try: text = raw.decode("utf-8")
        except UnicodeError: raise SigninRefused("environment_format") from None
        _environment_format(text)
        admit()
        if _node(before) != _node(os.fstat(fd)) or _node(before) != _node(os.stat(".env", dir_fd=parent, follow_symlinks=False)) or directory_binding(directory_before)!=directory_binding(root.lstat()) or directory_binding(directory_before)!=directory_binding(os.fstat(parent)):
            raise SigninRefused("environment_changed")
        addition = ("" if not raw or raw.endswith(b"\n") else "\n") + "export CLAUDE_CODE_OAUTH_TOKEN='" + token + "'\n"
        os.lseek(fd, 0, os.SEEK_END)
        data = addition.encode("ascii")
        if os.write(fd, data) != len(data): raise SigninRefused("storage_uncertain")
        os.fsync(fd)
        after = os.fstat(fd)
        if (_node(after) != _node(os.stat(".env", dir_fd=parent, follow_symlinks=False))
                or (after.st_dev,after.st_ino,after.st_uid,stat.S_IMODE(after.st_mode),after.st_nlink) != (before.st_dev,before.st_ino,before.st_uid,0o600,1)
                or after.st_size != len(raw)+len(data)
                or directory_binding(directory_before)!=directory_binding(root.lstat())
                or directory_binding(directory_before)!=directory_binding(os.fstat(parent))):
            raise SigninRefused("storage_uncertain")
    finally:
        if fd is not None: os.close(fd)
        os.close(parent)


ATTEMPT_DIRECTORY = "claude-signin-"  # + attempt ID, under staging(root)


def staging(root):
    """The private directory holding each attempt's staged executable copy."""
    return root/'state/provider-signin'


def stage_binary(parent, scope, attempt, source_fd, before):
    """Pin a public executable copy; an updater cannot swap the installed path.

    One nonsecret copy per attempt, never a credential or PTY log.
    No unverified executable path is used after the original descriptor read.
    """
    _private_directory(parent)
    directory=parent/(ATTEMPT_DIRECTORY+attempt)
    directory.mkdir(mode=0o700)  # Exclusive: a retired ID is never admitted again (begin), so no attempt is replayed.
    try:
        _private_directory(directory)
        target=directory/"claude"
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        digest=hashlib.sha256()
        try:
            os.lseek(source_fd,0,os.SEEK_SET)
            while True:
                data=os.read(source_fd,1024*1024)
                if not data: break
                digest.update(data)
                pending=memoryview(data)
                while pending:
                    written=os.write(fd,pending)
                    if not written: raise SigninRefused("artifact_copy_failed")
                    pending=pending[written:]
            os.fsync(fd)
            if (digest.hexdigest()!=scope['sha256'] or _node(before)!=_node(os.fstat(source_fd))
                    or _node(before)!=_node(Path(scope['binary']).lstat())):
                raise SigninRefused("unsupported_provider")
            os.fchmod(fd,0o500)
        finally: os.close(fd)
        pinned,info=_open_file(target,executable=True)
    except BaseException:
        discard_binary(parent,attempt)  # only what this call just created; it never raises
        raise
    return target,pinned,info


def discard_binary(parent, attempt):
    """Best effort: remove an ended attempt's staged copy and its directory.

    Only once the attempt's own child is reaped: by its worker, after that reap and
    closing its descriptors; by a retry retiring it, whose record proves the worker
    did; or by stage_binary for a copy it could not finish. Nothing is followed. Only a single-link regular file this
    account owns is removed, then the directory if that left it empty; anything
    else is left alone. It never raises.
    """
    try:
        from .owner_setup import _uuid  # the attempt-ID check begin's caller already applied
        if not _uuid(attempt): return
        _private_directory(parent)  # the same whole-path check stage_binary applies
        name=ATTEMPT_DIRECTORY+attempt
        parent_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            directory=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent_fd)
            try:
                try: info=os.stat("claude",dir_fd=directory,follow_symlinks=False)
                except FileNotFoundError: info=None  # an emptied directory is still removed
                if info is not None and stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1:
                    os.unlink("claude",dir_fd=directory)
            finally: os.close(directory)
            os.rmdir(name,dir_fd=parent_fd)  # refused unless empty
        finally: os.close(parent_fd)
    except Exception: pass


class ProviderSignin:
    """One current attempt bound to one SetupEngine; no restart recovery.

    Only a RETRYABLE ending admits a successor attempt ID (``begin``).
    """
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._thread = None
        self._cancel = threading.Event()
        self._code = None
        self._attempt = None
        self._url = None
        self._closed = False  # set by shutdown: this process starts no successor
        self._expires_at = None  # whole host-clock seconds: the last moment a code is taken
        self._code_until = None  # the same cutoff on the monotonic clock, which code() enforces

    @staticmethod
    def initialize(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS setup_provider(attempt_id TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,code_delivered INTEGER,retired INTEGER NOT NULL DEFAULT 0)")
        ProviderSignin._migrate(conn)

    @staticmethod
    def _schema(conn):
        """True for the current table, False for the single-attempt table it extends."""
        columns=[row[1] for row in conn.execute("PRAGMA table_info(setup_provider)")]
        if columns not in (_COLUMNS,_LEGACY_COLUMNS): raise AccessUnavailable()
        return columns == _COLUMNS

    @staticmethod
    def _migrate(conn):
        """Upgrade only inside a write transaction (allow, begin); reads accept either shape.

        A row from the single-attempt table never recorded whether its code was
        delivered, so it keeps NULL there, which no retry accepts.
        """
        if not ProviderSignin._schema(conn):
            conn.execute("ALTER TABLE setup_provider ADD COLUMN code_delivered INTEGER")
            conn.execute("ALTER TABLE setup_provider ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
            if not ProviderSignin._schema(conn): raise AccessUnavailable()

    @staticmethod
    def _current(conn):
        """The one current (unretired) row or None, and how many attempts exist."""
        extra = "code_delivered,retired" if ProviderSignin._schema(conn) else "NULL AS code_delivered,0 AS retired"
        rows = conn.execute("SELECT attempt_id,generation,state,reason,"+extra+" FROM setup_provider").fetchall()
        current = [row for row in rows if row['retired'] == 0]
        if (len(current)>1 or rows and not current
                or any(row['retired'] not in (0,1) or row['code_delivered'] not in (None,0,1) for row in rows)):
            raise AccessUnavailable()
        return (current[0] if current else None), len(rows)

    def _row(self, conn, grant, attempt):
        row,_ = self._current(conn)
        if row is None or row['attempt_id'] != attempt or row['generation'] != grant.generation:
            raise AccessDenied("signin_original_attempt_required")
        return row

    def _set(self, grant, attempt, state, reason=None):
        with self.store.access._connection(write=True) as conn:
            self._row(conn,grant,attempt)
            conn.execute("UPDATE setup_provider SET state=?,reason=? WHERE attempt_id=?", (state,reason,attempt))

    def _mark_delivered(self, grant, attempt):
        # Committed before the first code byte reaches the CLI: from then on a token may
        # exist, and no restart can read this attempt as one whose code was never sent.
        with self.store.access._connection(write=True) as conn:
            self._row(conn,grant,attempt)
            if not self._schema(conn): raise AccessUnavailable()
            conn.execute("UPDATE setup_provider SET code_delivered=1 WHERE attempt_id=?", (attempt,))

    @staticmethod
    def _retryable(row, attempts):
        """The record's half of a retry: a RETRYABLE ending, no code delivered, attempts left."""
        return (row['state'],row['reason']) in RETRYABLE and row['code_delivered'] == 0 and attempts < MAX_ATTEMPTS

    def _room(self, grant):
        """Does the approval leave a useful code window for a new attempt? Begin and status share it."""
        return grant.expires_at-self.store.access._now() >= CODE_MARGIN+MIN_CODE_WINDOW

    def _settled(self, attempt):
        """Under the lock, for a row read as terminal: has this process's worker returned?

        A terminal row is its worker's final write, so at most the thread's own return
        is still pending; it needs nothing this lock guards, and the join is bounded.
        """
        thread = self._thread
        if (thread is not None and thread.is_alive() and self._attempt == attempt
                and thread is not threading.current_thread()):
            thread.join(timeout=1)
        return thread is None or not thread.is_alive()

    def _admit(self, reader, grant):
        self.store.authorize(reader,expected=grant,effects=True)
        from .owner_setup import managed_setup
        package=managed_setup(self.store.access.root,grant)
        from ..installation import _check_maintenance
        _check_maintenance(package.installation,native=True)

    @contextmanager
    def _runtime(self,reader,grant):
        from ..installation import _native_lock, _check_maintenance, Installation
        installation=Installation(Path(grant.installation),self.store.access.root)
        fd=_native_lock(installation)
        try:
            self._admit(reader,grant)
            _check_maintenance(installation,native=True)
            yield
        finally: os.close(fd)

    def begin(self, reader, grant, attempt):
        with self._lock:
            self._admit(reader,grant)
            started = replaces = False  # set only inside the committed admission below
            with self.store.access._connection(write=True) as conn:
                self._migrate(conn)
                current,attempts = self._current(conn)
                if current is not None and current['attempt_id'] == attempt:
                    self._row(conn,grant,attempt)  # Original identity is retained even after a restart.
                else:
                    if current is not None:
                        # Another ID replaces the current attempt only after a RETRYABLE ending
                        # with no worker of it still running here, and never reuses a retired ID.
                        # The persisted terminal row is the proof for a worker in another process.
                        if (current['generation'] != grant.generation or not self._retryable(current,attempts)
                                or not self._settled(current['attempt_id'])
                                or conn.execute("SELECT 1 FROM setup_provider WHERE attempt_id=?", (attempt,)).fetchone()):
                            raise AccessDenied("signin_original_attempt_required")
                        if self._closed: raise AccessUnavailable()
                    if not self._room(grant):
                        raise AccessDenied("signin_approval_ending")  # refused before anything is staged
                    check_scope(grant.provider_signin,self.store.access.root)
                    environment_ready(self.store.access.root)  # A saved credential still refuses any attempt.
                    if current is not None:
                        conn.execute("UPDATE setup_provider SET retired=1 WHERE attempt_id=?", (current['attempt_id'],))
                    conn.execute("INSERT INTO setup_provider VALUES(?,?,?,NULL,0,0)", (attempt,grant.generation,"starting"))
                    started, replaces = True, current is not None
            if started:
                # Only after the commit, so a refused or rolled-back admission leaves no worker.
                if replaces:
                    # A fresh event: the ended attempt's may be set (Cancel or a drain), and a set
                    # event would stop the new worker at once. Nothing of its link or code carries over.
                    self._cancel = threading.Event()
                    self._code = self._url = self._expires_at = self._code_until = None
                self._attempt = attempt
                self._thread = threading.Thread(target=self._run,args=(reader,grant,attempt),daemon=False)
                self._thread.start()
                if replaces: discard_binary(staging(self.store.access.root),current['attempt_id'])
            return self.status(reader,grant,attempt)

    def status(self, reader, grant, attempt):
        self._admit(reader,grant)
        with self._lock, self.store.access._connection() as conn:
            row = self._row(conn,grant,attempt)
            state, reason = row['state'], row['reason']
            if state not in _TERMINAL and (attempt != self._attempt or self._thread is None or not self._thread.is_alive()):
                state,reason = "unknown","interrupted"
            result = {"attempt_id":attempt,"generation":grant.generation,"state":state,"reason":reason}
            if state == "waiting" and self._url:
                result["url"] = self._url
                # The deadline the worker enforces, so the browser shows the host's own time.
                if self._expires_at is not None: result["expires_at"] = self._expires_at
            # Present only when begin would now admit a new attempt ID; begin re-checks it all.
            if self._retryable(row,self._current(conn)[1]) and self._room(grant) and self._settled(attempt):
                result["retryable"] = True
            return result

    def code(self, reader, grant, attempt, code):
        with self._lock:
            current = self.status(reader,grant,attempt)
            if (current['state'] != 'waiting' or self._code is not None or not self._url
                    or self._code_until is None or time.monotonic() >= self._code_until):
                raise AccessDenied("signin_code_unavailable")
            parts = code.split('#') if isinstance(code,str) else []
            state = parse_qs(urlsplit(self._url).query)['state'][0]
            if len(parts)!=2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}",parts[0]) or parts[1]!=state:
                raise AccessDenied("signin_code_invalid")
            self._code = code
        return self.status(reader,grant,attempt)

    def cancel(self, reader, grant, attempt):
        with self._lock:
            self.status(reader,grant,attempt)  # Only the current attempt; a refusal ends here.
            # Set whatever its state (after the end it is inert), and taken with its worker
            # under one lock: a successor admitted later gets a fresh event. No close.
            self._cancel.set()
            thread=self._thread
        self._join(thread)
        return self.status(reader,grant,attempt)

    def shutdown(self):
        # The same lock orders cancellation against both effect admissions.
        # Never hold it while joining: the worker needs it to finish/drain.
        with self._lock:
            self._closed=True
            self._cancel.set()
            thread=self._thread
        self._join(thread)

    @staticmethod
    def _join(thread):
        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
            thread.join()  # Worker has a finite deadline and bounded owned-child discharge.

    def summary(self, reader, grant):
        with self.store.access._connection() as conn:
            row,_=self._current(conn)
        return None if row is None else self.status(reader,grant,row["attempt_id"])

    def ready(self, reader, grant):
        with self.store.access._connection() as conn:
            row,_=self._current(conn)
        if row is None or self.status(reader,grant,row['attempt_id'])['state'] != 'connected':
            raise AccessDenied("signin_not_complete")

    def _run(self, reader, grant, attempt):
        process = None
        master = slave = binary_fd = staged_fd = None
        staged = staged_info = None
        screen = TokenScreen()
        state,reason = "needs_attention","provider_failed"
        try:
            self._admit(reader,grant)
            with self._lock:
                if self._cancel.is_set(): raise SigninRefused("effect_uncertain")
            scope=grant.provider_signin
            check_scope(scope,self.store.access.root)
            binary_fd,before=_open_file(Path(scope['binary']),executable=True,public_source=True)
            parent=staging(self.store.access.root)
            parent.mkdir(mode=0o700,exist_ok=True)
            staged,staged_fd,staged_info=stage_binary(parent,scope,attempt,binary_fd,before)
            master,slave=pty.openpty()
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',64,4096,0,0))
            env={key:os.environ[key] for key in _ROUTING if key in os.environ}
            env.update({"HOME":scope['home'],"CLAUDE_CONFIG_DIR":scope['account_directory'],
                 "BROWSER":"/usr/bin/true","TERM":"xterm-256color","NO_COLOR":"1","COLUMNS":"4096","LINES":"64",
                 "DISABLE_AUTOUPDATER":"1","CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1"})
            self._admit(reader,grant)
            if _node(staged_info)!=_node(staged.lstat()): raise SigninRefused("unsupported_provider")
            with self._lock:
                if self._cancel.is_set(): raise SigninRefused("effect_uncertain")
                with self._runtime(reader,grant):
                    environment_ready(self.store.access.root)
                    process=subprocess.Popen([str(staged),'setup-token'],cwd=self.store.access.root,env=env,
                        stdin=slave,stdout=slave,stderr=slave,start_new_session=True,close_fds=True)
            os.close(slave); slave=None
            # The code window closes CODE_MARGIN seconds before the attempt's timeout or the
            # approval's expiry (which also ends it). Host-clock time is read first and the
            # result floored, so the reported expiry is never later than the enforced cutoff.
            wall=self.store.access._now(); begun=time.monotonic()
            deadline=begun+SECONDS
            window=min(SECONDS,grant.expires_at-wall)-CODE_MARGIN
            with self._lock: self._code_until,self._expires_at=begun+window,math.floor(wall+window)
            enter_at=None  # when the Enter after a delivered code is due
            delivered=False
            while True:
                if self._cancel.is_set(): state,reason="cancelled","effect_uncertain";break
                self._admit(reader,grant)  # Revocation/expiry stops the owned attempt, even with no browser polling.
                if time.monotonic()>=deadline: raise SigninRefused("timeout")
                if select.select([master],[],[],0.1)[0]:
                    try: data=os.read(master,16384)
                    except OSError as exc:
                        if exc.errno!=errno.EIO: raise
                        data=b''
                    if data: screen.feed(data)
                    elif process.poll() is not None: break
                with self._lock:
                    # select/admission above may have raced cancellation or
                    # revocation. Delivering the code can trigger token minting.
                    if self._cancel.is_set(): state,reason="cancelled","effect_uncertain";break
                    if not delivered and self._code is None and time.monotonic()>=self._code_until:
                        # No code can be taken any more and none was delivered: the same clean
                        # timeout now, rather than a waiting link nobody can use until the deadline.
                        raise SigninRefused("timeout")
                    if screen.url and not self._url:
                        self._url=screen.url; self._set(grant,attempt,"waiting")
                    if self._code is not None:
                        self._admit(reader,grant)
                        self._mark_delivered(grant,attempt)  # before any code byte reaches the CLI
                        delivered=True
                        code,self._code=self._code,None
                        os.write(master,code.encode('ascii'))
                        code=None; enter_at=time.monotonic()+ENTER_DELAY
                        self._set(grant,attempt,"starting")
                    elif enter_at is not None and time.monotonic()>=enter_at:
                        # Claude's raw-mode prompt submits only on Enter (CR); LF never does. Enter goes
                        # as its own keypress after the code, so the code can't be read as pasted text.
                        self._admit(reader,grant)
                        os.write(master,b'\r'); enter_at=None
                if process.poll() is not None:
                    # Drain the remaining finite PTY output before extracting a token.
                    while select.select([master],[],[],0)[0]:
                        try: data=os.read(master,16384)
                        except OSError as exc:
                            if exc.errno!=errno.EIO: raise
                            break
                        if not data: break
                        screen.feed(data)
                    break
            if reason!='effect_uncertain':
                token=screen.token()
                if process.poll()==0 and token:
                    with self._lock:
                        if self._cancel.is_set(): raise SigninRefused("effect_uncertain")
                        if _node(staged_info)!=_node(os.fstat(staged_fd)) or _node(staged_info)!=_node(staged.lstat()):
                            raise SigninRefused("unsupported_provider")
                        with self._runtime(reader,grant):
                            store_token(self.store.access.root,token,lambda:self._admit(reader,grant))
                        # An admitted completed commit remains true even if a
                        # cancellation is accepted immediately after this lock.
                        token=None;state,reason="connected",None
                elif process.poll()==0: reason="unsupported_output"
        except SigninRefused as exc:
            reason=str(exc)
            if reason=="effect_uncertain" and self._cancel.is_set(): state="cancelled"
        except BaseException:
            # Provider text, code, output and filesystem exceptions are never serialized.
            state,reason="needs_attention","boundary_or_process_failure"
        finally:
            try:
                if process is not None and process.poll() is None:
                    # The unreaped child pins the PID; never signal a replacement.
                    if os.getpgid(process.pid)==process.pid: os.killpg(process.pid,signal.SIGTERM)
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        if process.poll() is None and os.getpgid(process.pid)==process.pid: os.killpg(process.pid,signal.SIGKILL)
                        process.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired):
                state,reason="unknown","child_quiet_unproven"
            for fd in (slave,master,binary_fd,staged_fd):
                if fd is not None: os.close(fd)
            # Only the copy this worker staged, its child reaped and descriptors closed above;
            # removed before the terminal row, which stays the worker's final write. Unknown keeps it.
            if staged is not None and state!="unknown": discard_binary(staging(self.store.access.root),attempt)
            with self._lock:
                self._code=self._url=self._expires_at=self._code_until=None
                self._set(grant,attempt,state,reason)
