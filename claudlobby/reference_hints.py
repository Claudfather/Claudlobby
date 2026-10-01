"""Dependency-light canonical reference hints shared by queries and public results."""

from __future__ import annotations

from dataclasses import dataclass


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
