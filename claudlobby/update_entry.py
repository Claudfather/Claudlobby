"""Private short-lived application stages of the independent Git updater.

No ordinary CLI dispatch or environment flag grants this route. The calling
coordinator supplies its inherited locked descriptors and original request.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .git_update import (UpdateError, UpdateStore, _head, _verify_inherited_lock, _candidate_revision,
                         _initialization_scope, _check_initialization_intent, _plain)
from .installation import read_installation


def _retire_completed_render(store):
    """Persist exact cleanup intent before removing completed owned render files."""
    from .config_plan import read_rendered
    import hashlib
    import re
    import stat

    store._assert_locked()
    record = store.load()
    if record is None or record["phase"] != "done" or record.get("health_errors"):
        raise UpdateError("pending update render must be retained")
    directory = store.work / "render"
    if directory.is_symlink() or directory.resolve() != directory:
        raise UpdateError("completed render directory is redirected")
    intent = record.get("render_retirement")
    if intent is None:
        if not directory.exists():
            return
        rendered = read_rendered(store.root, record["config_fingerprint"])
        if rendered.revision != record["target"]:
            raise UpdateError("completed render belongs to another target")
        if {path.name for path in directory.iterdir()} != {"render.json", "files"}:
            raise UpdateError("completed render includes unknown scratch; retained")
        files = directory / "files"
        if files.is_symlink() or not files.is_dir() or files.resolve() != files:
            raise UpdateError("completed render content directory is redirected")
        referenced = set()
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "sha256" and isinstance(item, str):
                        referenced.add(item)
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(rendered.payload())
        paths = [directory / "render.json"]
        for path in files.iterdir():
            if path.name not in referenced or path.is_symlink() or not path.is_file():
                raise UpdateError("completed render includes unknown content; retained")
            rendered.blob(path.name)
            paths.append(path)
        intent = {"fingerprint": record["config_fingerprint"],
                  "directories": {"": stat.S_IMODE(directory.stat().st_mode),
                                  "files": stat.S_IMODE(files.stat().st_mode)},
                  "files": {str(path.relative_to(directory)): {
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "mode": stat.S_IMODE(path.stat().st_mode)} for path in paths}}
        record["render_retirement"] = intent
        store.save(record)
    # This narrowly recorded set survives deletion of the original manifest or
    # any blob. Missing owned paths are complete; changed or foreign paths refuse.
    if (not isinstance(intent, dict) or set(intent) != {"fingerprint", "directories", "files"}
            or intent["fingerprint"] != record["config_fingerprint"]
            or not isinstance(intent["directories"], dict) or set(intent["directories"]) != {"", "files"}
            or not isinstance(intent["files"], dict) or "render.json" not in intent["files"]):
        raise UpdateError("completed render cleanup intent changed")
    for name, mode in intent["directories"].items():
        if type(mode) is not int or mode < 0 or mode > 0o777:
            raise UpdateError("completed render cleanup directory mode is invalid")
        path = directory / name
        if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()
                or path.resolve() != path or stat.S_IMODE(path.stat().st_mode) != mode):
            raise UpdateError("completed render cleanup directory changed")
    for name, saved in intent["files"].items():
        if (name != "render.json" and re.fullmatch(r"files/[0-9a-f]{64}", name) is None
                or not isinstance(saved, dict) or set(saved) != {"sha256", "mode"}
                or not isinstance(saved["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", saved["sha256"]) is None
                or type(saved["mode"]) is not int or not 0 <= saved["mode"] <= 0o777):
            raise UpdateError("completed render cleanup file intent changed")
        path = directory / name
        if os.path.lexists(path) and (path.is_symlink() or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != saved["mode"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != saved["sha256"]):
            raise UpdateError("completed render cleanup file changed")
    if not directory.exists():
        return
    if any(path.name not in {"render.json", "files"} for path in directory.iterdir()):
        raise UpdateError("completed render includes unknown scratch; retained")
    files = directory / "files"
    if files.exists() and any("files/" + path.name not in intent["files"] for path in files.iterdir()):
        raise UpdateError("completed render includes unknown content; retained")
    for name in sorted(intent["files"]):
        (directory / name).unlink(missing_ok=True)
    if files.exists():
        files.rmdir()
    directory.rmdir()


def _authorize_initialization(store, scope, package):
    """The canonical cold operator gate and observed native placement, before effects."""
    import platform
    from .commands.operator_context import require_operator_context
    from .supervision_inventory import Adapter, _catalog
    from .update_operation import _fresh_root

    require_operator_context()  # Existing cold-setup gate; no committed runtime exists yet.
    _fresh_root(store)
    scope = _initialization_scope(scope)
    for manifest in scope["manifests"]:
        path = _plain(Path(manifest))
        if not path.is_file():
            raise UpdateError("initialization manifest is not an existing authored file")
    directory = _plain(Path(scope["directory"]))
    manager, _, directories, _, _ = _catalog(Adapter(package).read("svc_inventory_catalog"))
    if manager != platform.system() or directory not in directories:
        raise UpdateError("initial native directory is not observed in this host manager catalog")
    return {"directory": str(directory), "revision": package.source_revision}


def _render_paths(store, record, package):
    """Select original manifest paths with candidate resources, without a second composer."""
    from .active_config import read_current_config
    from .paths import Paths
    scope = record.get("initialization_scope")
    if scope is not None:
        manifests = _initialization_scope(scope)["manifests"]
    else:
        current = read_current_config(store.root, installation=store.installation)
        manifests = [item["manifest"] for item in current["fleets"].values()]
    paths = [Paths(store.root, package=package, fleet_dir=(None if Path(manifest).parent == store.root
              else Path(manifest).parent)) for manifest in manifests]
    if any(str(paths_item.fleet_yaml) != manifest for paths_item, manifest in zip(paths, manifests)):
        raise UpdateError("rendered manifest differs from its original authored scope")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private updater application stage")
    parser.add_argument("installation")
    parser.add_argument("stage", choices=["authorize", "retire-render", "render", "prepare", "quiesce", "quiet", "publish", "restore"])
    parser.add_argument("lock_fd", type=int)
    parser.add_argument("parent_pid", type=int)
    parser.add_argument("request_id")
    parser.add_argument("runtime_fd", type=int, nargs="?")
    parser.add_argument("--initialization-scope")
    args = parser.parse_args(argv)
    if args.initialization_scope is not None and args.stage != "authorize":
        raise UpdateError("initialization consent is accepted only at operator authorization")
    proposed = (_initialization_scope(json.loads(args.initialization_scope))
                if args.initialization_scope is not None else None)
    installation = read_installation(Path(args.installation))
    if args.stage == "authorize":
        if os.getppid() != args.parent_pid:
            raise UpdateError("operator stage does not belong to updater parent")
        store = UpdateStore(installation)
        _verify_inherited_lock(store.state / "update.lock", args.lock_fd)
        store._fd = args.lock_fd
    else:
        store = UpdateStore.from_parent(installation, fd=args.lock_fd, parent_pid=args.parent_pid,
                                        request_id=args.request_id)
    try:
        record = store.load()
        source = Path(__file__).absolute().parent.parent
        if args.stage == "render":
            if (record["phase"] != "prepare" or record.get("finalized", True)
                    or source != Path(record["candidate_source"])):
                raise UpdateError("candidate rendering differs from prepared source")
            _candidate_revision(store, source)
            expected_python = Path(record["candidate_python"])
        else:
            expected = (_head(installation) if args.stage == "authorize" else
                        record["before"] if args.stage in {"prepare", "quiesce", "quiet"} else record["target"])
            if source != installation.source or _head(installation) != expected:
                raise UpdateError("application stage source differs from required Git revision")
            expected_python = installation.environment / "bin/python"
        if Path(sys.executable).absolute() != expected_python:
            raise UpdateError("application stage interpreter differs from prepared environment")
        if args.stage in {"quiet", "publish"}:
            if args.runtime_fd is None:
                raise UpdateError("application stage requires inherited runtime EX")
            _verify_inherited_lock(store.state / "runtime-use.lock", args.runtime_fd)
        from .resources import get_resources
        if args.stage == "authorize":
            from .commands.operator_context import require_operator_context
            from .active_config import read_current_config
            package = get_resources(installation=installation)
            if record is not None:
                _check_initialization_intent(record, proposed)
            pending = record is not None and (record["phase"] != "done" or record.get("health_errors"))
            scope = proposed if proposed is not None else (record.get("initialization_scope") if pending else None)
            if scope is not None:
                print(json.dumps(_authorize_initialization(store, scope, package)))
                return 0
            require_operator_context(store.root)
            snapshot = read_current_config(store.root, installation=installation, revision=package.source_revision)
            placement = snapshot.get("supervision")
            if placement is None:
                raise UpdateError("current configuration has no committed native placement")
            print(json.dumps({"directory": placement["directory"], "revision": snapshot["revision"]}))
            return 0
        if args.stage == "retire-render":
            _retire_completed_render(store)
            return 0
        from .supervision_inventory import Adapter
        from . import update_operation
        if args.stage == "render":
            from .config_staging import render_git_configuration
            package = get_resources(installation=installation, checkout=source)
            paths = _render_paths(store, record, package)
            rendered = render_git_configuration(paths, revision=record["target"])
            record["candidate_render"] = rendered.fingerprint
            store.save(record)
        elif args.stage == "prepare":
            from .config_plan import read_rendered
            rendered = read_rendered(store.root, record["candidate_render"])
            update_operation.prepare_operation(store, rendered, Adapter(get_resources(installation=installation)),
                Path(record["native_install_directory"]), Path(record["host_interpreter"]), record["request_id"],
                initialize=record.get("initialization_scope") is not None)
        elif args.stage == "quiesce":
            update_operation.quiesce_operation(store, Adapter(get_resources(installation=installation)))
        elif args.stage == "quiet":
            update_operation.assert_operation_quiet(store, Adapter(get_resources(installation=installation)))
        elif args.stage == "publish":
            update_operation.publish_operation(store)
        else:
            update_operation.restore_operation(store, Adapter(get_resources(installation=installation)))
        return 0
    finally:
        store.__exit__()
        if args.runtime_fd is not None:
            os.close(args.runtime_fd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (UpdateError, OSError, ValueError, RuntimeError) as exc:
        print(f"update application stage: {exc}", file=sys.stderr)
        raise SystemExit(6)
