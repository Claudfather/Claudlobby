"""THE capture policy, stdlib-only, and the staged queue's bound.

Two consumers apply the same rule. `emit_api` applies it before ingest and the
spool; the socket client (lib/plane-socket-client.py) applies it before it
durably stages a batch the daemon could not take (S5a-04). A staged file must
never hold a fuller body than the policy allows: under metadata capture a
stranded or quarantined raw batch was the operator's opted-out content, kept
on disk for as long as the daemon stayed away.

The client imports this module inside its `python3 -S -E` process, so it may
import only the standard library and `registries` (itself stdlib-only). No
pydantic, no sqlite, no second interpreter per emission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from .registries import CONTENT_FIELDS, FIELD_POLICY

#: The shipped capture policy when nothing is configured (see emit_api for the
#: 2026-09-20 ruling and what the default trades).
DEFAULT_CAPTURE = "full"

#: The staged queue's bound (S5a-01). Staging is the only record a hook or a
#: timer has while the daemon is away, and its only consumer is that daemon.
#: Past either bound the client refuses the batch (exit 3) and appends a row to
#: `.emit-losses`; it never grows the queue without limit. 2,000 batches is
#: about half an hour of one busy fleet's hook traffic, and each file costs at
#: least one 4 KiB block.
STAGED_MAX_BATCHES = 2000
STAGED_MAX_BYTES = 32 * 1024 * 1024


class CaptureConfigInvalid(ValueError):
    """state/plane/capture.json exists but cannot be trusted. An environment
    fault, not a batch fault: the batch is intact and becomes recordable once
    the file is fixed. emit_api raises its ContractViolation subclass."""

    def __init__(self, errors):
        self.errors = errors
        # Not super(): emit_api's subclass also derives from ContractViolation,
        # whose __init__ would otherwise receive str(errors) as its errors.
        ValueError.__init__(self, str(errors))


def load_capture_config(root) -> dict:
    """Absent is the documented default ({}); a broken file raises rather than
    resolving to either mode. The WHOLE file must be valid: a typo'd mode on
    any fleet is a policy error someone believes is in force."""
    cfg = os.path.join(os.fspath(root), "state", "plane", "capture.json")
    try:
        with open(cfg) as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise CaptureConfigInvalid(
            [{"loc": ("capture.json",), "msg": f"unreadable: {exc}"}]
        ) from exc
    try:
        modes = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaptureConfigInvalid(
            [{"loc": ("capture.json",), "msg": f"invalid JSON: {exc}"}]
        ) from exc
    if not isinstance(modes, dict) or not all(
        isinstance(k, str) and v in ("full", "metadata") for k, v in modes.items()
    ):
        raise CaptureConfigInvalid(
            [{"loc": ("capture.json",),
              "msg": "must map fleet (or '*') to 'full' | 'metadata'"}]
        )
    return modes


def capture_mode(modes: dict, fleet: str | None) -> str:
    """Fleet-keyed mode from the loaded config. The caller's request never
    decides this."""
    return modes.get(fleet or "", modes.get("*", DEFAULT_CAPTURE))


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def body_proof(text: str) -> tuple[str, int, str, bool]:
    """(capped body, full byte count, sha256 of the full stripped body,
    truncated). ANSI-stripped, capped at FIELD_POLICY's communication-body cap
    read at call time, UTF-8 safe."""
    stripped = _ANSI_RE.sub("", text)
    raw = stripped.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    cap = FIELD_POLICY[("communication", "body")]["cap"]
    if len(raw) <= cap:
        return stripped, len(raw), digest, False
    return raw[:cap].decode("utf-8", errors="ignore"), len(raw), digest, True


def needs_config(raw: dict) -> bool:
    """Whether this event's family carries content the policy governs."""
    return bool(CONTENT_FIELDS.get(raw.get("event_type")))


def apply_capture(raw: dict, modes: dict) -> dict:
    """Transform EVERY content-bearing family (CONTENT_FIELDS). Communications
    keep the proof triple on drop.

    IDENTITY CONTRACT (T8): returns the input object itself when the policy
    changed nothing; emit_api uses `is` to skip a second validation pass.

    A communication whose body was already withheld (body None with its proof
    hash, as the socket client stages it under metadata capture) stays
    `metadata` under a later `full` policy: the words are gone, and labelling
    the row `full` would claim they were kept."""
    fields = CONTENT_FIELDS.get(raw.get("event_type"))
    if not fields:
        return raw
    mode = capture_mode(modes, raw.get("fleet"))
    if raw.get("event_type") == "communication":
        payload = dict(raw.get("payload") or {})
        body = payload.get("body")
        if mode == "full" and not (body is None and payload.get("body_sha256")):
            payload["privacy"] = "full"
        else:
            payload["privacy"] = "metadata"
            if body is not None:
                _capped, size, digest, truncated = body_proof(body)
                payload["body"] = None      # dropped AT THE DOOR (F23)
                payload["body_bytes"] = size
                payload["body_sha256"] = digest
                payload["truncated"] = truncated
        return {**raw, "payload": payload}
    if mode == "full":
        return raw
    payload = dict(raw.get("payload") or {})
    dropped = False
    for field in fields:
        if field in payload:
            payload.pop(field)              # dropped, no proof triple owed
            dropped = True
    if not dropped:
        return raw
    return {**raw, "payload": payload}


_PRIVACY_VALUES = ("metadata", "preview", "full")


def _refuse_before_strip(raw: dict, captured: dict) -> None:
    """What replay can no longer see once the client stripped it. emit_batch
    validates RAW first (#1372 review F1): an over-cap authored field with
    REJECT semantics, or an invalid privacy value, must be refused, not
    laundered into a valid staged shape. ValueError, a contract refusal."""
    before, after = raw.get("payload"), captured.get("payload")
    if not isinstance(before, dict):
        raise ValueError(f"{raw.get('event_type')} payload must be an object")
    family = raw.get("event_type")
    if family == "communication":
        if "privacy" in before and before["privacy"] not in _PRIVACY_VALUES:
            raise ValueError(f"communication.privacy {before['privacy']!r} is not "
                             f"one of {', '.join(_PRIVACY_VALUES)}")
        return
    for field in CONTENT_FIELDS.get(family, ()):
        value = before.get(field)
        if field in after or value is None:
            continue
        cap = FIELD_POLICY[(family, field)].get("cap")
        if not isinstance(value, str) or cap is not None and len(value.encode("utf-8")) > cap:
            raise ValueError(f"{family}.{field} exceeds {cap} bytes or is not text")


def apply_to_batch(root, events: list) -> list:
    """The policy-applied form of a raw batch, for the socket client's stage.
    Reads capture.json at most once, and only when a content family is
    present, so a content-free batch stages under a broken file exactly as
    emit_batch accepts it. CaptureConfigInvalid propagates; a field the policy
    would strip before replay could refuse it raises ValueError."""
    modes = None
    out = []
    for e in events:
        if not needs_config(e):
            out.append(e)
            continue
        if modes is None:
            modes = load_capture_config(root)
        if capture_mode(modes, e.get("fleet")) == "full":
            # Nothing to withhold: stage the event as given, so replay's
            # RAW-first validation still sees exactly what the caller sent.
            out.append(e)
            continue
        captured = apply_capture(e, modes)
        if captured is not e:
            _refuse_before_strip(e, captured)
        out.append(captured)
    return out
