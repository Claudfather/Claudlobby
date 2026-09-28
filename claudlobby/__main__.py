"""claudlobby CLI entry point."""

from __future__ import annotations

import logging
import sys

from . import __version__
from .commands._parsers import register_subparsers
from .command_result import ResultArgumentParser

log = logging.getLogger("claudlobby")


def main(argv: list[str] | None = None) -> int:
    parser = ResultArgumentParser(
        prog="claudlobby",
        description="Compositor for Claude Code agent fleets.",
    )
    parser.add_argument(
        "--root", help="Mutable host data root (or CLAUDLOBBY_ROOT); package resources come from this CLI's installation"
    )
    parser.add_argument(
        "--fleet",
        help="Fleet overlay name (uses local/<fleet>/ for fleet.yaml, library overlay, voices overlay, runtime/). "
        "If omitted, runs in root mode (fleet.yaml at the data root). "
        "Naming the root manifest's own fleet.name (no overlay) also resolves to root mode.",
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
    parser.add_argument("--json", dest="global_json", action="store_true",
                        help="Schema-1 result for migrated public operations")

    sub = parser.add_subparsers(dest="cmd", required=True)
    register_subparsers(sub)

    args = parser.parse_args(argv)
    if args.global_json:
        if not hasattr(args, "public_command"):
            parser.error("this operation has not adopted the common JSON result")
        args.json = True
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
