"""Only the exact current Git configuration admits operational mutations."""

from pathlib import Path
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from claudlobby import activation_state as a
from claudlobby.runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission


@pytest.fixture
def tmp_path(tmp_path_factory):
    # AF_UNIX has a short platform limit; keep owned data paths compact.
    return tmp_path_factory.mktemp("admit")


def test_missing_host_state_never_creates_an_install(tmp_path):
    root = tmp_path / "not-an-install"
    with pytest.raises(a.ActivationError, match="managed Git installation"):
        with mutation_admission(root, identity=RuntimeIdentity(Path('/cli'), Path('/native'), '')):
            pytest.fail("missing install admitted")
    assert not root.exists()


@pytest.fixture
def git_current(tmp_path, monkeypatch):
    from claudlobby import active_config, runtime_admission
    from claudlobby.installation import install_launcher
    from tests.test_package_resources import _source_installation

    installation = _source_installation(tmp_path)
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    revision = installation.preflight()
    snapshot = {"schema": 1, "data_root": str(installation.data_root),
                "installation": str(installation.directory), "source": str(installation.source),
                "revision": revision, "fleets": {"example": {
                    "manifest": str(installation.data_root / "fleet.yaml"),
                    "fleet": 'fleet: {name: example, manager: worker, system_defaults: false, bots: {worker: {expertise: [software-engineering]}}}\n',
                    "projects": None}}, "units": [], "owned_outputs": [], "identities": None}
    snapshot["fingerprint"] = active_config._fingerprint(snapshot)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    monkeypatch.delenv("CLAUDLOBBY_RELEASE_ID", raising=False)
    monkeypatch.delenv("CLAUDLOBBY_ARTIFACT_ID", raising=False)
    # Model execution from the recorded environment/source, while exercising
    # real Git/resource/config readers and the real exclusion file.
    monkeypatch.setattr(sys, "executable", str(installation.environment / "bin/python"))
    monkeypatch.setattr(runtime_admission, "__file__", str(installation.source / "claudlobby/runtime_admission.py"))
    return installation, snapshot


@pytest.mark.parametrize("explicit_identity", [False, True])
def test_git_admission_routes_actual_identity_without_environment_selector(git_current, monkeypatch,
                                                                         explicit_identity):
    from claudlobby import runtime_admission
    installation, snapshot = git_current
    identity = RuntimeIdentity.current()
    monkeypatch.delenv("CLAUDLOBBY_INSTALLATION")
    # Model the already identified executing runtime, not an environment claim.
    monkeypatch.setattr(RuntimeIdentity, "current", lambda: identity)
    (installation.data_root / "state/selected-release.json").write_text("historical unreadable pointer\n")
    options = {"identity": identity} if explicit_identity else {}
    with mutation_admission(installation.data_root, **options) as current:
        assert isinstance(current, runtime_admission.GitCurrentRuntime)
        assert current.installation == installation
        assert current.revision == snapshot["revision"]
        assert current.config_fingerprint == snapshot["fingerprint"]
    assert not (installation.data_root / "state/activation.lock").exists()


def test_git_current_admission_returns_configuration_and_preserves_launcher_lock(git_current):
    from claudlobby.installation import _runtime_lock, native_start_fence, InstallationError
    from claudlobby.runtime_admission import GitCurrentRuntime

    installation, snapshot = git_current
    inherited = _runtime_lock(installation)
    try:
        with mutation_admission(installation.data_root) as admitted:
            assert isinstance(admitted, GitCurrentRuntime)
            assert admitted.installation == installation
            assert admitted.revision == snapshot["revision"]
            assert admitted.config_fingerprint == snapshot["fingerprint"]
            assert admitted.snapshot == snapshot
            assert not hasattr(admitted, "release_id")
            with pytest.raises(InstallationError):
                with native_start_fence(installation):
                    pytest.fail("exclusive update raced admission")
        # Admission closes only its own SH, not the stable launcher's SH.
        with pytest.raises(InstallationError):
            with native_start_fence(installation):
                pytest.fail("launcher lifetime lock was released")
    finally:
        os.close(inherited)
    with native_start_fence(installation):
        with pytest.raises(a.ActivationError, match="runtime is in use"):
            with mutation_admission(installation.data_root):
                pytest.fail("exclusive update admitted a mutation")
    with native_start_fence(installation):
        pass
    assert not (installation.data_root / "state/activation.lock").exists()


@pytest.mark.parametrize("change", ["maintenance", "fingerprint", "revision", "identity", "source", "interpreter", "release"])
def test_git_current_admission_refuses_drift_before_effect(git_current, monkeypatch, change):
    installation, snapshot = git_current
    identity = RuntimeIdentity.current()
    expected = None
    if change == "maintenance":
        (installation.data_root / "state/update.json").write_text(
            json.dumps({"schema": 1, "maintenance": True, "phase": "git"}))
    elif change in {"fingerprint", "revision"}:
        snapshot[change] = "0" * (64 if change == "fingerprint" else 40)
        if change == "revision":
            from claudlobby.active_config import _fingerprint
            snapshot["fingerprint"] = _fingerprint(snapshot)
        (installation.data_root / "state/config.json").write_text(json.dumps(snapshot))
    elif change == "identity":
        identity = replace(identity, native=installation.native / "foreign")
    elif change == "source":
        from claudlobby import runtime_admission
        monkeypatch.setattr(runtime_admission, "__file__", str(installation.data_root / "foreign.py"))
    elif change == "interpreter":
        monkeypatch.setattr(sys, "executable", str(installation.data_root / "foreign-python"))
    else:
        expected = "r-" + "1" * 64
    with pytest.raises(a.ActivationError):
        with mutation_admission(installation.data_root, identity=identity, expected_release=expected):
            pytest.fail("changed Git runtime admitted")
    # Refusal must not provision or repair sealed state.
    assert not (installation.data_root / "state/activation.lock").exists()
    assert not (installation.data_root / "state/selection.json").exists()


def test_shared_git_route_comparison_binds_configuration_and_installation(git_current):
    from types import SimpleNamespace
    from claudlobby.resources import get_resources
    from claudlobby.runtime_admission import GitCurrentRuntime, runtime_route_matches, runtime_provenance
    installation, snapshot = git_current
    package = get_resources(installation=installation)
    current = GitCurrentRuntime(installation, snapshot["revision"], snapshot["fingerprint"], snapshot)
    route = SimpleNamespace(release_id=None, activation_id=None, plan_id=None,
                            revision=current.revision, config_fingerprint=current.config_fingerprint,
                            selected=SimpleNamespace(paths=SimpleNamespace(package=package)))
    assert runtime_route_matches(current, route)
    assert runtime_provenance(current) == {"source_revision": current.revision,
                                           "config_fingerprint": current.config_fingerprint}
    route.config_fingerprint = "b" * 64
    assert not runtime_route_matches(current, route)
    route.config_fingerprint = current.config_fingerprint
    route.release_id = "r-" + "c" * 64
    assert not runtime_route_matches(current, route)
    route.release_id = None
    from claudlobby.installation import Installation
    other = replace(package, installation=Installation(installation.directory.parent / "other", installation.data_root))
    assert not runtime_route_matches(current, route, package=other)


@pytest.mark.parametrize('marker', ['CLAUDLOBBY_RELEASE_ID', 'CLAUDLOBBY_ARTIFACT_ID'])
def test_git_admission_refuses_legacy_identity_markers_before_effect(git_current, monkeypatch, marker):
    installation, _ = git_current
    monkeypatch.setenv(marker, 'historical-identity')
    with pytest.raises(a.ActivationError, match='sealed release expectation'):
        with mutation_admission(installation.data_root):
            pytest.fail('mixed runtime admitted')


def test_unit_wrapper_refuses_unmanaged_or_sealed_generation_before_execution():
    from claudlobby.runtime_admission import wrap_unit_argv
    for environment in ({}, {'CLAUDLOBBY_RELEASE_ID': 'r-' + 'a' * 64}):
        with pytest.raises(a.ActivationError, match='managed Git installation'):
            wrap_unit_argv(environment, unit='example', phase='bots', mode='exec', argv=('/bin/true',))


def test_removed_native_module_entry_never_executes_child(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'claudlobby.runtime_admission', 'unit-start',
                             str(tmp_path / 'child-marker')], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'stable managed Git' in result.stderr
    assert not (tmp_path / 'child-marker').exists()
