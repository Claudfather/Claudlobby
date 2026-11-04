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

from .git_update import UpdateError, UpdateStore, _head, _verify_inherited_lock, _candidate_revision
from .installation import read_installation


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private updater application stage")
    parser.add_argument("installation")
    parser.add_argument("stage", choices=["authorize", "render", "prepare", "quiesce", "quiet", "publish", "restore"])
    parser.add_argument("lock_fd", type=int)
    parser.add_argument("parent_pid", type=int)
    parser.add_argument("request_id")
    parser.add_argument("runtime_fd", type=int, nargs="?")
    args = parser.parse_args(argv)
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
            require_operator_context(store.root)
            package = get_resources(installation=installation)
            snapshot = read_current_config(store.root, installation=installation, revision=package.source_revision)
            placement = snapshot.get("supervision")
            if placement is None:
                raise UpdateError("current configuration has no committed native placement")
            print(json.dumps({"directory": placement["directory"], "revision": snapshot["revision"]}))
            return 0
        from .supervision_inventory import Adapter
        from . import update_operation
        if args.stage == "render":
            from .active_config import read_current_config
            from .paths import Paths
            from .config_staging import render_git_configuration
            package = get_resources(installation=installation, checkout=source)
            current = read_current_config(store.root, installation=installation)
            paths = [Paths(store.root, package=package, fleet_dir=(None if Path(item["manifest"]).parent == store.root
                      else Path(item["manifest"]).parent)) for item in current["fleets"].values()]
            rendered = render_git_configuration(paths, revision=record["target"])
            record["candidate_render"] = rendered.fingerprint
            store.save(record)
        elif args.stage == "prepare":
            from .config_plan import read_rendered
            rendered = read_rendered(store.root, record["candidate_render"])
            update_operation.prepare_operation(store, rendered, Adapter(get_resources(installation=installation)),
                Path(record["native_install_directory"]), Path(record["host_interpreter"]), record["request_id"])
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
