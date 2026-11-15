"""Typed presentation for the finite first-team guide, not owner RPC.

The caller admits the local operator before constructing this adapter. Storage
policy and canonical transactions remain in the existing owner modules.
"""
from dataclasses import asdict
from contextlib import contextmanager
import asyncio
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys

from ..plane.owner_consent import TrustedWorkspacePresenter
from .owner_stdio import StdioRefused, _unique


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _directory(path, *, private=True):
    if path.resolve() != path:
        raise StdioRefused("prerequisite_changed")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
            or private and stat.S_IMODE(info.st_mode) != 0o700):
        raise StdioRefused("prerequisite_changed")
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _private_file(path, *, read=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise StdioRefused("prerequisite_changed")
        return os.read(fd, 65537) if read else None
    finally:
        os.close(fd)


class FixedOwnerProfile:
    """One account-database profile; a preparation receipt never licenses repair."""
    def __init__(self, paths, request):
        try:
            uid = os.getuid()
            if (uid == 0 or uid != os.geteuid() or uid != request["expected_uid"]
                    or not sys.platform.startswith("linux")):
                raise StdioRefused("account_changed")
            account = pwd.getpwuid(uid)
            self.home = Path(account.pw_dir)
            self.account_name, self.uid = account.pw_name, uid
            if not self.home.is_absolute():
                raise StdioRefused("account_changed")
            _directory(self.home, private=False)
            self.family = self.home / ".local/share/claudlobby-browser"
            self.install, self.data, self.staging = (self.family / name for name in ("install", "data", "staging"))
            for path in (self.install, self.data, self.staging):
                _directory(path)
            installation = paths.package.installation
            if (installation is None or installation.directory != self.install or installation.data_root != self.data
                    or paths.root != self.data or paths.package.source_revision != request["source_revision"]):
                raise StdioRefused("installation_changed")
            receipt = json.loads(_private_file(self.install / "preparation.json", read=True), object_pairs_hook=_unique)
            fields = {"schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256"}
            if (set(receipt) != fields | {"revision", "lock_sha256", "environment_owned", "complete"}
                    or type(receipt["schema"]) is not int or receipt["schema"] != 1
                    or receipt["purpose"] != "installation-preparation-only"
                    or receipt["installation"] != str(self.install) or receipt["data_root"] != str(self.data)
                    or receipt["remote"] != "https://github.com/Claudfather/Claudlobby.git"
                    or not isinstance(receipt["ref"], str) or re.fullmatch(r"[0-9a-f]{40}", receipt["ref"]) is None
                    or receipt["revision"] != receipt["ref"] or receipt["complete"] is not True
                    or receipt["environment_owned"] is not True
                    or not isinstance(receipt["lock_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", receipt["lock_sha256"]) is None):
                raise StdioRefused("installation_changed")
            binding = {key: receipt[key] for key in fields}
            if hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest() != request["binding_sha256"]:
                raise StdioRefused("installation_changed")
            interpreter = Path(receipt["interpreter"])
            if (not interpreter.is_absolute() or interpreter.resolve() != interpreter
                    or interpreter.is_relative_to(self.family)
                    or not interpreter.is_file() or _digest(interpreter) != receipt["interpreter_sha256"]):
                raise StdioRefused("installation_changed")
            if request["finish_setup"] is None and receipt["ref"] != request["source_revision"]:
                raise StdioRefused("installation_changed")
            self.project = self.home / "claudfather-projects/first-team"
            self.account = self.family / "provider-profile"
            self.policy = self.family / "host-policy/system.yaml"
            self.provider = self.family / "provider-bin/claude"
            self.units = self.home / ".config/systemd/user"
            self.request = dict(request)
            with self.environment():
                if installation.preflight(expected_revision=request["source_revision"]) != request["source_revision"]:
                    raise StdioRefused("installation_changed")
        except (OSError, ValueError, KeyError, TypeError):
            raise StdioRefused("installation_changed") from None

    @contextmanager
    def environment(self):
        """Called only after inherited/generated and native ancestry admission."""
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update({"HOME": str(self.home), "PATH": str(self.install / "env/bin") + ":/usr/bin:/bin",
            "CLAUDLOBBY_INSTALLATION": str(self.install), "CLAUDLOBBY_ROOT": str(self.data),
            "LANG": "C.UTF-8", "TERM": "dumb", "TMPDIR": str(self.staging),
            "CLAUDLOBBY_HOST_SYSTEM_YAML": str(self.policy), "CLAUDE_CONFIG_DIR": str(self.account),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_RUNTIME_DIR": "/run/user/" + str(os.getuid()),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + str(os.getuid()) + "/bus",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1", "PLANE_EMIT_DISABLED": "1"})
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def prepare_provider(self, consent, paths):
        """New explicit public-byte consent, never an auth/model retry owner."""
        from .. import provider_install
        observed = provider_install.status(self.account_name, self.uid)
        if observed.state == "ready":
            return observed  # Canonical exact bytes are a read-only no-op.
        if observed.state != "missing":
            raise StdioRefused("provider_install_" + observed.state)
        if getattr(self, "_provider_attempted", False):
            raise StdioRefused("provider_install_unknown")
        consent.approve("install_provider", {"version": observed.version, "platform": observed.platform,
            "sha256": observed.sha256, "binary": str(self.provider), "download_host": "downloads.claude.ai",
            "account_uid": self.uid, "source_revision": self.request["source_revision"],
            "binding_sha256": self.request["binding_sha256"],
            "effects": ["download and install this immutable public Claude executable on this host only",
                        "no provider sign-in, model work, service start or messages"]})
        consent.check_cancel()
        # The decision cannot authorize changed source/account/preparation or
        # filesystem state. A concurrent exact install needs no second write.
        FixedOwnerProfile(paths, self.request)
        current = provider_install.status(self.account_name, self.uid)
        if current.state == "ready":
            return current
        if current != observed:
            raise StdioRefused("provider_install_changed")
        consent.state("provider_installing", provider_version=observed.version, platform=observed.platform)
        consent.check_cancel()
        self._provider_attempted = True
        result = consent.install_provider(lambda cancel: provider_install.prepare(self.account_name, self.uid, cancel=cancel))
        if result.state != "ready":
            raise StdioRefused("provider_install_" + result.state)
        return result

    def prepare_directories(self, consent, paths):
        """Display every create-only fixed-directory effect before any write."""
        from .host_owner_setup import _native_directories
        if self.units not in _native_directories(paths):
            raise StdioRefused("native_manager_prerequisite_missing")
        directories = (self.project.parent, self.project, self.account, self.policy.parent, self.provider.parent,
                       self.units.parent.parent, self.units.parent, self.units)
        missing, retained = [], {}
        for path in dict.fromkeys(directories):
            if os.path.lexists(path):
                retained[path] = _directory(path, private=path not in (self.units.parent.parent, self.units.parent, self.units))
            else:
                missing.append(path)
        if self.project.exists() and any(self.project.iterdir()):
            raise StdioRefused("project_prerequisite_changed")
        env = self.data / ".env"
        make_env = not os.path.lexists(env)
        if not make_env:
            _private_file(env)  # Metadata only; never read credential contents.
        if missing or make_env:
            consent.approve("prepare_directories", {"directories": [str(p) for p in missing],
                "empty_private_environment": str(env) if make_env else None,
                "provider_directory": str(self.provider.parent),
                "effects": ["create only these fixed owned directories and empty private environment; no downloads or root setup"]})
            for path, identity in retained.items():
                if _directory(path, private=path not in (self.units.parent.parent, self.units.parent, self.units)) != identity:
                    raise StdioRefused("prerequisite_changed")
            if any(os.path.lexists(path) for path in missing) or make_env and os.path.lexists(env):
                raise StdioRefused("prerequisite_changed")
            for path in missing:
                path.mkdir(mode=0o700)
            if make_env:
                fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)

    def origin(self):
        """Read the official native daemon's own HTTPS identity; never a form URL."""
        from ..plane.owner_ingress import ServePrincipalVerifier
        async def inspect():
            verifier = ServePrincipalVerifier(tailscale_binary=Path("/usr/bin/tailscale"))
            async def read():
                await verifier._realm()
                value = await verifier._command("status", "--json")
                await verifier._realm()
                return value
            return await asyncio.wait_for(read(), 15)
        value = asyncio.run(inspect())
        own = value.get("Self", {})
        if not isinstance(own, dict):
            raise StdioRefused("native_ingress_prerequisite_missing")
        dns = own.get("DNSName")
        if (value.get("BackendState") != "Running" or own.get("Online") is not True
                or own.get("Expired", False) is not False or not isinstance(dns, str)
                or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net\.", dns) is None):
            raise StdioRefused("native_ingress_prerequisite_missing")
        return "https://" + dns[:-1]


class StdioGuideConsent(TrustedWorkspacePresenter):
    def __init__(self, channel):
        self.channel = channel

    def approve(self, action, scope, *, original=True, expires_at=None):
        return self.channel.approve(action, scope, original=original, expires_at=expires_at)

    def approve_workspace(self, proposal):
        return self.approve("bind_workspace", asdict(proposal),
                            original=proposal, expires_at=proposal.expires_at)

    def code(self, kind):
        return self.channel.token(kind)

    def state(self, state, **data):
        self.channel.emit("state", state=state, **data)

    def check_cancel(self):
        self.channel.check_cancel()

    def install_provider(self, operation):
        return self.channel.install_provider(operation)
