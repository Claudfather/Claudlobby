"""Development-only composition of an explicitly named disposable data root.

The public CLI owns staged installation; these existing measurements need the
same compositor without selecting or activating a release. The implementation
remains commands.core.cmd_generate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys


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
    args = parser.parse_args(argv)
    if not args.root.is_absolute():
        parser.error("--root must be an explicit absolute data root")
    tree = Path(__file__).resolve().parent.parent
    root = args.root.resolve()
    if root == tree and (tree / ".git").exists():
        parser.error("refusing to compose into the source checkout")
    if (root / "state/selected-release.json").exists() or (root / "state/selected-release.json").is_symlink():
        parser.error("refusing a root with an active or unreadable release selection")
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
    args.seed = False
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        datefmt="%H:%M:%S")
    from claudlobby.commands.core import cmd_generate

    return cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
