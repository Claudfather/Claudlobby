"""What a queued batch's name says about the transmissions in it (#2430).

The receipt check passes over an entry, and replay puts it behind the rest, on
its writer's word in the name. So a name must never set aside an entry that the
receipt check's own match would find. These pin the grammar, both writers and
the operator door that reads the names."""

import json
import random
import re
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import message_queries
from claudlobby.plane import queue_names as names
from claudlobby.plane.fleet_events import fleet_event_request
from claudlobby.plane.spool import spool_write

CLIENT = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/plane-socket-client.py"
LEAD = "ev_" + "0" * 32


def message(number):
    return f"msg_{number:032x}"


def tag_for(number):
    return ".m" + message(number)[len("msg_"):]


def transmission(msg_id, state="received"):
    return {"event_type": "transmission", "emitter": "dispatch-in-hook", "fleet": "a",
            "payload": {"msg_id": msg_id, "attempt_no": 1, "carrier": "tmux",
                        "destination": "worker", "state": state}}


def tool_call(number=0):
    return fleet_event_request("tool_call", fleet="a", subject_kind="actor", subject="bot:a/worker",
                               source="vitals", data={"event": "PreToolUse", "tool": "Bash", "n": number})


def nested(*events):
    """A system event whose data carries copies of others, as a door may record."""
    return {**tool_call(), "payload": {"event": "copied", "data": {"events": list(events)}}}


@pytest.mark.parametrize("events, tag", [
    ([], ".np"),
    ([tool_call()], ".np"),
    ([tool_call(), transmission(message(1))], tag_for(1)),
    ([transmission(message(1)), transmission(message(1), "pane_submitted")], tag_for(1)),
    ([transmission(message(1)), transmission(message(2))], ""),
    ([transmission("msg_1")], ""),
    ([transmission(None)], ""),
    ([{"event_type": "transmission", "payload": "not an object"}], ""),
    ([nested(transmission(message(3)))], tag_for(3)),
    ([nested(transmission(message(3))), transmission(message(1))], ""),
], ids=["empty", "no-transmission", "one-message", "one-message-twice", "two-messages",
        "short-id", "no-id", "no-payload", "nested", "nested-and-another"])
def test_a_tag_says_which_message_a_batchs_transmissions_name(events, tag):
    assert names.name_tag(events) == tag


@pytest.mark.parametrize("name, for_message_1, for_any", [
    (f"1-{LEAD}.np.batch", False, False),
    (f"1-{LEAD}{tag_for(1)}.batch", True, True),
    (f"1-{LEAD}{tag_for(2)}.batch", False, True),
    (f".1-{LEAD}.np.batch.42.tmp", False, False),
    (f".1-{LEAD}{tag_for(2)}.batch.42.tmp", False, True),
    (f"{LEAD}.np.json", False, False),
    (f"{LEAD}{tag_for(1)}.json", True, True),
    (f"{LEAD}{tag_for(2)}.json.inflight.42.ab12", False, True),
    # A name with no tag, or one outside the grammar, says nothing: it is read.
    (f"1-{LEAD}.batch", True, True),
    (f".1-{LEAD}.batch.42.tmp", True, True),
    (f"{LEAD}.json", True, True),
    (f".{LEAD}.tmp", True, True),
    ("1-ev_x.np.batch", True, True),
    (f"1-{LEAD}.m{'0' * 29}ABC.batch", True, True),
    (f"1-{LEAD}.np.batch.tmp", True, True),
    (f"1-{LEAD}.np.json", True, True),
    (f"{LEAD}.np.batch", True, True),
], ids=["staged-none", "staged-this", "staged-other", "temp-none", "temp-other", "spool-none",
        "spool-this", "claimed-other", "staged-untagged", "temp-untagged", "spool-untagged",
        "older-temp", "lead-not-an-id", "upper-case-tag", "temp-without-pid", "staged-prefix-spool-suffix",
        "spool-prefix-staged-suffix"])
def test_only_a_whole_tagged_name_sets_an_entry_aside(name, for_message_1, for_any):
    assert names.may_hold_transmission(name, message(1)) is for_message_1
    assert names.may_hold_any_transmission(name) is for_any


def test_no_name_sets_aside_an_entry_the_receipt_checks_own_match_finds():
    """The property both readers rely on, over 2000 seeded batches of tool
    calls, transmissions of three messages and nested copies: wherever the
    receipt check's own match (_queued_proof) finds a message's proof in a file
    as each writer lays it out, that file's name leaves room for it."""
    rng = random.Random(0)

    def event():
        roll = rng.random()
        if roll < 0.5:
            return tool_call(rng.randrange(9))
        proof = transmission(message(rng.randrange(1, 4)), rng.choice(["received", "pane_submitted"]))
        return proof if roll < 0.9 else nested(proof)

    found = 0
    for _ in range(2000):
        events = [event() for _ in range(rng.randrange(1, 4))]
        tag = names.name_tag(events)
        spooled = {"event_ids": [LEAD], "error": "database is locked", "history": [], "requests": events}
        for name, content in ((f"1-{LEAD}{tag}.batch", {"events": events}),
                              (f".1-{LEAD}{tag}.batch.7.tmp", {"events": events}),
                              (f"{LEAD}{tag}.json", spooled),
                              (f"{LEAD}{tag}.json.inflight.7.ab", spooled)):
            parsed = json.loads(json.dumps(content))
            for number in range(1, 4):
                if message_queries._queued_proof(parsed, message(number)):
                    found += 1
                    assert names.may_hold_transmission(name, message(number)), (name, events)
                    assert names.may_hold_any_transmission(name), (name, events)
    assert found > 1000  # the property was exercised, not vacuously true


def test_both_writers_name_their_batches_by_the_messages_named(tmp_path):
    """The socket client's stage and the spool, each as its owner writes it."""
    client = runpy.run_path(str(CLIENT))
    staged = tmp_path / "state/plane/staged"
    for events in ([tool_call()], [transmission(message(1))],
                   [transmission(message(1)), transmission(message(2))]):
        assert client["_stage_pending"](str(staged), client["_finalize"](events), "test") == 7
    first, second, third = sorted(path.name for path in staged.iterdir())  # staged in this order
    assert re.fullmatch(r"\d+-ev_[0-9a-f]{32}\.np\.batch", first), first
    assert re.fullmatch(rf"\d+-ev_[0-9a-f]{{32}}{re.escape(tag_for(1))}\.batch", second), second
    assert re.fullmatch(r"\d+-ev_[0-9a-f]{32}\.batch", third), third
    other = spool_write(tmp_path, client["_finalize"]([tool_call()]), "database is locked").name
    proof = spool_write(tmp_path, client["_finalize"]([transmission(message(2))]), "database is locked").name
    assert re.fullmatch(r"ev_[0-9a-f]{32}\.np\.json", other), other
    assert re.fullmatch(rf"ev_[0-9a-f]{{32}}{re.escape(tag_for(2))}\.json", proof), proof


def test_the_spool_door_reads_an_entry_under_its_tagged_name(tmp_path, monkeypatch):
    from claudlobby.commands import plane_maintenance
    name = spool_write(tmp_path, [{**transmission(message(1)), "event_id": LEAD}], "database is locked").name
    assert name == f"{LEAD}{tag_for(1)}.json"
    monkeypatch.setattr(plane_maintenance, "_root", lambda _args: tmp_path)
    shown = plane_maintenance.spool(SimpleNamespace(spool_action="inspect", name=name))
    assert shown.data["name"] == name and shown.data["entry"]["event_ids"] == [LEAD]
    assert plane_maintenance._QUARANTINED_NAME.fullmatch(f"1-{LEAD}.np.batch.42.json")
