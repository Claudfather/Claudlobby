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

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main
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


# --- fix wave A group 2: --fleet naming the root manifest's own fleet.name ---
# resolves to root mode instead of refusing. Every leaf manager's composed
# check-in doors write `claudlobby --fleet "$FLEET_NAME" <verb>`
# unconditionally, and a root-mode install (fleet.yaml at the repo root,
# documentation/getting-started.md's own first path) has no local/<fleet>/
# overlay for FLEET_NAME to resolve to — so those doors named the root
# fleet and still refused, before this fallback. Covers both refusal sites:
# Paths.detect() and the explicit --root twin in commands/_helpers.py.


def test_detect_falls_back_to_root_mode_when_fleet_names_root_manifest(
    tmp_path: Path,
):
    """A root install resolves --fleet <the root manifest's own fleet.name>
    to root mode -- exactly the Paths a bare call gets -- instead of
    refusing."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: solo\n")

    paths = Paths.detect(hint=root, fleet="solo")

    assert paths.fleet_dir is None
    assert paths.fleet_yaml == root / "fleet.yaml"
    assert paths.root == root.resolve()


def test_detect_still_refuses_a_different_fleet_name(tmp_path: Path):
    """The fallback is name-exact: a --fleet naming neither an overlay NOR
    the root manifest's own fleet still refuses exactly as before."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: solo\n")

    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(hint=root, fleet="other")


def test_overlay_wins_when_root_manifest_also_names_the_fleet(tmp_path: Path):
    """An overlay for NAME still wins over the root-mode fallback even when
    the root manifest ALSO happens to declare fleet.name == NAME --
    overlay-first precedence is unchanged."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: shared\n")
    overlay = root / "local" / "shared"
    overlay.mkdir(parents=True)
    (overlay / "fleet.yaml").write_text("fleet:\n  name: shared\n")

    paths = Paths.detect(hint=root, fleet="shared")

    assert paths.fleet_dir == overlay


def test_root_manifest_malformed_yaml_still_refuses(tmp_path: Path):
    """A root fleet.yaml that fails to parse must never raise a YAML error
    out of the fallback check -- the existing overlay-not-found refusal
    stands, never a traceback."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet: [this is not a mapping\n")

    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(hint=root, fleet="solo")


def test_root_manifest_without_fleet_name_still_refuses(tmp_path: Path):
    """Valid YAML with no fleet.name (and separately, no top-level fleet:
    key at all) also answers 'no' rather than raising."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  service_prefix: com.test\n")

    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(hint=root, fleet="solo")

    (root / "fleet.yaml").write_text("not_fleet: true\n")

    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(hint=root, fleet="solo")


def test_detect_root_fallback_logs_an_info_line(tmp_path: Path, caplog):
    """The fallback is disclosed, not silent -- an INFO line names the fleet
    and says the call is running in root mode."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: solo\n")

    with caplog.at_level(logging.INFO):
        Paths.detect(hint=root, fleet="solo")

    assert "solo" in caplog.text
    assert "root mode" in caplog.text


def test_resolve_paths_root_flag_falls_back_to_root_mode(tmp_path: Path):
    """The explicit --root twin (_resolve_paths, which never calls
    Paths.detect) gets the same fallback."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: solo\n")
    args = SimpleNamespace(fleet="solo", seed=False, root=str(root))

    paths = _resolve_paths(args)

    assert paths.fleet_dir is None
    assert paths.fleet_yaml == root / "fleet.yaml"


def test_resolve_paths_root_flag_still_refuses_a_different_fleet_name(
    tmp_path: Path,
):
    """The --root twin's refusal is unchanged in exit code and message for a
    name that is neither an overlay nor the root manifest's own name."""
    root = _make_root(tmp_path)
    (root / "fleet.yaml").write_text("fleet:\n  name: solo\n")
    args = SimpleNamespace(fleet="other", seed=False, root=str(root))

    with pytest.raises(SystemExit) as exc:
        _resolve_paths(args)

    assert exc.value.code == 1


def test_end_to_end_root_flag_own_name_runs_like_bare_call(fleet_dir, monkeypatch):
    """CLI-level proof: --root <root> --fleet <root's own fleet.name> validate
    exits exactly like --root <root> validate (no --fleet) -- the fleet_dir
    fixture is itself a root-mode install (fleet.yaml at its own root, no
    local/<fleet>/ overlay) named 'test-fleet'."""
    _prime_tokens(monkeypatch)

    bare_rc = main(["--root", str(fleet_dir), "validate"])
    named_rc = main(["--root", str(fleet_dir), "--fleet", "test-fleet", "validate"])

    assert bare_rc == 0
    assert named_rc == 0
