"""Dependency-light canonical reference hints shared by queries and public results."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

#: The read doors (`task show`, `assignment show`, `message show|receipt|wait`,
#: `request show`, `checkin show`) also take a prefix of an ID: its tag and at
#: least this many of its hex digits, or a request UUID's first characters
#: (#2380). Hand-written handoffs and notes carry 8-digit prefixes.
PREFIX_MIN_HEX = 8
#: Candidates an ambiguous prefix lists; `total_matches` counts every match.
PREFIX_HINT_LIMIT = 20
#: Rows a door reads for one prefix before it refuses to choose among them.
#: Minted IDs are random, so 8 shared hex digits are rare (1 in 2**32 per ID).
PREFIX_SCAN_LIMIT = 64

_UUID_FORM = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

IdShape = Literal["canonical", "prefix", "short"]


@dataclass(frozen=True)
class ReferenceCandidate:
    task_id: str
    assignment_id: str | None
    # Supported argv relative to the caller's SAME resolved root/fleet context.
    command: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceHint:
    candidates: tuple[ReferenceCandidate, ...]
    total_matches: int
    next_command: tuple[str, ...] | None


@dataclass(frozen=True)
class PrefixCandidate:
    id: str
    # Supported argv relative to the caller's SAME resolved root/fleet context.
    command: tuple[str, ...]


@dataclass(frozen=True)
class PrefixHint:
    """What a search by prefix found among the records the caller may read.

    A door reports 0 matches only after this search, never for a reference it
    could not search by prefix (#2380).
    """
    prefix: str
    candidates: tuple[PrefixCandidate, ...]
    total_matches: int
    next_command: tuple[str, ...] | None


def id_shape(reference: object, tag: str) -> IdShape | None:
    """How `reference` reads as an ID minted with `tag` (`msg_`, `ck_`, ...).

    `canonical` is the full 32-digit ID, `prefix` the tag and 8 to 31 of its
    lowercase hex digits, `short` fewer digits than a search accepts, and None
    anything else.
    """
    if not isinstance(reference, str) or not reference.startswith(tag):
        return None
    digits = reference[len(tag):]
    if not re.fullmatch(r"[0-9a-f]{0,32}", digits):
        return None
    if len(digits) == 32:
        return "canonical"
    return "prefix" if len(digits) >= PREFIX_MIN_HEX else "short"


def uuid_shape(reference: object) -> IdShape | None:
    """`id_shape` for a lowercase canonical UUID (a request ID): a prefix is its
    first 8 to 35 characters, dashes in place."""
    if not isinstance(reference, str) or not 0 < len(reference) <= len(_UUID_FORM):
        return None
    for char, form in zip(reference, _UUID_FORM):
        if (char == "-") != (form == "-") or (char != "-" and char not in "0123456789abcdef"):
            return None
    if len(reference) == len(_UUID_FORM):
        return "canonical"
    return "prefix" if len(reference) >= PREFIX_MIN_HEX else "short"


def note_prefix(data: dict, lines: tuple[str, ...], reference: str,
                resolved_id: str) -> tuple[str, ...]:
    """Say in a read's result that it resolved `reference` as a prefix.

    `data` gains `resolved_from_prefix` and the text output a last line; an
    exact read is left as it was. Returns the lines to print.
    """
    if reference == resolved_id:
        return lines
    data["resolved_from_prefix"] = {"prefix": reference, "id": resolved_id}
    return (*lines, f"resolved from prefix {reference}: {resolved_id}")
