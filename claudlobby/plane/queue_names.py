"""What a queued batch's name says about the transmissions in it (#2430).

A receipt check looks for one message's transmission proof in the staged queue
and the spool before ingest commits it, and it reads at most 256 entries. Under
load nearly every hook emit stages, so the queue held hundreds of other bots'
tool calls, and every receipt whose proof was not yet committed read
"unavailable" until the queue drained. So each writer names a batch for the
message its transmissions name:

    <time_ns>-<lead event>.np.batch         staged, no transmission in it
    <time_ns>-<lead event>.m<hex>.batch     staged, every transmission names msg_<hex>
    <lead event>.np.json                    the same two in the spool
    <lead event>.m<hex>.json

A temp stage (`.<name>.<pid>.tmp`) and a spool entry a drain has claimed
(`<name>.inflight.<pid>.<hex>`) carry the name they were written for. A name
with no tag says nothing: an older writer's, a batch whose transmissions name
several messages, one that names a message in some other form, or one nested
deeper than the walk reaches (#2446). Readers read it, as they always have.

Stdlib only: the socket client imports this inside its `python3 -S -E` process,
beside capture_policy.
"""

from __future__ import annotations

import re

#: The tag in a name, for a grammar that accepts one (the `plane spool` door).
TAG = r"\.(?:np|m[0-9a-f]{32})"

_MESSAGE = re.compile(r"msg_([0-9a-f]{32})")
_LEAD = r"ev_[0-9a-f]{32}"
_TAGGED = re.compile(
    rf"\d+-{_LEAD}\.(np|m[0-9a-f]{{32}})\.batch"                         # a staged batch
    rf"|\.\d+-{_LEAD}\.(np|m[0-9a-f]{{32}})\.batch\.\d+\.tmp"            # its temp, or an orphan
    rf"|{_LEAD}\.(np|m[0-9a-f]{{32}})\.json"                             # a spool entry
    rf"|{_LEAD}\.(np|m[0-9a-f]{{32}})\.json\.inflight\.\d+\.[0-9a-f]+")  # one a drain has claimed


def name_tag(events) -> str:
    """The tag a writer puts in the name of a batch of these events: ".np" when
    none is a transmission, ".m<hex>" when every transmission names msg_<hex>,
    and "" when the name cannot say. It walks every nested object, as the
    receipt check's own match does, so a tag never sets aside an entry that
    match would find: a tuple as the list json writes it as, and a batch
    nested past the walk's reach (json reads about 990 levels, the walk about
    340) gets no tag (#2446)."""
    named: set = set()
    try:
        whole = _named(events, named)
    except RecursionError:
        return ""
    if not whole or len(named) > 1:
        return ""
    return ".m" + named.pop() if named else ".np"


def _named(value, named: set) -> bool:
    """Add the message every transmission in value names; False when one names
    none in canonical form."""
    if isinstance(value, (list, tuple)):
        return all(_named(item, named) for item in value)
    if not isinstance(value, dict):
        return True
    if value.get("event_type") == "transmission":
        payload = value.get("payload")
        message = payload.get("msg_id") if isinstance(payload, dict) else None
        found = _MESSAGE.fullmatch(message) if isinstance(message, str) else None
        if found is None:
            return False
        named.add(found.group(1))
    return all(_named(item, named) for item in value.values())


def _tag(name: str) -> str | None:
    found = _TAGGED.fullmatch(name)
    return found.group(found.lastindex) if found else None


def may_hold_transmission(name: str, message_id: str) -> bool:
    """False only when an entry's name says it holds no transmission of
    message_id: it is tagged for none, or for another message."""
    tag = _tag(name)
    return tag is None or (tag != "np" and "msg_" + tag[1:] == message_id)


def may_hold_any_transmission(name: str) -> bool:
    """False only when an entry's name says it holds no transmission at all."""
    return _tag(name) != "np"
