"""Stale CLI and interrupted activation refuse before a mutation starts."""

from pathlib import Path

import pytest

from claudlobby import activation_state as a
from claudlobby.releases import read_release
from claudlobby.runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
from tests.test_activation_state import _advance, _prepare
from tests.test_config_plan import proposal
from tests.test_releases import installed


def _active(plan):
    with a.locked_activation(plan.data_root) as store:
        _prepare(store, plan)
        _advance(store, "candidate", a.STEPS[:a.STEPS.index("selection_switched")])
        store.begin("candidate", "selection_switched")
        store.select("candidate")
        _advance(store, "candidate", a.STEPS[a.STEPS.index("selection_switched") + 1:])


def _identity(builder):
    release = read_release(builder.root, builder.release_id)
    return RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)


def test_stale_context_and_executable_refuse_before_effect(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    _active(plan)
    identity = _identity(builder)
    with pytest.raises(ReleaseMismatch, match="no mutation performed") as err:
        with mutation_admission(builder.root, identity=identity, expected_release="r-stale"):
            pytest.fail("stale bot configuration admitted")
    assert err.value.exit_code == 7 and str(builder.root) in err.value.hint
    with pytest.raises(ReleaseMismatch):
        with mutation_admission(builder.root, identity=RuntimeIdentity(
                Path("/stale/bin/claudlobby"), identity.native, identity.artifact_id)):
            pytest.fail("same artifact from wrong executable admitted")
    with mutation_admission(builder.root, identity=identity) as admitted:
        assert admitted.release_id == plan.release_id
        with pytest.raises(a.ActivationError, match="holds the lock"):
            with a.locked_activation(builder.root):
                pytest.fail("activation raced an admitted operation")


def test_incomplete_activation_blocks_even_before_selection_changes(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    _active(plan)
    identity = _identity(builder)
    with a.locked_activation(builder.root) as store:
        store.assert_locked()
        with pytest.raises(a.ActivationError, match="activation is running"):
            with mutation_admission(builder.root, identity=identity):
                pytest.fail("exclusive cutover lock ignored")
        _prepare(store, plan, "next")
    with pytest.raises(a.ActivationError, match="unfinished activation next"):
        with mutation_admission(builder.root, identity=identity):
            pytest.fail("old selected release admitted during interrupted preparation")
    with pytest.raises(a.ActivationError, match="lock is not held"):
        store.assert_locked()


def test_missing_host_state_never_creates_an_install(tmp_path):
    root = tmp_path / "not-an-install"
    with pytest.raises(a.ActivationError, match="lock unavailable"):
        with mutation_admission(root):
            pytest.fail("missing install admitted")
    assert not root.exists()
