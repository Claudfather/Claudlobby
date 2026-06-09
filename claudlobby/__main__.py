"""claudlobby CLI entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .commands._parsers import register_subparsers

log = logging.getLogger("claudlobby")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudlobby",
        description="Compositor for Claude Code agent fleets.",
    )
    parser.add_argument(
        "--root", help="Path to claudlobby repo root (auto-detected by default)"
    )
    parser.add_argument(
        "--fleet",
        help="Fleet overlay name (uses local/<fleet>/ for fleet.yaml, library overlay, voices overlay, runtime/). "
        "If omitted, runs in root mode (fleet.yaml at repo root).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Operate on the built-in seed fleet (fleet.yaml.seed). "
        "Mutually exclusive with --fleet.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"claudlobby {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    sub = parser.add_subparsers(dest="cmd", required=True)
    register_subparsers(sub)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
