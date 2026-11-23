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

from ..bootstrap import trusted_system_python
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


def admitted_update(installation, receipt_revision, revision, named):
    """The finished update that moved this computer from its receipt's revision to ``revision``.

    ``named`` is the computer run's ``update_record``: the request id and target
    the client read. Admitted only when that is the record on disk, the record
    is ``done`` as strictly as today's finish tuple reads one (health errors an
    empty table, out of maintenance, configuration committed, no stage error),
    it ends at ``revision``, and the receipt's revision is an ancestor of
    ``revision``. Anything else refuses. Reads only.
    """
    from ..git_update import UpdateError, _git, healthy_done, read_update
    try:
        record = read_update(installation)
        if (type(named) is not list or record is None
                or [record.get("request_id"), record.get("target")] != named
                or not healthy_done(record)
                or record["target"] != revision):
            raise ValueError()
        if _git(installation, "merge-base", "--is-ancestor", receipt_revision, revision, check=False).returncode:
            raise ValueError()
    except (UpdateError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StdioRefused("installation_changed") from None
    return record


class FixedOwnerProfile:
    """One account-database profile; a preparation receipt never licenses repair."""
    def __init__(self, paths, request, *, updated_source=False, computer=False):
        """``updated_source`` is the one-operation SSH API only: it carries no
        finish tuple, and the installed revision is still compared exactly.

        ``computer`` is the computer run. It carries no finish tuple. At the
        receipt's revision it is admitted as today's guide is; at a later
        installed revision only when its ``update_record`` names the finished
        update that moved the computer there (``admitted_update``)."""
        try:
            uid = os.getuid()
            if (uid == 0 or uid != os.geteuid() or uid != request["expected_uid"]
                    or not (sys.platform.startswith("linux") or sys.platform == "darwin")):
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
            self.runtime = None
            self.installation = installation
            if sys.platform == "darwin":
                from ..installation import managed_browser_runtime
                self.runtime = managed_browser_runtime(installation)
                if (self.runtime is None or self.runtime.uid != uid
                        or self.runtime.account != self.account_name or self.runtime.home != self.home
                        or Path(sys.executable).resolve() != self.runtime.python):
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
                    or self.runtime is None and interpreter.is_relative_to(self.family)
                    or self.runtime is not None and interpreter != self.runtime.python
                    or not interpreter.is_file() or not isinstance(receipt["interpreter_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", receipt["interpreter_sha256"]) is None
                    or self.runtime is not None and _digest(interpreter) != receipt["interpreter_sha256"]):
                raise StdioRefused("installation_changed")
            # Linux: trusted by place and ownership; the recorded digest stays bound.
            if self.runtime is None and not trusted_system_python(interpreter):
                raise StdioRefused("installation_changed")
            if computer and request["finish_setup"] is not None:
                raise StdioRefused("installation_changed")
            if (request["finish_setup"] is None and not updated_source and not computer
                    and receipt["ref"] != request["source_revision"]):
                raise StdioRefused("installation_changed")
            self.receipt_revision = receipt["ref"]
            self.project = self.home / "claudfather-projects/first-team"
            self.account = self.family / "provider-profile"
            self.policy = self.family / "host-policy/system.yaml"
            self.provider = self.family / "provider-bin/claude"
            self.units = self.home / ("Library/LaunchAgents" if self.runtime is not None else ".config/systemd/user")
            self.request = dict(request)
            with self.environment():
                if installation.preflight(expected_revision=request["source_revision"]) != request["source_revision"]:
                    raise StdioRefused("installation_changed")
                if computer and receipt["ref"] != request["source_revision"]:
                    admitted_update(installation, receipt["ref"], request["source_revision"], request["update_record"])
        except (OSError, ValueError, KeyError, TypeError):
            raise StdioRefused("installation_changed") from None

    @contextmanager
    def environment(self):
        """Called only after inherited/generated and native ancestry admission."""
        previous = dict(os.environ)
        environment = {"HOME": str(self.home), "PATH": str(self.install / "env/bin") + ":/usr/bin:/bin",
            "CLAUDLOBBY_INSTALLATION": str(self.install), "CLAUDLOBBY_ROOT": str(self.data),
            "LANG": "C.UTF-8", "TERM": "dumb", "TMPDIR": str(self.staging),
            "CLAUDLOBBY_HOST_SYSTEM_YAML": str(self.policy), "CLAUDE_CONFIG_DIR": str(self.account),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_RUNTIME_DIR": "/run/user/" + str(os.getuid()),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + str(os.getuid()) + "/bus",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1"}
        # No PLANE_EMIT_DISABLED here. Preparation silences incidental recording
        # (bootstrap._browser_environment); the guide is past preparation. The listener
        # it starts inherits this environment and runs first-team setup from it, and
        # first-fleet setup refuses under that harness exemption (#2611,
        # registry_emit.activation_scan_refusal). No guide step records by itself.
        if self.runtime is not None:
            from ..installation import managed_browser_environment
            managed = managed_browser_environment(self.installation)
            if managed is None:
                raise StdioRefused("installation_changed")
            # Canonical runtime owns PATH, tools, CA and Plane policy. Owner-only
            # selectors stay fixed, with no Linux runtime/bus assumptions.
            owner = {key: environment[key] for key in ("CLAUDLOBBY_INSTALLATION", "CLAUDLOBBY_ROOT",
                "TERM", "TMPDIR", "CLAUDLOBBY_HOST_SYSTEM_YAML", "CLAUDE_CONFIG_DIR", "PYTHONNOUSERSITE")}
            environment = {**managed, **owner}
        os.environ.clear()
        os.environ.update(environment)
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

    # The whole private override an SSH-controlled computer starts with: the
    # owner connection job is not enrolled, and every other host job keeps its
    # packaged configuration because the override names nothing else.
    SSH_HOST_POLICY = b"host:\n  jobs:\n    plane-owner:\n      enroll: false\n"

    def prepare_directories(self, consent, paths, *, ssh_policy=False):
        """Display every create-only fixed-directory effect before any write.

        ``ssh_policy`` is the fresh SSH-controlled setup only. An existing
        private policy is never read for repair, replaced or rewritten here.
        """
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
        make_policy = ssh_policy and not os.path.lexists(self.policy)
        if ssh_policy and not make_policy:
            _private_file(self.policy)  # Metadata only; existing policy stays as it is.
        if missing or make_env or make_policy:
            scope = {"directories": [str(p) for p in missing],
                "empty_private_environment": str(env) if make_env else None,
                "provider_directory": str(self.provider.parent),
                "effects": ["create only these fixed owned directories and empty private environment; no downloads or root setup"]}
            if ssh_policy:
                scope["host_policy"] = None if not make_policy else {
                    "path": str(self.policy), "sha256": hashlib.sha256(self.SSH_HOST_POLICY).hexdigest(),
                    "job": "plane-owner", "enroll": False}
                if make_policy:
                    scope["effects"].append(
                        "this computer is controlled over your SSH connection, so no always-on owner "
                        "connection service is set up; create one private settings file recording that; "
                        "ordinary team jobs are unchanged and no existing settings are replaced")
            consent.approve("prepare_directories", scope)
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
            if make_policy:
                _directory(self.policy.parent)
                # Create-only: an unexpected node appearing since approval refuses.
                try:
                    fd = os.open(self.policy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise StdioRefused("prerequisite_changed") from None
                try:
                    os.write(fd, self.SSH_HOST_POLICY)
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def origin(self):
        """Read the official native daemon's own HTTPS identity; never a form URL."""
        if self.runtime is not None:
            # No fixed Mac daemon/Unix Serve principal profile is admitted yet.
            raise StdioRefused("native_ingress_prerequisite_missing")
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
