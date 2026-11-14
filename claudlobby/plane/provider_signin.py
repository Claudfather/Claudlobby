"""One locally approved Claude CLI setup-token attempt; secrets never leave this host.

The recognizer is pinned to inspected public distribution bytes, not a private
OAuth API. PTY contents, pasted codes and tokens are memory-only. A persisted
interrupted attempt is uncertain and can never be dispatched again.
"""
from __future__ import annotations

import codecs
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
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
}
SECONDS = 300
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


def _open_file(path, *, executable=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
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
    fd, before = _open_file(path, executable=True)
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


class TokenScreen:
    """Bounded private screen for the inspected Ink output, never a terminal API."""
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._escape = ""
        self._rows = [[] for _ in range(64)]
        self._row = self._col = self._bytes = 0
        self.url = None
        self._candidate = None

    def __repr__(self):
        return "<private Claude sign-in screen>"

    def _csi(self, value):
        match = re.fullmatch(r"\x1b\[([0-9;?]*)([A-Za-z])", value)
        if not match:
            raise SigninRefused("unsupported_output")
        raw, final = match.groups()
        if raw.startswith("?"):  # Cursor visibility, bracketed paste, alternate screen.
            if final not in "hl" or raw not in {"?25", "?2004", "?1049"}:
                raise SigninRefused("unsupported_output")
            return
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
                        if payload.startswith("8;;") and manual_url(payload[3:]):
                            if self.url is not None and self.url != payload[3:]: raise SigninRefused("ambiguous_output")
                            self.url = payload[3:]
                        self._escape = ""
                elif self._escape.startswith("\x1b["):
                    if c.isalpha() or c == "~": self._csi(self._escape); self._escape = ""
                elif len(self._escape) == 2 and c not in "[]": raise SigninRefused("unsupported_output")
                continue
            if c == "\x1b": self._escape = c
            elif c == "\r": self._col = 0
            elif c == "\n":
                self._row += 1
                if self._row == 64: self._rows.pop(0); self._rows.append([]); self._row = 63
            elif c == "\b": self._col = max(0, self._col-1)
            elif c == "\x07": pass
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


def stage_binary(parent, scope, attempt, source_fd, before):
    """Pin a public executable copy; an updater cannot swap the installed path.

    One nonsecret copy is retained per attempt, never a credential or PTY log.
    No unverified executable path is used after the original descriptor read.
    """
    _private_directory(parent)
    directory=parent/("claude-signin-"+attempt)
    directory.mkdir(mode=0o700)  # Exclusive: retained/unknown attempts are never replayed.
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
    return target,pinned,info


class ProviderSignin:
    """One attempt bound to one SetupEngine; no restart or replacement recovery."""
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._thread = None
        self._cancel = threading.Event()
        self._code = None
        self._attempt = None
        self._url = None

    @staticmethod
    def initialize(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS setup_provider(attempt_id TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,reason TEXT)")
        ProviderSignin._schema(conn)

    @staticmethod
    def _schema(conn):
        columns=[row[1] for row in conn.execute("PRAGMA table_info(setup_provider)")]
        if columns != ["attempt_id","generation","state","reason"]: raise AccessUnavailable()

    def _row(self, conn, grant, attempt):
        self._schema(conn)
        rows = conn.execute("SELECT * FROM setup_provider LIMIT 2").fetchall()
        if len(rows)>1: raise AccessUnavailable()
        row=rows[0] if rows else None
        if row is None or row['attempt_id'] != attempt or row['generation'] != grant.generation:
            raise AccessDenied("signin_original_attempt_required")
        return row

    def _set(self, grant, attempt, state, reason=None):
        with self.store.access._connection(write=True) as conn:
            self._row(conn,grant,attempt)
            conn.execute("UPDATE setup_provider SET state=?,reason=? WHERE attempt_id=?", (state,reason,attempt))

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
            with self.store.access._connection(write=True) as conn:
                self._schema(conn)
                rows = conn.execute("SELECT * FROM setup_provider").fetchall()
                if rows:
                    self._row(conn,grant,attempt)  # Original identity is retained even after a restart.
                else:
                    check_scope(grant.provider_signin,self.store.access.root)
                    environment_ready(self.store.access.root)
                    conn.execute("INSERT INTO setup_provider VALUES(?,?,?,NULL)", (attempt,grant.generation,"starting"))
                    self._attempt = attempt
                    self._thread = threading.Thread(target=self._run,args=(reader,grant,attempt),daemon=False)
            if self._thread is not None and self._thread.ident is None:
                self._thread.start()
            return self.status(reader,grant,attempt)

    def status(self, reader, grant, attempt):
        self._admit(reader,grant)
        with self._lock, self.store.access._connection() as conn:
            row = self._row(conn,grant,attempt)
            state, reason = row['state'], row['reason']
            if state not in _TERMINAL and (attempt != self._attempt or self._thread is None or not self._thread.is_alive()):
                state,reason = "unknown","interrupted"
            result = {"attempt_id":attempt,"generation":grant.generation,"state":state,"reason":reason}
            if state == "waiting" and self._url: result["url"] = self._url
            return result

    def code(self, reader, grant, attempt, code):
        with self._lock:
            current = self.status(reader,grant,attempt)
            if current['state'] != 'waiting' or self._code is not None or not self._url:
                raise AccessDenied("signin_code_unavailable")
            parts = code.split('#') if isinstance(code,str) else []
            state = parse_qs(urlsplit(self._url).query)['state'][0]
            if len(parts)!=2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}",parts[0]) or parts[1]!=state:
                raise AccessDenied("signin_code_invalid")
            self._code = code
        return self.status(reader,grant,attempt)

    def cancel(self, reader, grant, attempt):
        with self._lock:
            current=self.status(reader,grant,attempt)
            if current['state'] not in _TERMINAL:
                self._cancel.set()
        self.shutdown()
        return self.status(reader,grant,attempt)

    def shutdown(self):
        # The same lock orders cancellation against both effect admissions.
        # Never hold it while joining: the worker needs it to finish/drain.
        with self._lock:
            self._cancel.set()
            thread=self._thread
        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
            thread.join()  # Worker has a finite deadline and bounded owned-child discharge.

    def summary(self, reader, grant):
        with self.store.access._connection() as conn:
            row=conn.execute("SELECT * FROM setup_provider").fetchone()
        return None if row is None else self.status(reader,grant,row["attempt_id"])

    def ready(self, reader, grant):
        with self.store.access._connection() as conn:
            row=conn.execute("SELECT * FROM setup_provider").fetchone()
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
            binary_fd,before=_open_file(Path(scope['binary']),executable=True)
            staging=self.store.access.root/'state/provider-signin'
            staging.mkdir(mode=0o700,exist_ok=True)
            staged,staged_fd,staged_info=stage_binary(staging,scope,attempt,binary_fd,before)
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
            deadline=time.monotonic()+SECONDS
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
                    if screen.url and not self._url:
                        self._url=screen.url; self._set(grant,attempt,"waiting")
                    if self._code is not None:
                        self._admit(reader,grant)
                        code,self._code=self._code,None
                        os.write(master,(code+'\n').encode('ascii'))
                        code=None
                        self._set(grant,attempt,"starting")
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
            with self._lock:
                self._code=self._url=None
                self._set(grant,attempt,state,reason)
