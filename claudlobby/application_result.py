"""Transport-neutral successful application data; presentation belongs to callers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ApplicationOutcome:
    data: dict
    release_id: str | None = None
