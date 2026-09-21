"""Red first: the shipped default is `full`, and every opt-out path still works."""
from pathlib import Path
import importlib
ea = importlib.import_module("claudlobby.plane.emit_api")

def test_absent_config_defaults_to_full(tmp_path):
    assert ea.capture_mode(ea.load_capture_config(tmp_path), "any-fleet") == "full"

def test_star_metadata_opts_a_host_out(tmp_path):
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "metadata"}')
    m = ea.load_capture_config(tmp_path)
    assert ea.capture_mode(m, "a") == "metadata" and ea.capture_mode(m, "b") == "metadata"

def test_a_named_fleet_overrides_the_star(tmp_path):
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full", "quiet": "metadata"}')
    m = ea.load_capture_config(tmp_path)
    assert ea.capture_mode(m, "quiet") == "metadata" and ea.capture_mode(m, "loud") == "full"

def test_a_named_fleet_opts_out_while_the_rest_take_the_default(tmp_path):
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"quiet": "metadata"}')
    m = ea.load_capture_config(tmp_path)
    assert ea.capture_mode(m, "quiet") == "metadata"
    assert ea.capture_mode(m, "other") == "full"   # the shipped default, not the file


# --- the `capture config` rung reports the mode IN FORCE, not a constant ------
#
# It previously printed a fixed string naming the shipped DEFAULT, so it was
# informative exactly when the setting did not matter (unconfigured) and wrong
# exactly when it did. These drive the real `plane doctor` command and assert on
# its output, so a regression to any static string fails whatever that string
# says — asserting the resolved mode is PRESENT is not enough on its own, since
# a constant mentioning both modes would satisfy that.

import subprocess
import sys


def _doctor(root: Path) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root), "plane", "doctor"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={"PATH": "/usr/bin:/bin", "PLANE_EMIT_DISABLED": "1",
             "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    return "\n".join(
        ln for ln in (out.stdout + out.stderr).splitlines() if "capture config" in ln
    )


def _write(root: Path, text: str) -> None:
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (root / "state" / "plane" / "capture.json").write_text(text)


def test_the_rung_names_a_host_wide_opt_out_as_metadata(tmp_path):
    _write(tmp_path, '{"*": "metadata"}')
    line = _doctor(tmp_path)
    assert "metadata" in line, line
    # The discriminator: under the old constant this line said "default: full".
    assert "default: full" not in line, line
    assert "opt-out" in line, line


def test_the_rung_names_an_unconfigured_host_as_the_shipped_default(tmp_path):
    line = _doctor(tmp_path)
    assert "full (shipped default)" in line, line
    # Only the unconfigured host gets told the knob exists — that is the
    # population that has not heard of it.
    assert '{"*": "metadata"}' in line, line


def test_the_rung_names_the_fleets_that_differ_from_the_host_mode(tmp_path):
    _write(tmp_path, '{"*": "full", "quiet": "metadata"}')
    line = _doctor(tmp_path)
    assert "quiet=metadata" in line, line
    assert "1 fleet(s) differ" in line, line


def test_the_rung_is_not_a_constant(tmp_path):
    """The shape regression, pinned directly: two hosts with DIFFERENT
    configured modes must not produce the same rung line."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _write(a, '{"*": "metadata"}')
    _write(b, '{"*": "full"}')
    assert _doctor(a) != _doctor(b), "the rung is reporting a constant again"
