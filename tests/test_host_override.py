"""THIS host's override of host.jobs: ~/.config/claudlobby/system.yaml (#1251).

Host-local config lives outside the tracked tree, so a root pull can refuse any
dirty tree. Each test is named for the failure it guards against.
"""
import copy
from pathlib import Path

import pytest

from claudlobby import composer as composer_mod
from claudlobby.config import _load_system_defaults, load_host_jobs
from claudlobby import switches as sw
from claudlobby.paths import Paths

PAUSE = "host: { jobs: { claude-update: { enroll: false } } }\n"


@pytest.fixture
def override(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "host-override.yaml"
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(path))
    return path


def _units(root: Path, job: str) -> list[str]:
    root.mkdir()
    out = composer_mod.compose_host_timers(Paths(root=root))
    return sorted(p.name for p in out.iterdir() if p.name.startswith(f"claudlobby-{job}."))


def test_the_claude_update_pause_needs_no_edit_to_the_tracked_system_yaml(tmp_path, override):
    # The pause lived in a tracked edit to claudlobby/system.yaml, which the
    # 2026-09-26 pull had to carry with --autostash. Here the packaged file
    # stays as shipped, and the override alone stops the unit being composed.
    assert _load_system_defaults()["host"]["jobs"]["claude-update"].get("enroll", True) is not False
    assert _units(tmp_path / "shipped", "claude-update"), "precondition: shipped enrolled"
    override.write_text(PAUSE)
    assert load_host_jobs()["claude-update"]["enroll"] is False
    assert _units(tmp_path / "paused", "claude-update") == []


def test_arming_one_job_keeps_every_other_job_and_field_as_packaged(override):
    # A wholesale replacement of host.jobs would keep only the job the override
    # names, silently dropping every other job's schedule and enroll state.
    packaged = copy.deepcopy(_load_system_defaults()["host"]["jobs"])
    override.write_text("host: { jobs: { update-siblings: { enroll: true } } }\n")
    jobs = load_host_jobs()
    assert set(jobs) == set(packaged)
    for name, cfg in packaged.items():
        if name != "update-siblings":
            assert jobs[name] == cfg, name
    assert jobs["update-siblings"] == {**packaged["update-siblings"], "enroll": True}
    # ...and the cached packaged tier is not rewritten by the merge.
    assert _load_system_defaults()["host"]["jobs"] == packaged


@pytest.mark.parametrize("text", [
    'host: { jobs: { claude-update: { enroll: "false" } } }\n',  # the composer enrolls on a string
    "host: { jobs: { claude-update: { enrol: false } } }\n",     # a field nothing reads
], ids=["quoted-enroll", "misspelt-field"])
def test_a_pause_that_cannot_take_effect_is_refused_not_skipped(override, text):
    override.write_text(text)
    with pytest.raises(RuntimeError, match="claude-update"):
        load_host_jobs()


def test_a_job_this_install_does_not_ship_is_logged_not_fatal(override, caplog):
    # The file outlives the install it was written against: a pull that retires
    # a job must not stop every host-timers run on the host.
    override.write_text("host: { jobs: { claude-updte: { enroll: false } } }\n")
    assert "claude-updte" not in load_host_jobs()
    assert "did you mean claude-update" in caplog.text


def test_a_malformed_override_renders_unknown_never_the_shipped_default(tmp_path, override):
    # resolve() once caught the refusal and substituted {}, so doctor --switches
    # showed every host job at its SHIPPED default at rc 0 -- for a paused job,
    # the exact opposite of the host's intended state.
    override.write_text('host: { jobs: { claude-update: { enroll: "false" } } }\n')
    rows = sw.resolve(Paths(root=tmp_path), cascade={})
    host_rows = [r for r in rows if r.switch.scope in (sw.HOST_JOB, sw.HOST_SERVICE)
                 and "extra is not installed" not in r.source]   # that row is its own fact
    assert host_rows
    for r in host_rows:
        assert r.unknown and r.unknown_reason == "host-override", r.switch.key
        assert str(override) in r.detail and "must be true or false" in r.detail
        assert r.label == "unknown"
    assert "host jobs unreadable" in sw.summary_line(rows)
