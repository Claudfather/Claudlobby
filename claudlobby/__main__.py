"""claudlobby CLI entry point."""

from __future__ import annotations

import logging
import sys

from . import __version__
from .commands._parsers import register_subparsers
from .command_result import ResultArgumentParser

log = logging.getLogger("claudlobby")


def main(argv: list[str] | None = None) -> int:
    """Run one command, and exit with the status it answers (#2935).

    Every way in comes here: the installation launcher, a native unit and
    `python -m claudlobby`. So this is where a stderr that could not take a
    line is settled, for every command (`_close_unwritable_stderr`).
    """
    try:
        return _run(argv)
    finally:
        _close_unwritable_stderr()


def _close_unwritable_stderr() -> None:
    """Close the interpreter's stderr when it cannot take what it holds (#2935).

    A line whose write raised (a pipe whose reader has exited, a full device)
    stays in the stream's buffer: the guards that drop such a line (#2934, #2936,
    #2937) leave it there. As the interpreter ends it flushes stderr once more,
    that flush fails too, and the exit status becomes 120, though the answer on
    stdout reads ok. The interpreter passes over a closed stream at that flush,
    so this closes the stream, and the status is the command's. The close raises
    the same failure but closes the stream all the same. Only the interpreter's
    own stderr: never a stream put in its place (a test's capture), and nothing
    when it is None (fd 2 was closed when Python started, so nothing is written).
    """
    stream = sys.stderr
    if stream is None or stream is not sys.__stderr__:
        return
    try:
        stream.flush()
    except (OSError, ValueError):  # A ValueError: already closed, so passed over at exit.
        try:
            stream.close()
        except (OSError, ValueError):
            pass  # The stream is closed, though its last flush failed.


def _run(argv: list[str] | None) -> int:
    parser = ResultArgumentParser(
        prog="claudlobby",
        description="Compositor for Claude Code agent fleets.",
    )
    parser.add_argument(
        "--root", help="Mutable host data root (or CLAUDLOBBY_ROOT); package resources come from this CLI's installation"
    )
    parser.add_argument(
        "--fleet",
        help="Fleet overlay name (uses local/<fleet>/ for fleet.yaml, library overlay, runtime/). "
        "Public fleet operations default to generated session context when available; "
        "host-wide preparation does not inherit that scope. "
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
