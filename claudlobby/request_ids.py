"""The dependency-light `--request-id` check the mutation commands share: a canonical UUID,
the lowercase hyphenated form `str(UUID(value))` prints, passes unchanged; anything else is
refused as an invalid argument."""

from __future__ import annotations

from uuid import UUID

from .command_result import CommandFailure


def canonical_request_id(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")
