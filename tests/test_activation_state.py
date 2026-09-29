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


def test_cancel_only_unstarted_prepared_intent_retains_record(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        _prepare(store, plan, "cancelled")
        record = store.cancel_prepared("cancelled")
        assert record.status == "rolled_back"
        assert record.body["cancellation"]["kind"] == "prepared-before-effects"
        assert record.body["cancellation"]["journals"] == []
        assert a.read_activation(builder.root, "cancelled") == record
        _prepare(store, plan, "successor")
        with pytest.raises(a.ActivationError, match="effects or changed selection"):
            store.cancel_prepared("cancelled")


@pytest.mark.parametrize("change", ["config", "parking", "step", "selection"])
def test_cancel_prepared_refuses_any_uncertain_effect(proposal, change):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        _prepare(store, plan)
        if change == "config":
            (builder.root / "state/activations/candidate/config").mkdir()
        elif change == "parking":
            from claudlobby.activation_units import journal_id
            (builder.root / "state/activations" / journal_id("candidate", "producers")).mkdir()
        elif change == "step":
            store.begin("candidate", "producers_paused")
        else:
            a._write(builder.root / "state/selected-release.json", {
                "schema": 1, "activation_id": "foreign", "release_id": plan.release_id,
                "plan_id": plan.plan_id})
        with pytest.raises(a.ActivationError):
            store.cancel_prepared("candidate")
        assert a.read_activation(builder.root, "candidate").status != "rolled_back"


def test_first_adoption_records_unsealed_source_without_inventing_release(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    with a.locked_activation(builder.root) as store:
        record = store.prepare("adopt", plan, recovery_release_id=plan.release_id,
                               enrollment_digest="1" * 64, legacy_source=True)
        assert record.body["intent"]["source_kind"] == "legacy-unsealed"
        assert record.body["intent"]["source_release_id"] is None
        assert store.prepare("adopt", plan, recovery_release_id=plan.release_id,
                             enrollment_digest="1" * 64, legacy_source=True) == record
        with pytest.raises(a.ActivationError, match="repair forward"):
            store.begin_rollback("adopt")
        with pytest.raises(a.ActivationError, match="different intent"):
            _prepare(store, plan, "adopt")
        with pytest.raises(a.ActivationError, match="cannot claim a selected source"):
            store.prepare("other", plan, recovery_release_id=plan.release_id,
                          source_release_id=plan.release_id, enrollment_digest="1" * 64,
                          legacy_source=True)


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
