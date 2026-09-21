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
