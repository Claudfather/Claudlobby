"""Private committed Git runtime and canonical SQLite identities for CLI tests.

Only executable-origin observation is substituted: pytest runs outside the
private installation environment. Configuration, publication and admission use
production owners. This fixture never invokes native service actions.
"""
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from claudlobby import active_config, context, runtime_admission as admission
from claudlobby.activation_identity import read_registry_identity_bindings
from claudlobby.config_install import prepare_publication, apply_publication
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import unit_family
from claudlobby.git_update import UpdateStore
from claudlobby.installation import install_launcher
from claudlobby.plane.db import db_path
from claudlobby.plane.identity import resolve
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.migrations import migrate
from claudlobby.resources import get_resources
from tests.test_package_resources import _source_installation, _run
from tests.test_task_audit import _insert


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("cur")


@pytest.fixture
def current_runtime(tmp_path, monkeypatch):
    installation = _source_installation(tmp_path)
    root = installation.data_root
    repo = Path(__file__).resolve().parents[2]
    for name in ("library", "templates", "claudlobby/_runtime_scripts"):
        shutil.copytree(repo / name, installation.source / name, dirs_exist_ok=True)
    for name in ("claudlobby/system.yaml", "fleet.yaml.seed", "projects.yaml.seed", ".env.seed.example"):
        shutil.copy2(repo / name, installation.source / name)
    (installation.source / "requirements-runtime.lock").write_text("example==1.0 --hash=sha256:" + "a" * 64 + "\n")
    _run(["git", "-C", installation.source, "add", "."], tmp_path)
    _run(["git", "-C", installation.source, "-c", "user.name=Example", "-c",
          "user.email=example@example.invalid", "commit", "--quiet", "-m", "Runtime assets"], tmp_path)
    revision = installation.preflight()
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    package = get_resources(installation=installation)
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    for name in ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(context, "get_resources", lambda: package)
    monkeypatch.setattr(admission.RuntimeIdentity, "current", classmethod(lambda cls:
        cls(installation.cli, installation.native, "", installation, revision)))
    manifest = root / "fleet.yaml"
    manifest.write_text("fleet:\n  name: example\n  manager: manager\n  service_prefix: com.example\n"
                        "  bots:\n    worker: {expertise: [testing]}\n    manager: {expertise: [orchestration]}\n")
    builder = ConfigPlanBuilder(root, fleets=("example",),
        effects={"fleet_manifests": {"example": str(manifest)}, "units": []})
    projects = root / "projects.yaml"
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)}}}
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
           "CLAUDLOBBY_INSTALLATION": str(installation.directory),
           "CLAUDLOBBY_CLI": str(installation.cli),
           "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
           "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
    tmux_dir = tmp_path / "tmux"
    for bot in ("worker", "manager"):
        working = root / "runtime/bots" / bot
        stem = "com.example." + bot
        unit_env = {**env, "FLEET_NAME": "example", "BOT_ID": bot,
                    "BOT_SERVICE": stem, "TMUX_TMPDIR": str(tmux_dir)}
        argv = admission.wrap_unit_argv(unit_env, unit=stem, phase="bots", mode="exec",
            argv=[str(installation.native / "start-bot.sh"), str(working)])
        files = {stem + ".plist": (plistlib.dumps({"Label": stem,
            "WorkingDirectory": str(working), "EnvironmentVariables": unit_env,
            "ProgramArguments": list(argv)}), 0o644),
            stem + ".service": ((f"[Service]\nWorkingDirectory={working}\n" +
                "".join(admission.unit_systemd_environment(k, v) + "\n" for k,v in unit_env.items()) +
                f"ExecStart={admission.unit_systemd_command(argv)}\n[Install]\nWantedBy=default.target\n").encode(), 0o644)}
        builder.effects["units"].extend(unit_family(files, destination=working,
            scope="bot", phase="bots", fleet="example", bot=bot))
        builder.directory(working)
        for name, (content, mode) in files.items():
            builder.file(working / name, content, mode=mode)
        import shlex
        builder.file(working / "bot.conf", "".join(f"export {k}={shlex.quote(v)}\n" for k,v in unit_env.items()).encode())
        builder.directory(working / ".claude")
        builder.file(working / ".claude/settings.local.json", b'{"permissions":{"deny":["Write"]}}\n')
        for name in ("data", "memory", "logs", "projects"):
            builder.directory(working / name)
    rendered = builder.freeze(revision)
    with sqlite3.connect(db_path(root)) as conn:
        migrate(conn)
        conn.row_factory = sqlite3.Row
        host_uid = ensure_host_uid(root / "state")
        def identity(kind, alias, parent):
            uid = resolve(conn, kind, alias, now="2026-09-28T00:00:00Z", parent_uid=parent)
            conn.execute("UPDATE identity_registry SET provisional=0 WHERE uid=?", (uid,))
            return uid
        fleet_uid = identity("fleet", "example", host_uid)
        def snapshot(kind, alias, uid):
            _insert(conn, "registry_snapshots", entity_type=kind, entity_alias=alias,
                    entity_uid=uid, fleet_uid=fleet_uid, payload=json.dumps({"alias": alias}),
                    payload_hash="fixture", cause="generate", scan_id="private-scan")
            conn.execute("UPDATE registry_snapshots SET host_uid=? WHERE ingest_seq="
                         "(SELECT MAX(ingest_seq) FROM registry_snapshots)", (host_uid,))
        snapshot("fleet", "example", fleet_uid)
        for bot in ("manager", "worker"):
            alias = "bot:example/" + bot
            identity("actor", alias, fleet_uid)
            snapshot("bot", alias, identity("bot_instance", alias, fleet_uid))
    bindings = read_registry_identity_bindings(rendered, package=package)
    directory = tmp_path / "native-user"
    directory.mkdir()
    snapshot = active_config.snapshot_from_plan(rendered, installation, revision, identities=bindings,
        supervision={"manager": "Linux", "domain": "", "directory": str(directory)})
    with UpdateStore(installation) as store:
        record = store.prepare(revision, config_fingerprint=rendered.fingerprint, scope={}, request_id=str(uuid4()))
        record.update(phase="publish", maintenance=True, git_complete=True)
        store.save(record)
        prepare_publication(rendered, store)
        apply_publication(rendered, store)
        active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
        record = store.load()
        record.update(phase="done", maintenance=False, config_committed=True)
        store.save(record)
    host = SimpleNamespace(installation=installation, package=package, rendered=rendered,
        snapshot=snapshot, directory=directory, tmux_dir=tmux_dir, states={}, calls=[])
    return root, installation, rendered, host
