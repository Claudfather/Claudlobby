from __future__ import annotations

import pytest

from claudlobby.plane.contracts import (
    FAMILIES,
    ContractViolation,
    cap_body,
    export_schemas,
    validate_request,
)


def _req(event_type: str, payload: dict) -> dict:
    return {
        "event_type": event_type,
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "payload": payload,
    }


def _intent_payload(**over) -> dict:
    p = {
        "msg_id": "msg_" + "0" * 32,
        "sender": "bot:example-fleet/alpha",
        "recipient": "bot:example-fleet/beta",
        "message_class": "task_request",
        "command_type": "task",
        "body": "review PR 42",
        "privacy": "full",
    }
    p.update(over)
    return p


def test_families_registered():
    # "system" joined in Phase 2 PR-A (the daemon's lifecycle events are its
    # first emitter; vocabulary registry-governed, F19). "workstream" +
    # "workstream_event" joined in PR-B T6 with the workstream-update door
    # (wire name suffixed per spec ruling #8 — the construct/kind collision).
    # The registry lane's three joined in Phase 2b (§18): registry_snapshot
    # + metric_sample (construct tables) and declaration (events
    # kind=declaration — the provenance chain the hash gate never touches).
    assert set(FAMILIES) == {
        "communication", "transmission", "work_item", "assignment", "task",
        "system", "workstream", "workstream_event",
        "registry_snapshot", "metric_sample", "declaration",
    }


def test_valid_intent_parses():
    env, payload = validate_request(_req("communication", _intent_payload()))
    assert env.event_type == "communication"
    assert payload.message_class == "task_request"
    assert payload.body_bytes == len(b"review PR 42")
    assert payload.truncated is False


def test_unknown_event_type_is_violation():
    with pytest.raises(ContractViolation):
        validate_request(_req("nonsense", {}))


def test_unknown_message_class_is_violation_not_coercion():
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(message_class="shout")))


def test_extra_fields_rejected():
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(surprise=1)))


def test_body_cap_truncates_and_hashes():
    big = "x" * 20_000
    fields = cap_body(big)
    assert fields.truncated is True
    assert fields.body_bytes == 20_000
    assert len(fields.body.encode()) <= 16_384
    assert fields.body_sha256.startswith("sha256:")


def test_body_ansi_stripped():
    fields = cap_body("\x1b[31mred\x1b[0m plain")
    assert fields.body == "red plain"


def test_fleet_required_for_scoped_types():
    req = _req("comm_intent", _intent_payload()) if False else _req("communication", _intent_payload())
    req.pop("fleet")
    with pytest.raises(ContractViolation):
        validate_request(req)


def test_payload_envelope_duplicates_rejected():
    """Round-3 F4: correlation/causation/trace/span are envelope-only."""
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(correlation_id="x")))


def test_caps_enforce_from_field_policy(monkeypatch):
    """Round-5/6 F8: FIELD_POLICY is the SSOT for EVERY content family —
    shrinking any cap changes enforcement with no other edit."""
    from claudlobby.plane import registries

    monkeypatch.setitem(
        registries.FIELD_POLICY, ("task", "summary"),
        {"class": "CONTENT", "cap": 8},
    )
    with pytest.raises(ContractViolation):
        validate_request(_req("task", {
            "work_item_id": "wi_" + "0" * 32, "event": "progress",
            "summary": "longer than eight bytes",
        }))
    monkeypatch.setitem(
        registries.FIELD_POLICY, ("work_item", "body"),
        {"class": "CONTENT", "cap": 8},
    )
    with pytest.raises(ContractViolation):
        validate_request(_req("work_item", {
            "work_item_id": "wi_" + "0" * 32, "title": "t",
            "created_by": "bot:example-fleet/alpha",
            "body": "longer than eight bytes",
        }))
    monkeypatch.setitem(
        registries.FIELD_POLICY, ("communication", "body"),
        {"class": "CONTENT", "cap": 8, "proof": True},
    )
    _, payload = validate_request(_req("communication", _intent_payload(
        body="longer than eight bytes")))
    assert payload.truncated is True and payload.body_bytes > 8


def test_work_item_body_cap_is_bytes():
    fat = "\u00e9" * 10_000        # 10k chars, 20k bytes
    with pytest.raises(ContractViolation):
        validate_request(_req("work_item", {
            "work_item_id": "wi_" + "0" * 32, "title": "t",
            "created_by": "bot:example-fleet/alpha", "body": fat,
        }))


def test_receiver_acknowledged_is_gone():
    # 22 = the F9-v2.1 nineteen + supplied_id_not_open (§6b #6, PR-B)
    # + escalated and nudged (the task loop's human acts, chunk M-A #1481).
    from claudlobby.plane.contracts import TASK_EVENTS
    assert "receiver_acknowledged" not in TASK_EVENTS and len(TASK_EVENTS) == 22
    assert "supplied_id_not_open" in TASK_EVENTS
    assert {"escalated", "nudged"} <= set(TASK_EVENTS)


def test_task_event_vocabulary_enforced():
    good = {"work_item_id": "wi_" + "0" * 32, "event": "blocked_waiting"}
    env, payload = validate_request(_req("task", good))
    assert payload.event == "blocked_waiting"
    with pytest.raises(ContractViolation):
        validate_request(_req("task", {**good, "event": "blocked"}))


def test_transmission_states():
    good = {
                "msg_id": "msg_" + "0" * 32,
        "attempt_no": 1,
        "carrier": "tmux",
        "destination": "bot:example-fleet/beta",
        "state": "pane_submitted",
    }
    _, payload = validate_request(_req("transmission", good))
    assert payload.state == "pane_submitted"
    with pytest.raises(ContractViolation):
        validate_request(
            _req("transmission", {**good, "state": "delivered"})  # banned word
        )


def test_schemas_export():
    schemas = export_schemas()
    assert "envelope" in schemas and "communication" in schemas
    assert schemas["communication"]["title"] == "Communication"


def test_the_acts_authored_text_is_capped_and_by_survives_a_metadata_capture():
    """FOLD F11. `reason` / `question` are CONTENT (a metadata capture strips
    them with every other authored text). `by` must SURVIVE that capture — a
    card reading "needs you" with no asker is a question nobody can route —
    but it is authored input all the same, and it shipped as the one field in
    the family with NO cap at all, so a 4 KB "name" would ride into every
    attention card. METADATA, and capped like a name."""
    import pytest as _pytest

    from claudlobby.plane.contracts import ContractViolation, validate_request
    from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY

    assert FIELD_POLICY[("task", "by")]["class"] == "METADATA"
    assert FIELD_POLICY[("task", "by")]["cap"] == 128
    assert "by" not in CONTENT_FIELDS["task"]          # survives a metadata capture
    assert {"reason", "question"} <= set(CONTENT_FIELDS["task"])   # and these do not

    def _req(**payload):
        return {"event_type": "task", "emitter": "t", "fleet": "f",
                "payload": {"work_item_id": "wi_" + "0" * 32,
                            "event": "nudged", **payload}}

    validate_request(_req(by="c" * 128))
    with _pytest.raises(ContractViolation):
        validate_request(_req(by="c" * 129))
    with _pytest.raises(ContractViolation):
        validate_request(_req(reason="r" * 4097))
    with _pytest.raises(ContractViolation):
        validate_request(_req(question="q" * 4097))
