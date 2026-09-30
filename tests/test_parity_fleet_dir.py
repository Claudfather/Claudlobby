"""Both directory readers and their real consumers see the same valid fleet.

Genuine flat/nested collision refusal remains #1608 work: existing shell
callers conflate absence and ambiguity, so this slice preserves that branch.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from claudlobby.paths import Paths, _find_fleet_dir

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def estate(tmp_path, monkeypatch):
    root, home, bindir = (tmp_path / name for name in ("isolated estate", "home", "bin"))
    for path in (root / "local", root / "lib", home, bindir):
        path.mkdir(parents=True, exist_ok=True)
    for name in ("lib-common.sh", "supervisor.sh", "env-tiers.sh"):
        shutil.copyfile(REPO / "lib" / name, root / "lib" / name)
    # Only source-time detection and the read-only resolver/consumer tools.
    # No inherited PATH, shell startup file, model, network or supervisor CLI.
    for name in ("bash", "uname", "dirname", "awk", "grep", "rm"):
        command = shutil.which(name)
        assert command, f"required test utility unavailable: {name}"
        (bindir / name).symlink_to(command)
    # read_tiers deliberately constructs a child env without TMPDIR. Keep
    # lib-common's source-time scratch directory private on that path too.
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp
    mktemp = bindir / "mktemp"
    mktemp.write_text(
        f'#!/bin/bash\nTMPDIR={shlex.quote(str(tmp_path))} '
        f'exec {shlex.quote(real_mktemp)} "$@"\n'
    )
    mktemp.chmod(0o755)
    env = {
        "PATH": str(bindir), "HOME": str(home), "TMPDIR": str(tmp_path),
        "CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "f", "LC_ALL": "C",
        "PLANE_EMIT_DISABLED": "1", "PLANE_SOCKET": str(root / "absent.sock"),
        "TELEGRAM_STATE_DIR": str(home / "channel"), "TMUX_BIN": "/usr/bin/false",
    }
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bindir))
    yield root, env
    assert not (root / "state").exists()
    assert not (home / "channel").exists()
    assert not (root / "absent.sock").exists()


def _fleet(root, relative):
    path = root / "local" / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / "fleet.yaml").write_text("fleet:\n  name: fixture\n")
    return path


def _bash(estate, command, *args):
    root, env = estate
    return subprocess.run(
        ["/bin/bash", "-c", '. "$1"; shift; ' + command, "_",
         str(root / "lib" / "lib-common.sh"), *args],
        env=env, cwd=root, capture_output=True, text=True, timeout=5,
    )


@pytest.mark.parametrize("bare,marked,expected", [
    ([], [], None),
    ([], ["f"], "f"),
    (["f/runtime"], [], "f"),
    ([], ["system/f"], "system/f"),
    (["f/runtime"], ["system/f"], "system/f"),
    ([], ["other", "other/f"], None),
    (["f/runtime"], ["other", "other/f"], "f"),
    ([], ["other", "other/f", "system/f"], "system/f"),
    ([], ["system/f", "second/f"], "ambiguous"),
], ids=["absent", "flat", "bare-scaffold", "nested", "husk-yields",
        "fleet-is-not-container", "husk-only-valid-choice", "ignore-false-competitor",
        "multiple-nested"])
def test_unambiguous_directory_readers_agree(estate, bare, marked, expected):
    root, _ = estate
    for path in bare:
        (root / "local" / path).mkdir(parents=True)
    for path in marked:
        _fleet(root, path)
    shell = _bash(estate, 'resolve_fleet_dir "$1"', "f")
    if expected == "ambiguous":
        with pytest.raises(ValueError, match="multiple systems"):
            _find_fleet_dir(root / "local", "f")
        assert shell.returncode == 1 and shell.stdout == ""
    else:
        wanted = root / "local" / expected if expected else None
        assert _find_fleet_dir(root / "local", "f") == wanted
        assert shell.returncode == (0 if wanted else 1), shell.stderr
        assert shell.stdout == (f"{wanted}\n" if wanted else "")
    assert shell.stderr == ""


def test_genuine_flat_nested_collision_keeps_existing_shell_behavior(estate):
    """Scope control, not parity: caller ambiguity propagation is still open."""
    root, _ = estate
    flat = _fleet(root, "f")
    _fleet(root, "system/f")
    with pytest.raises(ValueError, match="two depths"):
        _find_fleet_dir(root / "local", "f")
    shell = _bash(estate, 'resolve_fleet_dir "$1"', "f")
    assert shell.returncode == 0 and shell.stdout == f"{flat}\n"
    assert shell.stderr == ""


@pytest.mark.parametrize("consumer", ["resolve_bots_dir", "fleet_runtime_dir", "env_tier_rows", "source_env_tiered"])
def test_real_consumers_use_nested_fleet_instead_of_runtime_husk(estate, consumer):
    root, _ = estate
    husk = root / "local" / "f"
    (husk / "runtime").mkdir(parents=True)
    # Different synthetic values make a stale path observable without a real
    # credential, generated artifact, service, channel or database.
    (husk / ".env").write_text("PARITY_TOKEN=wrong-husk\n")
    nested = _fleet(root, "system/f")
    (nested / ".env").write_text("PARITY_TOKEN=nested-value\n")
    if consumer == "source_env_tiered":
        shell = _bash(estate, 'source_env_tiered; printf "%s" "${PARITY_TOKEN-<unset>}"')
        assert shell.returncode == 0 and shell.stdout == "nested-value", shell.stderr
    elif consumer == "env_tier_rows":
        shell = _bash(estate, 'env_tier_rows "" f')
        assert shell.returncode == 0, shell.stderr
        rows = {line.split("\t")[0]: line.split("\t")[1:] for line in shell.stdout.splitlines()}
        assert rows["fleet"] == [str(nested / ".env"), "present"]
        # The shipped Python resolver asks this same shell door under its own
        # constructed environment; ensure its consumer also identifies it.
        tiers = Paths(root=root, fleet_dir=nested).env_tiers()
        assert next(row.path for row in tiers if row.tier == "fleet") == nested / ".env"
    else:
        shell = _bash(estate, '"$1" f', consumer)
        expected = nested / "runtime" / "bots" if consumer == "resolve_bots_dir" else nested / "runtime"
        assert shell.returncode == 0 and shell.stdout == str(expected), shell.stderr
    assert shell.stderr == ""
