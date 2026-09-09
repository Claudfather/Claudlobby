"""Minted identifiers (design v2 §3, F10). Names are aliases; uids are truth.

A corrupted host-uid file is a hard error, never silently re-minted: re-minting
would fork every subsequent row's host identity from the estate's history,
which is exactly the longitudinal-join corruption F10 exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

_UID_PREFIX = {
    "host": "host_",
    "fleet": "fleet_",
    "actor": "actor_",
    "bot_instance": "boti_",
    "session": "sess_",
    "vault": "vault_",
    "project": "proj_",
    "library_item": "lib_",
    # F12 refinement (§19.6, delivered PR-B T7): session_uid is the TRANSCRIPT
    # identity (stable across resume — empirically confirmed 2026-08-25);
    # process_uid distinguishes the concurrent RESUMES of one transcript —
    # minted fresh per process at SessionStart, never derived.
    "process": "proc_",
}

# Anchored (round-2 F9): pydantic's pattern is a SEARCH — unanchored patterns
# accept embedded garbage. ^...$ anchors work in both Rust-regex and re.
ID_PATTERNS: dict[str, str] = {
    "event": r"^ev_[0-9a-f]{32}$",
    "msg": r"^msg_[0-9a-f]{32}$",
    "work_item": r"^wi_[0-9a-f]{32}$",
    "assignment": r"^asg_[0-9a-f]{32}$",
    **{kind: "^" + prefix + r"[0-9a-f]{32}$" for kind, prefix in _UID_PREFIX.items()},
}

_HOST_UID_RE = re.compile(r"^host_[0-9a-f]{32}$")


def mint(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def derive_hex(material: str) -> str:
    """The deterministic 32-hex the plane derives from content: sha256 of
    *material*, truncated — ONE definition, so the truncation and hash are a
    single decision (expiry's `expired` event ids, the importer's ids, the
    parity content key all ride it)."""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def derive_uid(prefix: str, material: str) -> str:
    """``<prefix>_<derive_hex(material)>`` — a minted-shape id that is a pure
    function of its material, so a replay classifies duplicate."""
    return f"{prefix}_{derive_hex(material)}"


def mint_event_id() -> str:
    return mint("ev_")


def mint_msg_id() -> str:
    return mint("msg_")


def mint_work_item_id() -> str:
    return mint("wi_")


def mint_assignment_id() -> str:
    return mint("asg_")


def derive_session_uid(platform_session_id: str) -> str:
    """sess_ uid DERIVED from the platform session id (sha256, first 32 hex).

    Deliberately deterministic, not random (§9d): any emitter — bash included,
    via shasum — computes the same uid for the same session with no registry
    lookup, and the transcript/OTel join needs exactly that stability."""

    if not platform_session_id or not platform_session_id.strip():
        raise ValueError("empty platform session id — refusing to derive")
    digest = hashlib.sha256(platform_session_id.encode("utf-8")).hexdigest()
    return "sess_" + digest[:32]


def mint_uid(kind: str) -> str:
    return mint(_UID_PREFIX[kind])


def ensure_host_uid(state_dir: Path) -> str:
    """Read the persisted host uid, minting it exactly once (atomic, 0600)."""
    state_dir = Path(state_dir)
    path = state_dir / "host-uid"
    if path.exists():
        value = path.read_text().strip()
        if not _HOST_UID_RE.fullmatch(value):
            raise ValueError(
                f"corrupt host-uid at {path}: {value!r} — refusing to re-mint; "
                "restore from backup or delete deliberately"
            )
        os.chmod(path, 0o600)   # round-4 note: a pre-existing valid file may
                                 # carry its creator's mode; the uid is joined
                                 # against every row — owner-only, always
        # Round-3 F2 (verifier's window): a loser can reach this fast path
        # after the winner's os.link but BEFORE the winner's directory fsync —
        # returning a uid whose dirent a power loss could still erase, while
        # the caller starts recording events under it. Fsync the directory
        # before EVERY return, so the published name is durable no matter
        # which racer syncs it first.
        dfd = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return value
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    value = mint_uid("host")
    # Round-3 F2: NEVER publish the final pathname before its content exists.
    # Write+fsync a UNIQUE tmp, then os.link() it into place — link fails
    # EEXIST if a winner already published (create-if-absent), and the final
    # name only ever appears fully written. A crash leaves only tmp litter.
    tmp = state_dir / f".host-uid.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (value + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass                      # loser: the winner's COMPLETE file is there
    finally:
        os.unlink(tmp)
    dfd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return ensure_host_uid(state_dir)   # single read path validates the result
