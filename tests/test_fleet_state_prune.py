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

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"
UPDATER = REPO_ROOT / "lib" / "fleet-state-update.sh"
RECONCILE = REPO_ROOT / "lib" / "reconcile-fleet.sh"

# Attribution matters now: prune is SCOPED, so a row's owning fleet decides
# whether this fleet may remove it. "a0" is f-alpha's own departed bot — the one
# row an f-alpha prune is entitled to reap. "orphan" carries no attribution at
# all (the shape every row on a live host had before stamping existed) and is
# therefore protected, not guessed at.
SEED_ROWS = {
    "a1": {"status": "idle", "fleet": "f-alpha"},
    "a2": {"status": "idle", "fleet": "f-alpha"},
    "a0": {"status": "idle", "fleet": "f-alpha"},
    "b1": {"status": "working", "fleet": "f-beta"},
    "g1": {"status": "idle", "fleet": "f-gamma"},
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
    # These rows used to be listed as things prune was ABOUT TO DELETE. They are
    # now listed as rows it will not touch — the disclosure survives the scoping
    # fix, which is the point of keeping this test rather than deleting it.
    assert "b1 — declared by another fleet on this host" in out, out
    assert "g1 — declared by another fleet on this host" in out, out
    assert "orphan — no fleet attribution" in out, out


def test_message_reports_what_is_missing_not_only_what_this_run_removed(
    tmp_path: Path,
) -> None:
    """The core disclosure bug: a small number must not read as reassuring.

    Only ``orphan`` is left to remove because the sibling rows were already
    wiped by an earlier run — but three of the four host-declared bots hold no
    row, and the message has to say so.
    """
    root = _host(tmp_path)
    _seed_state(
        root,
        {
            "a1": {"status": "idle", "fleet": "f-alpha"},
            "a0": {"status": "idle", "fleet": "f-alpha"},
        },
    )
    out = _prune(root).stdout
    assert "Pruned 1 row(s)" in out
    assert "3 of 4 host-declared bots have NO row" in out, out


# --- the write path still works, and stamps ----------------------------------


def test_real_prune_removes_only_undeclared_and_stamps_updated(tmp_path: Path) -> None:
    """The write path still reaps — but only what this fleet owns.

    This assertion used to read ``== ["a1", "a2"]``, i.e. it required f-alpha's
    prune to DELETE f-beta's and f-gamma's rows. That is the incident, pinned as
    the expected result: the test would have gone green on the exact behaviour
    the issue was filed about. The intent — "the write still happens" — is kept;
    the witness moves off the sibling rows and onto f-alpha's own departed one.
    """
    root = _host(tmp_path)
    state = _seed_state(root)
    proc = _prune(root)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(state.read_text())
    assert "a0" not in data["bots"], "f-alpha's own departed row should be reaped"
    assert sorted(data["bots"]) == ["a1", "a2", "b1", "g1", "orphan"]
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


# --- the scoping half: whose rows a prune may remove -------------------------
#
# The assertions that matter here run against the fleets NOT doing the prune. A
# prune that is correct for its own fleet and destructive for its siblings passes
# every test that only inspects the fleet doing the work, which is exactly how
# this shipped: the write-path test above required the sibling rows to vanish.


def _update(root: Path, *args: str, fleet: str | None = None):
    """Drive the normal update arm. Env is built from scratch, not inherited:
    FLEET_NAME is exported into every real bot session, so a test that inherits
    the runner's environment asserts something different locally than in CI."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(root),
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_STATE_PATH": str(root / "state" / "fleet-state.json"),
    }
    if fleet is not None:
        env["FLEET_NAME"] = fleet
    return subprocess.run(
        ["bash", str(UPDATER), *args], capture_output=True, text=True, env=env
    )


@pytest.mark.parametrize(
    "row,why",
    [
        ("b1", "a SIBLING fleet's live bot"),
        ("g1", "another sibling's live bot"),
        ("orphan", "unattributed — predates .fleet stamping, so never guessed at"),
    ],
)
def test_prune_leaves_every_row_it_does_not_own(tmp_path: Path, row, why) -> None:
    root = _host(tmp_path)
    state = _seed_state(root)
    _prune(root)
    assert row in json.loads(state.read_text())["bots"], f"{row} destroyed, but it is {why}"


def test_sibling_rows_are_byte_identical_after_a_prune(tmp_path: Path) -> None:
    """Present is not enough — a surviving row with a rewritten field is damage."""
    root = _host(tmp_path)
    state = _seed_state(root)
    before = json.loads(state.read_text())["bots"]
    _prune(root)
    after = json.loads(state.read_text())["bots"]
    for row in ("b1", "g1", "orphan"):
        assert after[row] == before[row], f"{row} was modified by another fleet's prune"


def test_a_row_that_moved_fleets_is_not_reaped_by_its_old_one(tmp_path: Path) -> None:
    """`claudlobby move-bot` leaves a window where the row still carries the OLD
    fleet while the manifests already say the new one. Attribution alone would
    delete a live bot that had just moved away; the host-wide declaration check
    is what closes it."""
    root = _host(tmp_path)
    state = _seed_state(root, {**SEED_ROWS, "b1": {"status": "idle", "fleet": "f-alpha"}})
    _prune(root)
    assert "b1" in json.loads(state.read_text())["bots"]


def test_prune_backfills_attribution_on_its_own_declared_rows(tmp_path: Path) -> None:
    """Backfill is what makes scoping converge after one reconcile per fleet
    rather than waiting on each bot's next write."""
    root = _host(tmp_path)
    state = _seed_state(root, {"a1": {"status": "idle"}, "b1": {"status": "idle"}})
    _prune(root)
    assert json.loads(state.read_text())["bots"]["a1"]["fleet"] == "f-alpha"


def test_writer_stamps_the_fleet_without_displacing_row_defaults(tmp_path: Path) -> None:
    root = _host(tmp_path)
    _seed_state(root, {})
    _update(root, "newbot", "working", "t1", "repo1", fleet="f-alpha")
    row = json.loads((root / "state" / "fleet-state.json").read_text())["bots"]["newbot"]
    assert row["fleet"] == "f-alpha"
    assert row["status"] == "working"
    assert row["last_completed"] is None, "the //= default object was displaced"


def test_writer_without_fleet_name_leaves_the_row_unattributed(tmp_path: Path) -> None:
    """An operator shell has no FLEET_NAME. Absent is PROTECTED from prune; a
    blank stamp would not be, so the field must be left off rather than set."""
    root = _host(tmp_path)
    _seed_state(root, {})
    _update(root, "operbot", "idle")
    row = json.loads((root / "state" / "fleet-state.json").read_text())["bots"]["operbot"]
    assert "fleet" not in row

