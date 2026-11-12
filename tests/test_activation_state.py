"""Retained activation evidence is strict, read-only and never repaired or resumed.

Historical envelopes are authored directly (exact on-disk schema and digest);
no activation writer, native service or bootstrap path is involved.
"""

import json

import pytest

from claudlobby import activation_state as a


def write_activation(
    root,
    activation_id,
    *,
    status="active",
    pending=None,
    completed=(),
    release_id="r-" + "a" * 64,
    plan_id="p-" + "b" * 64,
    **extra,
):
    """Author one historical activation.json exactly as the retired writer sealed it."""
    root = root.resolve()
    body = {
        "schema": 1,
        "activation_id": activation_id,
        "root": str(root),
        "status": status,
        "pending": pending,
        "completed": list(completed),
        "intent": {"release_id": release_id, "plan_id": plan_id},
        **extra,
    }
    path = root / "state/activations" / activation_id / "activation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"record": body, "sha256": a._digest(body)}))
    return path, body


def write_selection(
    root, activation_id, release_id="r-" + "a" * 64, plan_id="p-" + "b" * 64
):
    value = {
        "schema": 1,
        "activation_id": activation_id,
        "release_id": release_id,
        "plan_id": plan_id,
    }
    path = root.resolve() / "state/selected-release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path, value


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_sealed_writer_and_resume_surface_are_gone():
    for name in ("ActivationStore", "locked_activation", "STEPS", "ROLLBACK_STEPS"):
        assert not hasattr(a, name)
    assert issubclass(a.ActivationRefusal, a.ActivationError)


def test_read_activation_returns_the_recorded_body_without_touching_it(tmp_path):
    path, body = write_activation(
        tmp_path,
        "previous",
        status="activating",
        pending="producers_paused",
        completed=["producers_paused"],
    )
    before = snapshot(tmp_path)
    record = a.read_activation(tmp_path, "previous")
    assert record.status == "activating" and record.activation_id == "previous"
    assert record.body == body and record.root == tmp_path.resolve()
    assert snapshot(tmp_path) == before and path.exists()


@pytest.mark.parametrize(
    "tamper", ["body", "digest", "id", "root", "schema", "garbage", "shape"]
)
def test_read_activation_refuses_changed_or_unreadable_evidence(tmp_path, tamper):
    path, body = write_activation(tmp_path, "previous")
    raw = json.loads(path.read_text())
    if tamper == "body":
        raw["record"]["status"] = "rolled_back"  # digest no longer matches
    elif tamper == "digest":
        raw["sha256"] = "0" * 64
    elif tamper == "id":
        raw["record"] = {**body, "activation_id": "other"}
        raw["sha256"] = a._digest(raw["record"])
    elif tamper == "root":
        raw["record"] = {**body, "root": str(tmp_path.resolve() / "elsewhere")}
        raw["sha256"] = a._digest(raw["record"])
    elif tamper == "schema":
        raw["record"] = {**body, "schema": 2}
        raw["sha256"] = a._digest(raw["record"])
    elif tamper == "shape":
        raw = ["not", "an", "envelope"]
    if tamper == "garbage":
        path.write_text("not json")
    else:
        path.write_text(json.dumps(raw))
    before = snapshot(tmp_path)
    with pytest.raises(a.ActivationError):
        a.read_activation(tmp_path, "previous")
    assert snapshot(tmp_path) == before  # nothing is repaired


def test_read_activation_refuses_missing_record_and_unsafe_ids(tmp_path):
    with pytest.raises(a.ActivationError, match="cannot read activation"):
        a.read_activation(tmp_path, "absent")
    for bad in ("", "../escape", "a/b", ".hidden", "x" * 97):
        with pytest.raises(a.ActivationError, match="explicit simple activation id"):
            a.read_activation(tmp_path, bad)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("redirect", ["state", "activation"])
def test_read_activation_refuses_redirected_state(tmp_path, redirect):
    root, foreign = tmp_path / "root", tmp_path / "foreign"
    root.mkdir()
    write_activation(foreign, "previous")
    if redirect == "state":
        (root / "state").symlink_to(foreign / "state", target_is_directory=True)
    else:
        (root / "state/activations").mkdir(parents=True)
        (root / "state/activations/previous").symlink_to(
            foreign / "state/activations/previous", target_is_directory=True
        )
    with pytest.raises(a.ActivationError, match="redirected"):
        a.read_activation(root, "previous")


def test_read_selection_distinguishes_absent_valid_and_invalid(tmp_path):
    assert a.read_selection(tmp_path) is None
    assert not (tmp_path / "state").exists()
    path, value = write_selection(tmp_path, "previous")
    before = snapshot(tmp_path)
    assert a.read_selection(tmp_path) == value
    assert snapshot(tmp_path) == before
    for bad in (
        "not json",
        json.dumps([value]),
        json.dumps({**value, "extra": 1}),
        json.dumps({**value, "schema": 2}),
        json.dumps({k: v for k, v in value.items() if k != "plan_id"}),
    ):
        path.write_text(bad)
        with pytest.raises(a.ActivationError):
            a.read_selection(tmp_path)
        assert path.read_text() == bad


def test_read_selection_refuses_a_redirected_pointer(tmp_path):
    foreign = tmp_path / "foreign.json"
    _, value = write_selection(tmp_path / "other", "previous")
    foreign.write_text(json.dumps(value))
    (tmp_path / "state").mkdir()
    (tmp_path / "state/selected-release.json").symlink_to(foreign)
    with pytest.raises(a.ActivationError, match="redirected"):
        a.read_selection(tmp_path)
