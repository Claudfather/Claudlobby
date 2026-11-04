"""Development-only composition of an explicitly named disposable data root.

The public CLI owns staged installation; these existing measurements need the
same compositor without selecting or activating a release. The implementation
remains commands.core.cmd_generate.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import venv


def private_installation(root, tree, *, native=None):
    """Prepare executable private Git paths; never enroll a native service."""
    from claudlobby.installation import Installation, write_installation, install_launcher
    installation = Installation(root.parent / (".harness-installation-" + root.name), root)
    if not installation.source.exists():
        installation.source.mkdir(parents=True)
        for name in ("claudlobby", "library", "templates"):
            shutil.copytree(tree / name, installation.source / name,
                            ignore=shutil.ignore_patterns("__pycache__", "_resources", "_artifact.json"))
        for name in ("fleet.yaml.seed", "projects.yaml.seed", ".env.seed.example", "requirements-runtime.lock"):
            shutil.copy2(tree / name, installation.source / name)
        subprocess.run(["git", "init", "--quiet", str(installation.source)], check=True)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(installation.environment)
        # Use the selected test interpreter's installed dependencies. Application
        # code still comes only from the private recorded Git checkout.
        sites = list(installation.environment.glob("lib/python*/site-packages"))
        (sites[0] / "harness-dependencies.pth").write_text("\n".join(
            path for path in sys.path if path.endswith(("site-packages", "dist-packages"))) + "\n")
        write_installation(installation)
        install_launcher(installation, interpreter=Path(sys.executable).resolve())
    if native is not None:
        for item in (tree / "claudlobby/_runtime_scripts").iterdir():
            if item.is_file():
                shutil.copy2(item, installation.native / item.name)
        for item in native.iterdir():
            if item.is_file():
                shutil.copy2(item, installation.native / item.name)
    subprocess.run(["git", "-C", str(installation.source), "add", "--all"], check=True)
    changed = subprocess.run(["git", "-C", str(installation.source), "diff", "--cached", "--quiet"])
    if changed.returncode:
        subprocess.run(["git", "-C", str(installation.source), "-c", "user.name=Example",
                        "-c", "user.email=example@example.invalid", "commit", "--quiet",
                        "-m", "Private harness source"], check=True)
    return installation


def publish_fixture(installation, fleet, *, bot=None):
    """Commit fixture manifests/identities through the same configuration owner.

    Like tests/fixtures/current_runtime.py, this prepares a data-only runtime:
    no generated service is enrolled and no service manager is invoked.
    """
    from claudlobby.resources import get_resources
    from claudlobby.config import load_fleet
    from claudlobby.config_plan import ConfigPlanBuilder
    from claudlobby.active_config import snapshot_from_plan, publish_current_config
    from claudlobby.plane.db import db_path
    from claudlobby.plane.migrations import migrate
    from claudlobby.plane.ids import ensure_host_uid
    from claudlobby.plane.identity import resolve
    import yaml
    package = get_resources(installation=installation)
    root = installation.data_root
    manifests = sorted((root / "local").rglob("fleet.yaml"))
    if (root / "fleet.yaml").is_file():
        manifests.append(root / "fleet.yaml")
    selected = root / "local" / fleet / "fleet.yaml"
    if not any(path.parent.name == fleet for path in manifests):
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("# harness: scan runtime bots\nfleet: {name: " + fleet + ", bots: {}}\n")
        manifests.append(selected)
    configs = []
    for manifest in manifests:
        text = manifest.read_text()
        doc = yaml.safe_load(text)
        # Legacy behavioral scenes author bot.conf directly. Declare that same
        # private roster before asking the public command to operate on it.
        if text.startswith("# harness: scan runtime bots") or not doc["fleet"].get("bots"):
            directory = manifest.parent / "runtime/bots"
            bots = sorted(path.name for path in directory.iterdir() if path.is_dir()) if directory.exists() else []
            if not bots:
                continue
            manifest.write_text("# harness: scan runtime bots\n" + yaml.safe_dump({"fleet": {
                "name": doc["fleet"].get("name", manifest.parent.name), "manager": bots[0],
                "service_prefix": "tmux", "bots": {name: {"expertise": ["testing"]} for name in bots}}}))
        configs.append((manifest, load_fleet(manifest)[0]))
    builder = ConfigPlanBuilder(root, fleets=tuple(config.name for _, config in configs),
        effects={"fleet_manifests": {}, "fleet_sources": {}, "units": []})
    previous = root / "state/config.json"
    prior = json.loads(previous.read_bytes()) if previous.exists() else None
    identities = prior["identities"] if prior else {"schema": 1, "host_uid": ensure_host_uid(root / "state"), "fleets": {}}
    needs_identities = any(config.name not in identities["fleets"] or
                           set(identities["fleets"][config.name]["bots"]) != set(config.bots)
                           or identities["fleets"][config.name]["manager"] != config.manager
                           for _, config in configs)
    for manifest, config in configs:
        name = config.name
        builder.effects["fleet_manifests"][name] = str(manifest)
        builder.effects["fleet_sources"][name] = {
            "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
            "projects": {"path": str(manifest.parent / "projects.yaml"),
                         "sha256": builder.input_content(manifest.parent / "projects.yaml")}}
        if bot is not None and config.name == fleet:
            from claudlobby.paths import Paths
            from claudlobby.composer import compose_launchd_plist, compose_systemd_unit
            from claudlobby.config_units import unit_family
            paths = Paths(root, package=package, fleet_dir=manifest.parent)
            stem = config.service_prefix + "." + bot
            files = {stem + ".plist": (compose_launchd_plist(config.bots[bot], config, paths).encode(), 0o644),
                     stem + ".service": (compose_systemd_unit(config.bots[bot], config, paths).encode(), 0o644)}
            working = paths.bot_runtime(bot)
            builder.effects["units"].extend(unit_family(files, destination=working,
                scope="bot", phase="bots", fleet=fleet, bot=bot))
            for name, (content, mode) in files.items():
                builder.file(working / name, content, mode=mode)
    if needs_identities:
        with sqlite3.connect(db_path(root)) as conn:
            migrate(conn)
            conn.row_factory = sqlite3.Row
            def identity(kind, alias, parent):
                uid = resolve(conn, kind, alias, now="2026-09-28T00:00:00Z", parent_uid=parent)
                conn.execute("UPDATE identity_registry SET provisional=0 WHERE uid=?", (uid,))
                return uid
            for manifest, config in configs:
                name = config.name
                uid = identity("fleet", name, identities["host_uid"])
                actors = {bot: identity("actor", "bot:" + name + "/" + bot, uid) for bot in config.bots}
                identities["fleets"][name] = {"fleet_uid": uid, "manager": config.manager,
                                             "manager_uid": actors[config.manager], "bots": actors}
    identities["fleets"] = {config.name: identities["fleets"][config.name] for _, config in configs}
    revision = package.source_revision
    rendered = builder.freeze(revision)
    expected = prior["fingerprint"] if prior else None
    snapshot = snapshot_from_plan(rendered, installation, revision, identities=identities)
    publish_current_config(snapshot, installation=installation, expected_previous=expected)
    # A fixture publication is complete; no updater owns a pending render here.
    shutil.rmtree(rendered.directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fleet")
    parser.add_argument("--bot")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--record-plane", action="store_true",
                        help="allow the boot sampler's private Plane scan")
    parser.add_argument("--installed", action="store_true",
                        help="use the private installed candidate in this data root")
    parser.add_argument("--pulse", action="store_true", help="run the public pulse against a private committed fixture")
    parser.add_argument("--prepare-runtime", action="store_true", help="prepare the private committed fixture without running it")
    parser.add_argument("--native-dir", type=Path, help="private recording/matcher collaborators for pulse")
    args = parser.parse_args(argv)
    if (args.pulse or args.prepare_runtime) and not args.fleet:
        parser.error("private runtime fixtures require --fleet")
    if not args.root.is_absolute():
        parser.error("--root must be an explicit absolute data root")
    tree = Path(__file__).resolve().parent.parent
    root = args.root.resolve()
    if root == tree and (tree / ".git").exists():
        parser.error("refusing to compose into the source checkout")
    if (root / "state/selected-release.json").exists() or (root / "state/selected-release.json").is_symlink():
        parser.error("refusing a root with an active or unreadable release selection")
    marker = root / ".claudlobby-harness-root"
    if (marker.is_symlink() or not marker.is_file()
            or marker.read_text().strip() != str(root)):
        parser.error("root is not a marked disposable harness directory")
    if args.record_plane:
        os.environ.pop("PLANE_EMIT_DISABLED", None)
    else:
        os.environ["PLANE_EMIT_DISABLED"] = "1"
    import claudlobby
    from claudlobby.resources import get_resources

    origin = Path(claudlobby.__file__).resolve().parent
    try:
        resources = get_resources()
    except RuntimeError as exc:
        parser.error(f"{exc} Build and select a private wheel interpreter for this harness")
    if args.installed:
        if not origin.is_relative_to(root / ".probe-release"):
            parser.error("private installed compositor does not belong to this probe root")
    elif (tree / "claudlobby/_artifact.json").is_file():
        # Prepared exports retain a content-bound artifact without a Git
        # revision. Compare that existing identity, not the export's new HEAD.
        captured = json.loads((tree / "claudlobby/_artifact.json").read_text())
        if resources.artifact_id != captured["artifact_id"]:
            parser.error("selected wheel differs from this prepared artifact")
    elif (tree / ".git").exists():
        revision = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                                  capture_output=True, text=True)
        clean = subprocess.run(["git", "-C", str(tree), "diff", "--quiet", "HEAD"],
                               capture_output=True)
        if revision.returncode or clean.returncode:
            parser.error("use a prepared disposable export or a clean committed harness tree")
        if resources.source_revision != revision.stdout.strip():
            parser.error("selected wheel was not built from this harness tree revision")
    else:
        parser.error("harness source has no built artifact or Git revision identity")
    installation = private_installation(root, tree, native=args.native_dir)
    os.environ["CLAUDLOBBY_INSTALLATION"] = str(installation.directory)
    for key in ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"):
        os.environ.pop(key, None)
    if args.pulse or args.prepare_runtime:
        publish_fixture(installation, args.fleet, bot=args.bot)
        if args.prepare_runtime:
            print(installation.cli)
            return 0
        return subprocess.run([str(installation.cli), "--root", str(root), "--fleet", args.fleet,
                               "fleet", "pulse"]).returncode
    atexit.register(shutil.rmtree, installation.directory)
    args.seed = False
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        datefmt="%H:%M:%S")
    from claudlobby.commands.core import cmd_generate

    return cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
