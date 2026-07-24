"""B4 gate — fleets resolve at flat OR nested depth (Claudlobby#602 P2 slice 1).

The recursive-containment vault may nest fleets one level under a ``<system>/``
container:

    flat    local/<fleet>/fleet.yaml
    nested  local/<system>/<fleet>/fleet.yaml   (the container has no fleet.yaml)

Backwards-compat (F4) is THE invariant: a marker-less flat overlay resolves
byte-identically to the pre-nesting behavior; nesting is opt-in. This module
pins BOTH shapes — the nested assertions are the red that drives the feature,
the flat assertions guard against regression.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.commands._helpers import _resolve_paths
from claudlobby.config import load_fleet
from claudlobby.paths import Paths, _find_fleet_dir
from claudlobby.validator import validate


def _make_root(base: Path) -> Path:
    """A claudlobby root carrying the detection markers (library/ + lib/)."""
    root = base / "claudlobby"
    (root / "library").mkdir(parents=True)
    (root / "lib").mkdir()
    return root


# --- (a) + (b): single-fleet name -> dir resolution + derived properties ---


def test_nested_fleet_resolves_one_level_under_a_system_container(tmp_path: Path):
    """(a) a nested name resolves to local/<system>/<fleet>, and (b) every
    fleet-relative derived path hangs off that nested dir."""
    root = _make_root(tmp_path)
    nested = root / "local" / "sys1" / "fleetA"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: fleetA\n")

    paths = Paths.detect(hint=root, fleet="fleetA")

    assert paths.fleet_dir == nested
    assert paths.fleet_yaml == nested / "fleet.yaml"
    assert paths.runtime == nested / "runtime"
    assert paths.shared_docs == nested / "shared"
    assert paths.bot_runtime("botA") == nested / "runtime" / "bots" / "botA"


def test_flat_fleet_resolves_at_depth_one(tmp_path: Path):
    """The flat shape resolves to local/<fleet> with matching derived paths."""
    root = _make_root(tmp_path)
    flat = root / "local" / "fleetX"
    flat.mkdir(parents=True)
    (flat / "fleet.yaml").write_text("fleet:\n  name: fleetX\n")

    paths = Paths.detect(hint=root, fleet="fleetX")

    assert paths.fleet_dir == flat
    assert paths.fleet_yaml == flat / "fleet.yaml"
    assert paths.runtime == flat / "runtime"
    assert paths.shared_docs == flat / "shared"
    assert paths.bot_runtime("botX") == flat / "runtime" / "bots" / "botX"


# --- (d): flat behavior byte-identical (the backwards-compat invariant) ---


def test_flat_bare_dir_without_fleet_yaml_still_resolves(tmp_path: Path):
    """A flat overlay dir with no fleet.yaml resolves exactly as before —
    scaffolding and the existing suite depend on this byte-identical corner."""
    root = _make_root(tmp_path)
    bare = root / "local" / "bare-fleet"
    bare.mkdir(parents=True)

    paths = Paths.detect(hint=root, fleet="bare-fleet")

    assert paths.fleet_dir == bare


def test_unknown_fleet_still_raises_filenotfound(tmp_path: Path):
    """A name present at neither depth still raises the same FileNotFoundError."""
    root = _make_root(tmp_path)
    (root / "local").mkdir()

    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(hint=root, fleet="ghost")


# --- (c): the cross-fleet collision scan sees a NESTED sibling fleet ---


def _prime_tokens(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
    monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")


def test_collision_scan_sees_a_nested_fleet(fleet_dir, monkeypatch):
    """A colliding bot in a NESTED sibling fleet must be flagged — today the
    depth-1-only scan is blind to it (this is the red driver)."""
    _prime_tokens(monkeypatch)

    # Current fleet: a flat overlay.
    my_fleet = fleet_dir / "local" / "my-fleet"
    my_fleet.mkdir(parents=True)
    (my_fleet / "fleet.yaml").write_text((fleet_dir / "fleet.yaml").read_text())

    # A NESTED sibling fleet under a system container, colliding bot 'lead'.
    other = fleet_dir / "local" / "sys1" / "other-fleet"
    other.mkdir(parents=True)
    (other / "fleet.yaml").write_text("fleet:\n  name: other-fleet\n")
    other_bots = other / "runtime" / "bots" / "lead"
    other_bots.mkdir(parents=True)
    (other_bots / "bot.conf").write_text("BOT_NAME=lead\n")

    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    report = validate(fleet, Paths(root=fleet_dir, fleet_dir=my_fleet))

    assert any(
        "lead" in w and "other-fleet" in w and "collide" in w
        for w in report.warnings
    )


def test_collision_scan_flat_sibling_unchanged(fleet_dir, monkeypatch):
    """The flat-sibling collision path is unchanged (regression guard)."""
    _prime_tokens(monkeypatch)

    my_fleet = fleet_dir / "local" / "my-fleet"
    my_fleet.mkdir(parents=True)
    (my_fleet / "fleet.yaml").write_text((fleet_dir / "fleet.yaml").read_text())

    other_bots = fleet_dir / "local" / "other-fleet" / "runtime" / "bots" / "lead"
    other_bots.mkdir(parents=True)
    (other_bots / "bot.conf").write_text("BOT_NAME=lead\n")

    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    report = validate(fleet, Paths(root=fleet_dir, fleet_dir=my_fleet))

    assert any(
        "lead" in w and "other-fleet" in w and "collide" in w
        for w in report.warnings
    )


# --- F5: a name that resolves at BOTH depths is a global-unique violation ---


def test_name_at_both_depths_raises_global_unique_violation(tmp_path: Path):
    """A name present flat AND nested must fail loudly, not silently pick one."""
    root = _make_root(tmp_path)
    flat = root / "local" / "dup"
    flat.mkdir(parents=True)
    (flat / "fleet.yaml").write_text("fleet:\n  name: dup\n")
    nested = root / "local" / "sys1" / "dup"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: dup\n")

    with pytest.raises(ValueError, match="globally unique"):
        Paths.detect(hint=root, fleet="dup")


# --- F5 husk tolerance: a bare flat husk must yield to a real nested fleet ---


def test_husk_flat_dir_yields_to_nested_fleet(tmp_path: Path):
    """A bare flat husk (leftover gitignored ``runtime/``, NO fleet.yaml) must
    YIELD to a real nested fleet of the same name — not falsely raise F5.

    Reproduces the git-mv migration trap: ``git mv local/foo local/sys1/foo``
    moves only tracked files, leaving the gitignored ``local/foo/runtime/``
    behind as a bare husk. Pre-fix that husk (no fleet.yaml) still tripped the
    both-depths raise, bricking every ``--fleet foo`` command mid-migration.
    """
    local = tmp_path / "local"
    husk = local / "foo"
    (husk / "runtime").mkdir(parents=True)  # bare husk — NO fleet.yaml
    nested = local / "sys1" / "foo"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: foo\n")

    assert _find_fleet_dir(local, "foo") == nested


def test_name_under_two_systems_raises_global_unique_violation(tmp_path: Path):
    """The same fleet name nested under TWO system containers is a genuine F5
    collision — raise, never silently pick one. No flat arm here: this pins the
    two-systems branch the both-depths test never exercises.
    """
    local = tmp_path / "local"
    s1 = local / "s1" / "foo"
    s1.mkdir(parents=True)
    (s1 / "fleet.yaml").write_text("fleet:\n  name: foo\n")
    s2 = local / "s2" / "foo"
    s2.mkdir(parents=True)
    (s2 / "fleet.yaml").write_text("fleet:\n  name: foo\n")

    with pytest.raises(ValueError, match="multiple systems"):
        _find_fleet_dir(local, "foo")


# --- the default CLI path guards _find_fleet_dir's ValueError (not a traceback) ---


def test_default_path_surfaces_f5_as_clean_exit(tmp_path: Path, monkeypatch):
    """A genuine F5 collision on the DEFAULT ``claudlobby --fleet <dup>`` path
    (no --root, so ``_resolve_paths`` → ``Paths.detect``) must exit(1) via
    log.error — the twin of the --root guard at _helpers.py:30-34 — not throw a
    raw ValueError traceback at the user.
    """
    root = _make_root(tmp_path)
    flat = root / "local" / "dup"
    flat.mkdir(parents=True)
    (flat / "fleet.yaml").write_text("fleet:\n  name: dup\n")
    nested = root / "local" / "sys1" / "dup"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: dup\n")

    # Default branch: no --root, so Paths.detect() resolves the root via env.
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    args = SimpleNamespace(fleet="dup", seed=False, root=None)

    with pytest.raises(SystemExit) as exc:
        _resolve_paths(args)
    assert exc.value.code == 1
