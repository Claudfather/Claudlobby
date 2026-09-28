"""Interrupted selection stays recoverable and never implies resumed intake."""

import json

import pytest

from claudlobby import activation_state as a
from tests.test_config_plan import proposal
from tests.test_releases import installed


def _prepare(store, plan, name="candidate"):
    return store.prepare(name, plan, recovery_release_id=plan.release_id,
                         enrollment_digest="1" * 64)


def _advance(store, name, steps):
    for step in steps:
        store.begin(name, step)
        store.complete(name, step, evidence_digest="2" * 64)


def test_lock_and_unfinished_intent_prevent_competing_cutovers(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        with pytest.raises(a.ActivationError, match="holds the lock"):
            with a.locked_activation(builder.root):
                pytest.fail("another lock was admitted")
        first = _prepare(store, plan)
        assert first.status == "prepared"
        assert _prepare(store, plan) == first
        with pytest.raises(a.ActivationError, match="unfinished activation"):
            _prepare(store, plan, "competing")
        with pytest.raises(a.ActivationError, match="out of order"):
            store.begin("candidate", "ingest_started")
    with pytest.raises(a.ActivationError, match="lock is not held"):
        store.begin("candidate", "producers_paused")
    assert a.read_selection(builder.root) is None


def test_interrupted_switch_reconciles_exact_selection_without_resuming(proposal, monkeypatch):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        _prepare(store, plan)
        _advance(store, "candidate", a.STEPS[:a.STEPS.index("selection_switched")])
        store.begin("candidate", "selection_switched")
        write = a._write

        def crash(path, value):
            if path.name == "activation.json":
                raise OSError("interrupted after pointer replacement")
            write(path, value)

        with monkeypatch.context() as patch:
            patch.setattr(a, "_write", crash)
            with pytest.raises(OSError, match="interrupted"):
                store.select("candidate")
        selected = a.read_selection(builder.root)
        assert selected["release_id"] == plan.release_id
        assert a.read_activation(builder.root, "candidate").status == "activating"
        assert a.read_activation(builder.root, "candidate").body["pending"] == "selection_switched"
    with a.locked_activation(builder.root) as store:
        recovered = store.select("candidate")
        assert recovered.status == "activating"
        assert recovered.body["pending"] is None
        assert a.read_selection(builder.root) == selected
        _advance(store, "candidate", a.STEPS[a.STEPS.index("selection_switched") + 1:])
        assert a.read_activation(builder.root, "candidate").status == "active"


def test_rollback_preserves_forward_evidence_and_refuses_foreign_pointer(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        _prepare(store, plan)
        _advance(store, "candidate", a.STEPS[:a.STEPS.index("selection_switched")])
        store.begin("candidate", "selection_switched")
        store.select("candidate")
        forward = a.read_activation(builder.root, "candidate").body.copy()
        store.begin_rollback("candidate")
        _advance(store, "candidate", a.ROLLBACK_STEPS[:a.ROLLBACK_STEPS.index("selection_restored")])
        store.begin("candidate", "selection_restored")
        pointer = builder.root / "state/selected-release.json"
        selected = pointer.read_bytes()
        other = json.loads(selected)
        other["activation_id"] = "foreign"
        pointer.write_text(json.dumps(other))
        with pytest.raises(a.ActivationError, match="outside this activation"):
            store.restore_selection("candidate")
        assert json.loads(pointer.read_bytes()) == other
        pointer.write_bytes(selected)
        store.restore_selection("candidate")
        _advance(store, "candidate", a.ROLLBACK_STEPS[a.ROLLBACK_STEPS.index("selection_restored") + 1:])
        result = a.read_activation(builder.root, "candidate")
        assert result.status == "rolled_back"
        assert result.body["forward"]["completed"] == forward["completed"]
        assert a.read_selection(builder.root) is None
