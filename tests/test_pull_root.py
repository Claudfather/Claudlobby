"""lib/pull-root.sh — the compositor root pulls itself, and watches what it did (#1251).

Each test is named for a failure from the design's table
(https://github.com/Claudfather/Claudlobby/issues/1251#issuecomment-5845912184).

The job runs inside a scratch INSTALL: a bare origin and a clone of it whose
lib/ is the real lib/, so it pulls the tree it runs from, exactly as on a host.
Only plane-lookup.py is a stub (committed, so the tree stays clean); the
`claudlobby` CLI and `systemctl` are stubs on PATH; the run's record lands on a
REAL plane under the scratch root through the shim's cold CLI rung.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import constructed_env, plane_emit_env, read_fleet_events

REPO = Path(__file__).resolve().parent.parent
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

# plane-lookup.py stub: canned answers per mode. The first --escalation call is
# the pre-pull window, every later one the post-pull window.
LOOKUP_STUB = """#!/usr/bin/env python3
import os, sys
stub = os.environ["PULL_ROOT_STUB"]
if "--escalation" in sys.argv:
    calls = os.path.join(stub, "esc_calls")
    n = int(open(calls).read()) if os.path.exists(calls) else 0
    open(calls, "w").write(str(n + 1))
    name = "esc_pre" if n == 0 else "esc_post"
elif "script_error" in sys.argv:
    name = "errors"
else:
    sys.exit(0)
path = os.path.join(stub, name)
if os.path.exists(path):
    sys.stdout.write(open(path).read())
"""

# `claudlobby` stub: the job's two CLI reads.
CLI_STUB = """#!/usr/bin/env bash
case "$*" in
*host-job*) cat "$PULL_ROOT_STUB/job.json" ;;
*status*--json*) python3 "$PULL_ROOT_STUB/status.py" "$CLAUDLOBBY_ROOT" ;;
esac
"""

# Heartbeats relative to the job's own windows (state/pull-root/window), so the
# fixture needs no clock: "silent" heartbeated 1 s before the pull, "healthy" after.
STATUS_PY = """import datetime as d, json, os, sys
pre, t0 = open(os.path.join(sys.argv[1], "state/pull-root/window")).read().split()
at = d.datetime.fromisoformat(t0)
names = open(os.path.join(os.environ["PULL_ROOT_STUB"], "silent")).read().split() \\
    if os.path.exists(os.path.join(os.environ["PULL_ROOT_STUB"], "silent")) else []
bots = [{"name": "healthy", "last_heartbeat": (at + d.timedelta(seconds=1)).isoformat(), "plane_unreachable": None}]
bots += [{"name": n, "last_heartbeat": (at - d.timedelta(seconds=1)).isoformat(), "plane_unreachable": None} for n in names]
print(json.dumps({"fleet": "f1", "bots": bots}))
"""


def _git(cwd, *args):
    return subprocess.run(
        [*GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="session")
def template_origin(tmp_path_factory):
    """A bare origin whose one commit is this repo's lib/ (plane-lookup.py
    stubbed) and .gitignore — built once, copied per test."""
    base = tmp_path_factory.mktemp("pull-root-template")
    work = base / "work"
    shutil.copytree(REPO / "lib", work / "lib", symlinks=True)
    (work / "lib" / "plane-lookup.py").write_text(LOOKUP_STUB)
    shutil.copy(REPO / ".gitignore", work / ".gitignore")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(base / "origin.git")],
        check=True,
    )
    return base / "origin.git"


class Install:
    def __init__(self, tmp_path: Path, template: Path):
        self.origin = tmp_path / "origin.git"
        shutil.copytree(template, self.origin)
        self.root = tmp_path / "root"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root)], check=True
        )
        self.work = tmp_path / "upstream"  # where "merged PRs" are made
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.work)], check=True
        )
        (self.root / "local" / "f1").mkdir(parents=True)
        (self.root / "local" / "f1" / "fleet.yaml").write_text("fleet: {name: f1}\n")
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        (self.stub / "status.py").write_text(STATUS_PY)
        self.job({"hold": None})
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("claudlobby", CLI_STUB),
            (
                "systemctl",
                '#!/bin/bash\necho "$*" >> "$PULL_ROOT_STUB/systemctl.log"\n',
            ),
        ):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.home = tmp_path / "home"
        units = self.home / ".config" / "systemd" / "user"
        units.mkdir(parents=True)
        for unit in ("claudlobby-plane-daemon", "claudlobby-plane-view"):
            (units / f"{unit}.service").write_text("[Service]\n")

    def job(self, cfg: dict) -> None:
        (self.stub / "job.json").write_text(json.dumps(cfg))

    def merge_upstream(self, files: dict, msg: str = "upstream") -> str:
        for rel, text in files.items():
            (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.work / rel).write_text(text)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", msg)
        _git(self.work, "push", "-q", "origin", "main")
        return _git(self.work, "rev-parse", "--short", "HEAD")

    def head(self) -> str:
        return _git(self.root, "rev-parse", "--short", "HEAD")

    def run(self) -> subprocess.CompletedProcess:
        env = constructed_env(
            PATH=f"{self.bin}:/usr/bin:/bin",
            HOME=self.home,
            CLAUDLOBBY_ROOT=self.root,
            PULL_ROOT_STUB=self.stub,
            PULL_ROOT_WATCH_S="1",
            FLEET_EVENT_EMIT_TIMEOUT_S="60",
            **plane_emit_env(),
        )
        return subprocess.run(
            ["bash", str(self.root / "lib" / "pull-root.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def events(self, etype: str) -> list[dict]:
        rows = [json.loads(line) for line in read_fleet_events(self.root).splitlines()]
        return [r for r in rows if r.get("type") == etype]

    def records(self) -> list[dict]:
        return [r["data"] for r in self.events("source_pull")]

    def restarts(self) -> list[str]:
        log = self.stub / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def inst(tmp_path, template_origin) -> Install:
    return Install(tmp_path, template_origin)


def _ok(proc):
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_run_lands_exactly_one_source_pull_record(inst):
    # 09-09: "no pull is recorded anywhere I can read" -- a pull that happened
    # and one that did not were indistinguishable after the fact.
    to = inst.merge_upstream({"lib/new.sh": "echo new\n"})
    _ok(inst.run())
    _ok(inst.run())
    outcomes = [(r["outcome"], r["to"]) for r in inst.records()]
    assert outcomes == [("pulled", to), ("current", to)]


def test_a_dirty_tree_is_refused_and_the_blocker_named_not_a_command(inst):
    # 2026-08-27: one staged file held 83 commits for 14 days while the daily
    # notice printed `git pull --ff-only`, a remedy that could not run.
    before = inst.head()
    inst.merge_upstream({"lib/new.sh": "echo new\n"})
    (inst.root / "lib" / "residue.md").write_text("staged by someone\n")
    _git(inst.root, "add", "lib/residue.md")
    _ok(inst.run())
    assert inst.head() == before
    (record,) = inst.records()
    assert record["outcome"] == "blocked" and "lib/residue.md" in record["blocker"]
    (notice,) = inst.events("source_pull_blocked")
    assert "lib/residue.md" in json.dumps(notice) and "git pull" not in json.dumps(
        notice
    )


def test_a_hold_pins_the_host_and_never_moves_past_it(inst):
    # #865: a deliberate pin read as drift, and the remedy printed for drift.
    b = inst.merge_upstream({"lib/b.sh": "echo b\n"}, "B")
    inst.merge_upstream({"lib/c.sh": "echo c\n"}, "C")
    inst.job(
        {
            "hold": {
                "sha": b,
                "by": "dara",
                "reason": "canary B first",
                "until": "2999-01-01",
            }
        }
    )
    _ok(inst.run())
    assert inst.head() == b  # up to the hold...
    _ok(inst.run())
    assert inst.head() == b  # ...and never past it
    assert [r["outcome"] for r in inst.records()] == ["pulled", "held"]
    assert "canary B first" in inst.records()[1]["hold"]


def test_an_expired_or_incomplete_hold_still_holds_and_says_so(inst):
    # #865's second instance: a pause whose reason had expired, unrecognized by
    # its own author. Lapsing would roll the change the hold protected.
    before = inst.head()
    inst.merge_upstream({"lib/b.sh": "echo b\n"})
    inst.job(
        {
            "hold": {
                "sha": before,
                "by": "dara",
                "reason": "baseline",
                "until": "2000-01-01",
            }
        }
    )
    _ok(inst.run())
    inst.job({"hold": {"sha": before}})
    _ok(inst.run())
    assert inst.head() == before
    notices = json.dumps(inst.events("source_pull_held"))
    assert "past its expiry" in notices and "incomplete hold" in notices


def test_the_plane_services_restart_when_claudlobby_moved_and_only_then(inst):
    # 2026-09-22: a pull changed the daemon's ingest code with no migration, so
    # exit 4 never fired; the view served pre-pull code for ~20 h.
    inst.merge_upstream({"lib/only.sh": "echo lib\n"})
    _ok(inst.run())
    assert inst.restarts() == []
    inst.merge_upstream({"claudlobby/plane/ingest.py": "# moved\n"})
    _ok(inst.run())
    assert inst.restarts() == [
        "--user restart claudlobby-plane-daemon.service",
        "--user restart claudlobby-plane-view.service",
    ]
    assert (
        inst.records()[-1]["restarted"]
        == "claudlobby-plane-daemon claudlobby-plane-view"
    )


def test_the_watch_pages_on_a_bot_that_went_silent_at_the_pull(inst):
    # #1485: 261 heartbeat samples lost in ~15 minutes on the Mini after a pull,
    # found by hand. A bot heartbeating before the pull and not since is the sign.
    (inst.stub / "silent").write_text("otis\n")
    inst.merge_upstream({"lib/keepalive-ish.sh": "echo k\n"})
    _ok(inst.run())
    (record,) = inst.records()
    assert (
        record["watch"] == "paged"
        and "no heartbeat since the pull from otis" in record["findings"]
    )
    assert "healthy" not in record["findings"]
    (page,) = inst.events("source_pull_regression")
    assert "otis" in json.dumps(page)


def test_the_watch_pages_on_what_the_pull_brought_not_on_what_was_already_there(inst):
    # script_error runs 3-10 a day on one fleet here, and a critical that was
    # firing before the pull is not the pull's: paging on either is noise.
    (inst.stub / "esc_pre").write_text(
        "vera activity_stuck 2026-09-26T10:50:00+00:00\n"
    )
    (inst.stub / "esc_post").write_text(
        "vera activity_stuck 2026-09-26T11:05:00+00:00\n"
        "otis crash_loop 2026-09-26T11:06:00+00:00\n"
    )
    (inst.stub / "errors").write_text(
        json.dumps(
            {"bot": "otis", "type": "script_error", "data": {"script": "changed.sh"}}
        )
        + "\n"
        + json.dumps(
            {"bot": "vera", "type": "script_error", "data": {"script": "untouched.sh"}}
        )
        + "\n"
    )
    inst.merge_upstream({"lib/changed.sh": "echo c\n"})
    _ok(inst.run())
    findings = inst.records()[0]["findings"]
    assert (
        "new critical otis/crash_loop" in findings
        and "vera/activity_stuck" not in findings
    )
    assert "otis:changed.sh" in findings and "untouched.sh" not in findings


def test_a_quiet_watch_records_clean_and_pages_nobody(inst):
    # The canary is a watch, not a feed: a clean pull must not train the
    # operator to ignore the page that matters.
    inst.merge_upstream({"lib/new.sh": "echo new\n"})
    _ok(inst.run())
    assert inst.records()[0]["watch"] == "clean"
    assert inst.events("source_pull_regression") == []


def test_a_migration_pull_page_carries_the_revert_runbook(inst):
    # Old code refuses a newer plane db and does not spool, so a revert past a
    # migration needs a db step the operator has to be told.
    before = inst.head()
    (inst.stub / "silent").write_text("otis\n")
    inst.merge_upstream({"claudlobby/plane/migrations/0099_probe.sql": "-- additive\n"})
    _ok(inst.run())
    page = json.dumps(inst.events("source_pull_regression"))
    assert "0099_probe.sql" in page and "PRAGMA user_version" in page
    assert f"reset --keep {before}" in page and "fix-forward" in page


def test_the_job_finishes_on_its_own_bytes_when_the_pull_rewrites_it(inst):
    # Run from INSIDE the tree: git replaces a changed file with a new inode, so
    # the running script keeps its old bytes. The rewrite shifts every offset
    # and ends in a marker-and-exit that would fire if bash read the new file.
    new = "\n".join(
        ["#!/usr/bin/env bash"]
        + ["# shifted"] * 400
        + ['touch "$CLAUDLOBBY_ROOT/NEW-BYTES-RAN"; exit 7', ""]
    )
    inst.merge_upstream({"lib/pull-root.sh": new}, "rewrite the job")
    proc = inst.run()
    _ok(proc)
    assert not (inst.root / "NEW-BYTES-RAN").exists()
    assert inst.records()[0]["outcome"] == "pulled"


def test_a_hold_is_readable_at_the_state_with_its_reason(tmp_path, monkeypatch):
    # #865's acceptance test: a reader inspecting the state recovers WHY a host
    # is held -- through the same merge the job reads.
    override = tmp_path / "host-override.yaml"
    override.write_text(
        "host: { jobs: { pull-root: { hold: { sha: abc1234, by: dara, "
        "reason: 'canary #1795 first', until: 2999-01-01 } } } }\n"
    )
    env = constructed_env(CLAUDLOBBY_HOST_SYSTEM_YAML=override, HOME=tmp_path)
    proc = subprocess.run(
        [
            str(
                Path(
                    shutil.which("python3", path=str(REPO / ".venv" / "bin"))
                    or "python3"
                )
            ),
            "-m",
            "claudlobby",
            "host-job",
            "pull-root",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hold"]["reason"] == "canary #1795 first"


def test_a_new_critical_pages_even_when_the_window_before_was_quiet(inst):
    # The common case is a quiet fleet: a pre-window with NO critical rows must
    # not read as "every post-pull row was already there" (awk's NR == FNR idiom
    # does exactly that when its first file is empty).
    (inst.stub / "esc_post").write_text("otis crash_loop 2026-09-26T11:06:00+00:00\n")
    inst.merge_upstream({"lib/new.sh": "echo new\n"})
    _ok(inst.run())
    assert "new critical otis/crash_loop" in inst.records()[0]["findings"]
