"""Stdlib-only OTLP intake declarations shared by parsing and runtime readers."""

import argparse

OTEL_INTAKE_PORT: int = 4319
DEFAULT_RETENTION_DAYS = 14


def _positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def retention_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--retention-days", type=_positive, default=DEFAULT_RETENTION_DAYS)
    return parser
