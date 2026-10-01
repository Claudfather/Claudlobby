"""Signed, read-served report prefixes; no report or acknowledgement storage."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile


_IDENTITY = frozenset({"host_uid", "fleet_uid", "viewer_uid", "release_id",
                       "activation_id", "plan_id"})
_KEY_FILE = "report-cursor.key"
_TOKEN_LIMIT = 4096
_MSG = re.compile(r"msg_[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({"v", "identity", "prior_seq", "prior_ack_seq", "through_seq",
                     "through_message_id", "through_ts", "count", "prefix_sha256"})


class CursorError(ValueError):
    """Invalid served prefix, or unavailable private signing key."""

    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True, slots=True)
class ServedPrefix:
    identity: dict[str, str]
    prior_seq: int | None
    prior_ack_seq: int | None
    through_seq: int
    through_message_id: str
    through_ts: str
    count: int
    prefix_sha256: str


def _identity(value: dict) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != _IDENTITY
            or any(not isinstance(part, str) or not part for part in value.values())):
        raise CursorError("report cursor identity is invalid")
    return {key: value[key] for key in sorted(_IDENTITY)}


def _key(root: Path, *, create: bool) -> bytes:
    path = Path(root) / "state" / _KEY_FILE
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    def read_existing() -> bytes:
        try:
            fd = os.open(path, flags)
            try:
                found = os.fstat(fd)
                if (not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid()
                        or stat.S_IMODE(found.st_mode) != 0o600 or found.st_nlink != 1):
                    raise CursorError("report cursor key is not an owned private regular file",
                                      unavailable=True)
                value = os.read(fd, 33)
                if len(value) != 32:
                    raise CursorError("report cursor key has an invalid length", unavailable=True)
                return value
            finally:
                os.close(fd)
        except OSError as exc:
            raise CursorError("report cursor key is unavailable", unavailable=True) from exc

    try:
        return read_existing()
    except CursorError as exc:
        if not create or not isinstance(exc.__cause__, FileNotFoundError):
            raise

    state = path.parent
    if not state.is_dir():
        raise CursorError("report cursor state directory is unavailable", unavailable=True)
    candidate = os.urandom(32)
    try:
        fd, temporary = tempfile.mkstemp(prefix=".report-cursor-", dir=state)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Another eligible read published its complete key first.
        finally:
            os.unlink(temporary)
        directory = os.open(state, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CursorError("report cursor key cannot be created", unavailable=True) from exc
    return read_existing()


def _ordered(rows: list[dict]) -> list[dict]:
    try:
        ordered = sorted(rows, key=lambda row: (row["_seq"], row["plane_msg_id"]))
        keys = [(row["_seq"], row["plane_msg_id"]) for row in ordered]
        if any(type(seq) is not int or seq < 1 or not isinstance(msg, str)
               or _MSG.fullmatch(msg) is None for seq, msg in keys):
            raise ValueError
        if len({seq for seq, _ in keys}) != len(keys):
            raise ValueError
        return ordered
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("report prefix rows are not canonical ingest records") from exc


def _digest(rows: list[dict]) -> str:
    keys = [[row["_seq"], row["plane_msg_id"]] for row in rows]
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()


def _payload(prefix: ServedPrefix) -> dict:
    return {"v": 1, "identity": prefix.identity, "prior_seq": prefix.prior_seq,
            "prior_ack_seq": prefix.prior_ack_seq, "through_seq": prefix.through_seq,
            "through_message_id": prefix.through_message_id, "through_ts": prefix.through_ts,
            "count": prefix.count, "prefix_sha256": prefix.prefix_sha256}


def _prefix(value: dict) -> ServedPrefix:
    try:
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or type(value["v"]) is not int or value["v"] != 1):
            raise ValueError
        identity = _identity(value["identity"])
        prior, prior_ack = value["prior_seq"], value["prior_ack_seq"]
        through, count = value["through_seq"], value["count"]
        if ((prior is not None and (type(prior) is not int or prior < 0))
                or (prior_ack is not None and (type(prior_ack) is not int or prior_ack < 1))
                or type(through) is not int or through < 1 or through <= (prior or 0)
                or type(count) is not int or count < 1
                or not isinstance(value["through_message_id"], str)
                or _MSG.fullmatch(value["through_message_id"]) is None
                or not isinstance(value["through_ts"], str) or not 1 <= len(value["through_ts"]) <= 128
                or not isinstance(value["prefix_sha256"], str)
                or _SHA.fullmatch(value["prefix_sha256"]) is None):
            raise ValueError
        return ServedPrefix(identity, prior, prior_ack, through,
                            value["through_message_id"], value["through_ts"], count,
                            value["prefix_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorError("report cursor schema is invalid") from exc


def decode_cursor(root: Path, token: str, *, identity: dict) -> ServedPrefix:
    """Verify an existing key, signature, schema and exact stable identity."""
    expected = _identity(identity)
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= _TOKEN_LIMIT:
            raise ValueError
        blob = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if len(blob) <= 32:
            raise ValueError
    except (ValueError, TypeError, binascii.Error) as exc:
        raise CursorError("report cursor token is invalid") from exc
    raw, signature = blob[:-32], blob[-32:]
    key = _key(root, create=False)
    if not hmac.compare_digest(hmac.new(key, raw, hashlib.sha256).digest(), signature):
        raise CursorError("report cursor signature differs from the host key")
    try:
        prefix = _prefix(json.loads(raw))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CursorError("report cursor payload is invalid") from exc
    if prefix.identity != expected:
        raise CursorError("report cursor belongs to another active viewer or release")
    return prefix


def validate_prefix(prefix: ServedPrefix, rows: list[dict]) -> list[dict]:
    """Prove the previously served first N eligible rows still match the reader."""
    ordered = _ordered(rows)
    if any(row["_seq"] <= (prefix.prior_seq or 0) for row in ordered):
        raise CursorError("report cursor rows precede its prior read position")
    served = ordered[:prefix.count]
    if len(served) != prefix.count:
        raise CursorError("report cursor prefix differs from retained report history")
    if (served[-1]["_seq"] != prefix.through_seq
            or served[-1]["plane_msg_id"] != prefix.through_message_id
            or served[-1].get("ts") != prefix.through_ts
            or not hmac.compare_digest(_digest(served), prefix.prefix_sha256)):
        raise CursorError("report cursor prefix differs from retained report history")
    return served


def issue_cursor(root: Path, identity: dict, *, prior_seq: int | None,
                 prior_ack_seq: int | None, rows: list[dict]) -> str:
    """Sign a cumulative, nonempty ingest-ordered prefix actually served."""
    canonical_identity = _identity(identity)
    ordered = _ordered(rows)
    if (not ordered or any(row["_seq"] <= (prior_seq or 0) for row in ordered)
            or (prior_seq is not None and (type(prior_seq) is not int or prior_seq < 0))
            or (prior_ack_seq is not None and (type(prior_ack_seq) is not int or prior_ack_seq < 1))):
        raise CursorError("report cursor requires a valid served prefix")
    last = ordered[-1]
    if not isinstance(last.get("ts"), str) or not 1 <= len(last["ts"]) <= 128:
        raise CursorError("report cursor requires a display timestamp")
    prefix = ServedPrefix(canonical_identity, prior_seq, prior_ack_seq, last["_seq"],
                          last["plane_msg_id"], last["ts"], len(ordered), _digest(ordered))
    raw = json.dumps(_payload(prefix), sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_key(root, create=True), raw, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")
    if len(token) > _TOKEN_LIMIT:
        raise CursorError("report cursor exceeds its size bound")
    return token
