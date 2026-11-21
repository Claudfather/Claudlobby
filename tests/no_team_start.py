"""A computer started with no team, through the updater's own stages.

Rendering, preparation, the reader lease's drain, the Git cutover, publication,
the database, the committed configuration and restore are the code a computer
runs. Only the native layer is a stand-in: units are written to a temporary
directory and a table answers for the user manager, and the operator gate, the
native catalog, the enrollment inventory and the updater's native job are
fakes. The application stages run in this process instead of in a fresh one,
with this checkout's resources bound to the fixture's revision.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import yaml

from claudlobby import (
    git_update as updater,
    update_entry,
    update_operation,
    update_services,
)
from claudlobby.active_config import read_current_config
from claudlobby.config_plan import read_rendered
from claudlobby.git_update import UpdateStore, read_update
from claudlobby.installation import install_launcher
from tests.package_fixtures import source_package
from tests.test_package_resources import _run, _source_installation


REPO = Path(__file__).resolve().parents[1]
LOCK = b"example==1.0 --hash=sha256:" + b"a" * 64 + b"\n"
OWNER_ON = (
    "host:\n  jobs:\n    plane-owner:\n      enroll: true\n"
    "      origin: https://host.example.test\n      tailscale: {tailscale}\n"
    "      tailscale_socket: null\n      socket: {socket}\n"
    "      workspace_issuer: https://accounts.example.test\n"
    "      workspace_origin: https://plane.example.test\n"
)
#: What the settings file says over SSH (owner_setup_consent.py).
OWNER_OFF = "host:\n  jobs:\n    plane-owner:\n      enroll: false\n"


def default_on_jobs() -> set[str]:
    """Every host job the shipped system.yaml switches on."""
    jobs = yaml.safe_load((REPO / "claudlobby/system.yaml").read_text())["host"]["jobs"]
    return {
        name
        for name, item in jobs.items()
        if (item or {}).get("enroll", True) is not False
    }


def job_name(target: str) -> str:
    """claudlobby-plane-daemon.service -> plane-daemon."""
    name = target.removeprefix("claudlobby-")
    for suffix in (".service", ".timer"):
        name = name.removesuffix(suffix)
    return name


def _snapshot(path):
    """An inventory's record of one file."""
    content = path.read_bytes()
    return {
        "path": str(path),
        "resolved": str(path.resolve()),
        "mode": path.stat().st_mode & 0o777,
        "link": None,
        "sha256": hashlib.sha256(content).hexdigest(),
        "content": {"base64": base64.b64encode(content).decode()},
    }


class Native:
    """A Linux user manager's unit table, keyed by unit name."""

    def __init__(self, directory, package):
        self.directory = directory
        self.package = package
        self.states = {}
        self.calls = []

    def read(self, function, *args):
        assert function == "svc_inventory_catalog", function
        return f"manager\tLinux\ndirectory\t{self.directory}\n"

    def state(self, installed, target):
        if target in self.states:
            return self.states[target]
        return (
            "enabled loaded inactive"
            if Path(installed).exists()
            else "not-found not-found inactive"
        )

    def call(self, function, *args, timeout=30):
        self.calls.append((function, args))
        output = ""
        if function == "svc_inventory_catalog":
            output = self.read(function)
        elif function == "svc_activation_start":
            self.states[args[1]] = "enabled loaded active"
        elif function == "svc_activation_pause":
            self.states[args[1]] = "masked-runtime masked inactive"
        elif function == "svc_activation_resume":
            self.states[args[1]] = args[2]
        elif function in {"svc_inventory_state", "svc_activation_snapshot"}:
            output = self.state(args[0], args[1])
        elif function == "svc_activation_quiet":
            output = (
                "inactive\tcgroup-empty"
                if self.state(args[0], args[1]).endswith(" inactive")
                else "unknown"
            )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def _commit(installation, message):
    _run(["git", "-C", installation.source, "add", "."], installation.data_root)
    _run(
        [
            "git",
            "-C",
            installation.source,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        installation.data_root,
    )
    return installation.preflight()


class Case:
    """One paired, fresh computer: a managed checkout at `before`, with `target` ahead of it."""

    def __init__(self, tmp_path, monkeypatch, *, owner=None):
        import platform
        from claudlobby import composer, env_tiers, supervision_inventory
        from claudlobby.commands import operator_context
        from claudlobby.plane.ids import ensure_host_uid
        from claudlobby.plane.owner_access import OwnerAccess

        self.monkeypatch = monkeypatch
        installation = _source_installation(tmp_path)
        source = installation.source
        shutil.copy2(
            REPO / "claudlobby/runtime_versions.py",
            source / "claudlobby/runtime_versions.py",
        )
        shutil.copy2(
            REPO / "claudlobby/supervision_runtime.py",
            source / "claudlobby/supervision_runtime.py",
        )
        (source / "claudlobby/_runtime_scripts/supervisor.sh").write_text(
            "# fixture native support\n"
        )
        (source / "requirements-runtime.lock").write_bytes(LOCK)
        self.before = _commit(installation, "Current source")
        (source / "templates/candidate.txt").write_text("target bytes\n")
        self.target = _commit(installation, "Target source")
        _run(
            ["git", "-C", source, "checkout", "--quiet", self.before],
            installation.data_root,
        )
        self.installation, self.root = installation, installation.data_root
        self.python = Path(sys.executable).resolve()
        install_launcher(installation, interpreter=self.python)
        self.directory = tmp_path / "native-units"
        self.directory.mkdir()
        self.scope = {"directory": str(self.directory), "manifests": []}
        # Paired: the owner preparation a guide leaves before the first activation.
        self.host_uid = ensure_host_uid(self.root / "state")
        self.access = OwnerAccess.initialize(self.root, before_first_fleet=True)
        settings = tmp_path / "private" / "host.yaml"
        settings.parent.mkdir(mode=0o700)
        if owner == "on":
            settings.write_text(
                OWNER_ON.format(
                    tailscale=self.python, socket=self.root / "state/owner/owner.sock"
                )
            )
        elif owner == "off":
            settings.write_text(OWNER_OFF)
        self.settings = settings
        monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(settings))
        self.native = Native(self.directory, self.package(self.before))
        self.trace, self.dispatches = [], []
        self.job_state = "inactive"
        # The rendering environment of tests/test_config_staging.py.
        monkeypatch.setattr(
            env_tiers,
            "read_tiers",
            lambda paths, bot_name=None, fleet_name=None: [
                env_tiers.EnvTier(name, path, "present" if path.is_file() else "absent")
                for name, path in (
                    ("host", Path.home() / ".env"),
                    ("root", paths.root / ".env"),
                )
            ],
        )
        monkeypatch.setattr(composer, "_host_cpu_count", lambda: 4)
        monkeypatch.setattr(composer, "_git", lambda *_: None)
        monkeypatch.setattr(composer._switches, "missing_extra", lambda *_: None)
        # The native layer.
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            operator_context, "require_operator_context", lambda root=None: None
        )
        monkeypatch.setattr(
            supervision_inventory, "Adapter", lambda _package: self.native
        )
        monkeypatch.setattr(update_services, "collect_enrollment", self._inventory)
        # A timer restored to running is held past its catch-up run; that has its
        # own tests in tests/test_update_timer_catch_up.py.
        monkeypatch.setattr(update_services, "_held_start", lambda *_args: None)
        monkeypatch.setattr(updater, "_application_stage", self.stage)
        monkeypatch.setattr(updater, "_independent_quiet", lambda store: None)
        monkeypatch.setattr(updater, "_prepare_job_support", self._job_support)
        monkeypatch.setattr(updater, "_retained_job", lambda store: self._jobs(store))
        monkeypatch.setattr(
            updater, "_authorize_retained_operator", lambda store, adapter: None
        )

    def package(self, revision):
        """This checkout's resources, bound to the fixture installation at `revision`."""
        return replace(
            source_package(),
            installation=self.installation,
            artifact_id="",
            source_revision=revision,
        )

    def add_target(self, message="Later source"):
        """Commit one more revision ahead of the checked-out one, for an ordinary update."""
        source = self.installation.source
        head = updater._head(self.installation)
        (source / "templates/candidate.txt").write_text(f"{message}\n")
        later = _commit(self.installation, message)
        _run(["git", "-C", source, "checkout", "--quiet", head], self.root)
        return later

    def _inventory(
        self, data_root, declarations, *, adapter=None, only_names=None, **_kwargs
    ):
        """The enrollment inventory of the native directory, as the unit table reads it."""
        rows = []
        for declaration in declarations:
            name = declaration.source.name
            if only_names is not None and name not in only_names:
                continue
            installed = self.directory / name
            state = self.native.state(installed, name)
            rows.append(
                {
                    "target": name,
                    "installed": [_snapshot(installed)] if installed.exists() else [],
                    "generated": _snapshot(declaration.source),
                    "declaration": {
                        "source": str(declaration.source),
                        "scope": declaration.scope,
                        "working_directory": str(declaration.working_directory),
                        "environment": [list(item) for item in declaration.environment],
                        "fleet": declaration.fleet,
                        "bot": declaration.bot,
                        "service": declaration.service,
                    },
                    "properties": [
                        [key, value]
                        for key, value in zip(
                            ("UnitFileState", "LoadState", "ActiveState"), state.split()
                        )
                    ],
                }
            )
        payload = {"units": rows, "observed_files": []}
        return SimpleNamespace(
            require_complete=lambda: SimpleNamespace(payload=lambda: deepcopy(payload))
        )

    def stage(
        self,
        store,
        stage,
        *,
        source=None,
        python=None,
        runtime_fd=None,
        initialization=None,
        convert_sealed=False,
    ):
        """One application stage, as update_entry.main runs it, in this process."""
        store._assert_locked()
        record = store.load()
        self.trace.append(stage)
        if stage == "authorize":
            pending = record is not None and (
                record["phase"] != "done" or record.get("health_errors")
            )
            scope = (
                initialization
                if initialization is not None
                else (record.get("initialization_scope") if pending else None)
            )
            revision = updater._head(self.installation)
            if scope is not None:
                authorized = update_entry._authorize_initialization(
                    store, scope, self.package(revision)
                )
            else:
                snapshot = read_current_config(
                    store.root, installation=self.installation, revision=revision
                )
                authorized = {
                    "directory": snapshot["supervision"]["directory"],
                    "revision": snapshot["revision"],
                }
            return {**authorized, "interpreter": str(self.python)}
        if stage == "retire-render":
            update_entry._retire_completed_render(store)
        elif stage == "render":
            update_entry._render_candidate(
                store, record, self.package(record["target"])
            )
        elif stage == "prepare":
            update_operation.prepare_operation(
                store,
                read_rendered(store.root, record["candidate_render"]),
                self.native,
                Path(record["native_install_directory"]),
                Path(record["host_interpreter"]),
                record["request_id"],
                initialize=record.get("initialization_scope") is not None,
            )
        elif stage == "quiesce":
            update_operation.quiesce_operation(store, self.native)
        elif stage == "quiet":
            update_operation.assert_operation_quiet(store, self.native)
        elif stage == "publish":
            update_operation.publish_operation(store)
        elif stage == "restore":
            update_operation.restore_operation(store, self.native)
        else:
            raise AssertionError(stage)
        return store.load()

    def _job(self, store):
        directory = Path(store.load()["native_install_directory"])
        return SimpleNamespace(
            manager="Linux",
            target="claudlobby-update-job.service",
            source=self.installation.bin / "claudlobby-update-job.service",
            installed=directory / "claudlobby-update-job.service",
            content=b"fixture job",
        )

    def _jobs(self, store):
        job = self._job(store)

        def run(*_args, **_kwargs):
            self.dispatches.append(read_update(self.installation)["request_id"])
            return {"dispatch": "started"}

        jobs = SimpleNamespace(
            _definition=lambda *_args, **_kwargs: job,
            prepare_update_job=lambda *_args: None,
            bind_update_job=lambda *_args: None,
            run_update_job=run,
            observe_update_job=lambda *_args, **_kwargs: {
                "target": job.target,
                "state": self.job_state,
            },
        )
        return jobs, self.installation, self.native

    def _job_support(self, store):
        updater._prepare_quiet_support(store)
        record = store.load()
        job = self._job(store)
        record["updater_job"] = {
            "manager": job.manager,
            "target": job.target,
            "source": str(job.source),
            "installed": str(job.installed),
            "sha256": updater._digest(job.content),
        }
        store.save(record)
        return self._jobs(store)

    def run_job(self, at=None):
        """The dispatched job: run the frozen operation to its end. `at[n]` runs at the n-th lock poll."""
        at = dict(at or {})
        real = time.sleep
        count = [0]

        def sleep(seconds):
            if seconds == updater._RUNTIME_POLL:
                count[0] += 1
                action = at.pop(count[0], None)
                if action is not None:
                    action()
            real(seconds)

        with self.monkeypatch.context() as patch:
            patch.setattr(updater, "_RUNTIME_POLL", 0.01)
            patch.setattr(updater, "_RUNTIME_STOP", 20)
            patch.setattr(time, "sleep", sleep)
            with UpdateStore(self.installation) as store:
                result = updater.run_prepared_update(store)
        assert not at, f"the updater never waited for polls {sorted(at)}"
        return result

    def start(self):
        """Dispatch the no-team first install and run its job to its end."""
        dispatched = updater.apply_or_continue(
            self.installation, target=self.target, initialization=self.scope
        )
        assert dispatched["job"] == {"dispatch": "started"}
        return self.run_job()

    def admit(self):
        """Run later commands in this process as the started computer's own runtime."""
        from claudlobby.paths import Paths
        from claudlobby.runtime_admission import RuntimeIdentity

        identity = RuntimeIdentity(
            self.installation.cli,
            self.installation.native,
            "",
            self.installation,
            self.target,
        )
        self.monkeypatch.setattr(
            RuntimeIdentity, "current", classmethod(lambda cls: identity)
        )
        self.monkeypatch.setenv(
            "CLAUDLOBBY_INSTALLATION", str(self.installation.directory)
        )
        self.monkeypatch.setenv("CLAUDLOBBY_ROOT", str(self.root))
        for key in (
            "CLAUDLOBBY_RELEASE_ID",
            "CLAUDLOBBY_ARTIFACT_ID",
            "CLAUDLOBBY_FLEET",
            "CLAUDLOBBY_UPDATE_SCHEDULED",
        ):
            self.monkeypatch.delenv(key, raising=False)
        return identity, Paths(self.root, package=self.package(self.target))

    def update(self, target):
        """Dispatch an ordinary update to `target` and run its job to its end."""
        dispatched = updater.apply_or_continue(self.installation, target=target)
        assert dispatched["job"] == {"dispatch": "started"}
        return self.run_job()
