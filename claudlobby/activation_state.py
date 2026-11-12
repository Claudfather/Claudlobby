"""Read-only historical activation evidence and shared lifecycle errors.

Git updates own current runtime state. Retained sealed journals are inspected
without creating, repairing, selecting or resuming an old activation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re


class ActivationError(RuntimeError):
    """Another activation or an unproven recovery state; keep the host paused."""


class ActivationRefusal(ActivationError):
    """A product-authored refusal whose text is safe to show the operator.

    Messages name only fixed conditions and product-owned paths; never raise
    this with native stderr, authored config values or another exception's text.
    """


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _state(root: Path) -> Path:
    path = Path(root).expanduser().resolve() / "state"
    if path.resolve() != path:
        raise ActivationError("activation state directory is redirected")
    return path


def _record_path(root: Path, activation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", activation_id):
        raise ActivationError("supply an explicit simple activation id")
    path = _state(root) / "activations" / activation_id / "activation.json"
    if path.resolve() != path:
        raise ActivationError("activation record is redirected")
    return path


@dataclass(frozen=True)
class ActivationRecord:
    activation_id: str
    root: Path
    body: dict

    @property
    def status(self) -> str:
        return self.body["status"]


def read_activation(root: Path, activation_id: str) -> ActivationRecord:
    path = _record_path(root, activation_id)
    try:
        raw = json.loads(path.read_bytes())
        body = raw["record"]
        if (raw["sha256"] != _digest(body) or body["schema"] != 1
                or body["activation_id"] != activation_id
                or body["root"] != str(_state(root).parent)):
            raise ActivationError("activation record identity changed")
        return ActivationRecord(activation_id, _state(root).parent, body)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ActivationError(f"cannot read activation {activation_id}: {exc}") from exc


def read_selection(root: Path) -> dict | None:
    path = _state(root) / "selected-release.json"
    if path.is_symlink():
        raise ActivationError("release selection is redirected")
    try:
        result = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ActivationError(f"cannot read release selection: {exc}") from exc
    if (not isinstance(result, dict)
            or set(result) != {"schema", "activation_id", "release_id", "plan_id"}
            or result["schema"] != 1):
        raise ActivationError("invalid release selection")
    return result
