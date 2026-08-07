"""fleet-state prune: an audit verb must not write, and its message must not lie (#892).

``state/fleet-state.json`` is ONE host-shared file, while the prune builds its
keep-list from a SINGLE fleet's manifest — so every bot outside the invoking
fleet is undeclared *by construction* and gets deleted on a perfect parse. That
fired at least seven times across three managers in one day, on ordinary
session-start hygiene, and nobody caught it: ``reconcile-fleet.sh`` without
``--enroll`` is named, documented and flagged as report-only, and the output
line read as routine housekeeping.

Two properties are pinned here, and they fail differently:

- **The audit path writes nothing.** Not "deletes fewer rows" — writes nothing.
  Scoping which rows a report-only verb may delete concedes that it deletes.
- **The message discloses what is MISSING, not merely what THIS run removed.**
  A run that deletes 2 rows because 15 were already gone prints a small number,
  which reads as reassuring and is the exact opposite of the truth. That
  discrepancy is what made the first incident count wrong.

Plus the load-bearing guard the wipe needed: zero extraction refuses rather than
matching nothing and deleting everything.

CI runs pytest only, so the bash is exercised via subprocess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"
UPDATER = REPO_ROOT / "lib" / "fleet-state-update.sh"
RECONCILE = REPO_ROOT / "lib" / "reconcile-fleet.sh"

SEED_ROWS = {
    "a1": {"status": "idle"},
    "a2": {"status": "idle"},
    "b1": {"status": "working"},
    "g1": {"status": "idle"},
    "orphan": {"status": "idle"},
}


def _host(
    tmp_path: Path,
    *,
    alpha_bots: str = "    a1:\n      expertise: [x]\n    a2:\n      expertise: [x]\n",
) -> Path:
    """Build a fake host root: two flat fleets plus one NESTED under a container."""
    (tmp_path / "local" / "f-alpha").mkdir(parents=True)
    (tmp_path / "local" / "sys" / "f-beta").mkdir(parents=True)
    (tmp_path / "local" / "f-gamma").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "local" / "f-alpha" / "fleet.yaml").write_text(
        "fleet:\n  name: f-alpha\n  bots:\n" + alpha_bots
    )
    (tmp_path / "local" / "sys" / "f-beta" / "fleet.yaml").write_text(
        "fleet:\n  name: f-beta\n  bots:\n    b1:\n      expertise: [x]\n"
    )
    (tmp_path / "local" / "f-gamma" / "fleet.yaml").write_text(
        "fleet:\n  name: f-gamma\n  bots:\n    g1:\n      expertise: [x]\n"
    )
    return tmp_path


def _seed_state(root: Path, rows: dict | None = None) -> Path:
    state = root / "state" / "fleet-state.json"
    state.write_text(
        json.dumps(
            {
                "updated": "x",
                "bots": rows if rows is not None else SEED_ROWS,
                "queue": [],
            }
        )
    )
    return state


def _prune(
    root: Path, *args: str, yaml: str = "local/f-alpha/fleet.yaml"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(UPDATER), "prune", str(root / yaml), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(root),
            "CLAUDLOBBY_ROOT": str(root),
            "FLEET_STATE_PATH": str(root / "state" / "fleet-state.json"),
        },
    )


# --- the host-wide fleet enumeration the message needs -----------------------


def test_discover_fleet_manifests_finds_flat_and_nested(tmp_path: Path) -> None:
    """Attribution is a HOST-wide question; a flat-only enumeration under-reports."""
    root = _host(tmp_path)
    proc = subprocess.run(
        ["bash", "-c", f'. "{LIB_COMMON}"; discover_fleet_manifests'],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "CLAUDLOBBY_ROOT": str(root)},
    )
    found = {line.split("\t")[0] for line in proc.stdout.strip().splitlines() if line}
    assert found == {"f-alpha", "f-beta", "f-gamma"}, proc.stdout


# --- the audit path writes NOTHING -------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _host(tmp_path)
    state = _seed_state(root)
    before = state.read_bytes()
    proc = _prune(root, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert state.read_bytes() == before, "audit path mutated host-shared state"
    assert "WOULD prune" in proc.stdout
    assert "nothing was written" in proc.stdout


def test_reconcile_without_enroll_uses_dry_run(tmp_path: Path) -> None:
    """Structural: the audit branch passes --dry-run, the enroll branch does not.

    Named as structural deliberately — it pins the wiring, not the runtime
    behaviour of a full reconcile (which probes systemd and tmux). The
    end-to-end proof is the empirical gate cited in the PR.
    """
    src = RECONCILE.read_text()
    assert 'prune "$FLEET_YAML" --dry-run' in src
    assert '"$ENROLL" = "--enroll"' in src
    # The unconditional call this issue is about must be gone.
    unconditional = [
        ln
        for ln in src.splitlines()
        if 'prune "$FLEET_YAML"' in ln and "--dry-run" not in ln
    ]
    assert len(unconditional) == 1, "expected exactly one (guarded) write call"


# --- the message must not read as routine housekeeping -----------------------


def test_message_names_the_fleet_each_row_belongs_to(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _seed_state(root)
    out = _prune(root, "--dry-run").stdout
    assert "f-beta (NOT this fleet" in out
    assert "f-gamma (NOT this fleet" in out
    assert "declared by no fleet on this host" in out and "orphan" in out
    assert "belong to OTHER fleets" in out


def test_message_reports_what_is_missing_not_only_what_this_run_removed(
    tmp_path: Path,
) -> None:
    """The core disclosure bug: a small number must not read as reassuring.

    Only ``orphan`` is left to remove because the sibling rows were already
    wiped by an earlier run — but three of the four host-declared bots hold no
    row, and the message has to say so.
    """
    root = _host(tmp_path)
    _seed_state(root, {"a1": {"status": "idle"}, "orphan": {"status": "idle"}})
    out = _prune(root).stdout
    assert "Pruned 1 row(s)" in out
    assert "3 of 4 host-declared bots have NO row" in out, out


# --- the write path still works, and stamps ----------------------------------


def test_real_prune_removes_only_undeclared_and_stamps_updated(tmp_path: Path) -> None:
    root = _host(tmp_path)
    state = _seed_state(root)
    proc = _prune(root)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(state.read_text())
    assert sorted(data["bots"]) == ["a1", "a2"]
    assert data["updated"] != "x", "prune must stamp .updated like delete/update do"


# --- the load-bearing guard --------------------------------------------------


def test_zero_extraction_refuses_and_touches_nothing(tmp_path: Path) -> None:
    """A trailing comment on ``bots:`` is PyYAML-valid and extracts zero names.

    Without the bail the empty keep-set matches no key and every row on the
    host-shared file is deleted.
    """
    root = _host(tmp_path, alpha_bots="    a1:\n      expertise: [x]\n")
    (root / "local" / "f-alpha" / "drift.yaml").write_text(
        "fleet:\n  name: f-alpha\n  bots:  # my bots\n    a1:\n      expertise: [x]\n"
    )
    state = _seed_state(root)
    before = state.read_bytes()
    proc = _prune(root, yaml="local/f-alpha/drift.yaml")
    assert proc.returncode != 0
    assert "refusing to prune" in proc.stderr
    assert state.read_bytes() == before, "zero-extraction wiped the state file"
