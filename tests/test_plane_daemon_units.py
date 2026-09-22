"""Phase-2 T1: composed host-service units + the launcher.

The load-bearing assertion is compose-time dormancy: an UNARMED service job
composes NO files, because setup-system's macOS leg enrolls every
claudlobby-*.plist it finds — a composed-but-dormant service plist would be
started by the next setup run (a root pull silently activating a resident
process, the no-silent-switches violation)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import claudlobby.composer as composer_mod
from claudlobby.paths import Paths


REPO = Path(__file__).resolve().parent.parent


def _paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path)


def _compose(tmp_path: Path, monkeypatch, jobs: dict) -> Path:
    monkeypatch.setattr(composer_mod, "load_host_jobs", lambda: jobs)
    return composer_mod.compose_host_timers(_paths(tmp_path))


SERVICE_JOB = {
    "unit": "service",
    "script": "$CLAUDLOBBY_ROOT/lib/plane-daemon.sh",
}


def test_armed_service_composes_service_and_plist_no_timer(tmp_path, monkeypatch):
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    service = out / "claudlobby-plane-daemon.service"
    plist = out / "claudlobby-plane-daemon.plist"
    assert service.exists() and plist.exists()
    assert not (out / "claudlobby-plane-daemon.timer").exists()
    body = service.read_text()
    assert "Restart=always" in body
    assert f"Environment=CLAUDLOBBY_ROOT={tmp_path}" in body
    assert f"{tmp_path}/lib/plane-daemon.sh" in body
    pbody = plist.read_text()
    assert "<key>KeepAlive</key>" in pbody and "<true/>" in pbody
    assert "<key>RunAtLoad</key>" in pbody


def test_service_relaunches_on_a_NONZERO_exit(tmp_path, monkeypatch):
    """#1485. The ingest daemon exits 4 when the db outruns its loaded code,
    so the composed units are what turns that exit into a repair. Both forms
    already relaunch on ANY exit; this pins them against a narrowing.

    launchd: `KeepAlive` must be the bare <true/>, not the dictionary form.
    `<key>KeepAlive</key><dict><key>SuccessfulExit</key><true/></dict>` would
    relaunch ONLY on a clean exit and strand the daemon on exactly the exit
    this fix introduces — and it is one word away from the correct dictionary
    form, which is reason enough to pin the shape rather than the key."""
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    body = (out / "claudlobby-plane-daemon.service").read_text()
    assert "Restart=always" in body, "on-success/no would strand the exit"
    # RestartSec keeps a permanent-condition loop under systemd's default
    # start limit (5 starts / 10s); without it the unit latches `failed` and
    # stops relaunching, which is the incident again with extra steps.
    rsec = [ln for ln in body.splitlines() if ln.startswith("RestartSec=")]
    assert len(rsec) == 1, rsec
    assert int(rsec[0].split("=", 1)[1]) >= 2, rsec

    pbody = (out / "claudlobby-plane-daemon.plist").read_text()
    squashed = "".join(pbody.split())
    assert "<key>KeepAlive</key><true/>" in squashed, (
        "KeepAlive must be unconditional — a SuccessfulExit dict would skip"
        f" the downgrade exit: {pbody}"
    )
    assert "SuccessfulExit" not in pbody


def test_the_exit_line_lands_somewhere_a_person_can_read(tmp_path, monkeypatch):
    """#1485 fold. The daemon's ONE exit line is the only record a stale exit
    leaves — a process that refuses the db cannot write a row about refusing
    it — and launchd sends an unredirected service's stdio to /dev/null, so on
    macOS a relaunch loop was invisible: no plane row, no log, nothing.

    Pinned against `plane.daemon.DAEMON_LOG_NAME` rather than a literal,
    because the exit line PRINTS that path: if the two drift, the daemon sends
    an operator to a file that holds nothing, which is worse than silence."""
    from claudlobby.plane.daemon import DAEMON_LOG_NAME

    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    log = tmp_path / "state" / DAEMON_LOG_NAME
    squashed = "".join((out / "claudlobby-plane-daemon.plist").read_text().split())
    assert f"<key>StandardOutPath</key><string>{log}</string>" in squashed
    assert f"<key>StandardErrorPath</key><string>{log}</string>" in squashed
    # launchd drops output when the DIRECTORY is missing, so compose must
    # provision it — a path pinned into a plist nobody can write to is the
    # same silence with extra steps.
    assert log.parent.is_dir(), "state/ must exist for launchd to open the log"

    # systemd carries no redirect on purpose: an unredirected unit's stderr is
    # already in the journal, and a StandardOutput= would move it out of
    # `journalctl -u` where every other unit's output lives.
    body = (out / "claudlobby-plane-daemon.service").read_text()
    assert "StandardOutput=" not in body and "StandardError=" not in body


def test_unarmed_service_composes_nothing(tmp_path, monkeypatch):
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": False}}
    )
    leftovers = (
        [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    )
    assert leftovers == [], f"dormant service leaked units: {leftovers}"


def test_enroll_absent_means_dormant_for_services(tmp_path, monkeypatch):
    """Timers default enroll to TRUE; services must default to FALSE — the
    asymmetry is the safety property, so pin it."""
    out = _compose(tmp_path, monkeypatch, {"plane-daemon": dict(SERVICE_JOB)})
    leftovers = (
        [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    )
    assert leftovers == []


def test_service_script_source_guard_applies(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="source"):
        _compose(
            tmp_path,
            monkeypatch,
            {
                "plane-daemon": {
                    "unit": "service",
                    "enroll": True,
                    "script": "/etc/passwd",
                }
            },
        )


def test_sibling_timer_jobs_still_compose_around_a_service(tmp_path, monkeypatch):
    out = _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": True},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
    assert (out / "claudlobby-disk-monitor.timer").exists()
    assert (out / "claudlobby-plane-daemon.service").exists()


def test_armed_to_unarmed_transition_prunes_the_composed_units(tmp_path, monkeypatch):
    """PR-#1345 review F3: recomposing unarmed left the armed run's units on
    disk, and setup-system enrolls every claudlobby-*.plist it finds — the
    supposedly dormant service silently reactivated. Disarm must PRUNE."""
    armed = {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    out = _compose(tmp_path, monkeypatch, armed)
    assert (out / "claudlobby-plane-daemon.service").exists()
    unarmed = {"plane-daemon": {**SERVICE_JOB, "enroll": False}}
    _compose(tmp_path, monkeypatch, unarmed)
    leftovers = sorted(p.name for p in out.glob("claudlobby-plane-daemon.*"))
    assert leftovers == [], f"disarming left units for setup to re-enroll: {leftovers}"


def test_prune_only_touches_the_disarmed_service_units(tmp_path, monkeypatch):
    out = _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": True},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
    _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": False},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
    assert not (out / "claudlobby-plane-daemon.service").exists()
    assert (out / "claudlobby-disk-monitor.timer").exists(), (
        "the prune must never reach sibling timer jobs"
    )


def test_service_enroller_refuses_foreign_owner_without_adopt(tmp_path):
    """PR-#1345 review F5: enrolling from a second tree silently captured the
    first tree's installed unit — the timer enroller's ownership gate now
    guards the service enroller too, with --adopt as the explicit override."""
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "systemctl").write_text("#!/bin/bash\nexit 0\n")
    os.chmod(stub_bin / "systemctl", 0o755)

    def composed_unit(root: str) -> str:
        return (
            "# Generated by claudlobby — do not hand-edit.\n[Unit]\n"
            "Description=claudlobby plane-daemon (host service)\n\n[Service]\n"
            f"Environment=CLAUDLOBBY_ROOT={root}\nExecStart={root}/lib/plane-daemon.sh\n"
        )

    installed = (
        home / ".config" / "systemd" / "user" / "claudlobby-plane-daemon.service"
    )
    installed.write_text(composed_unit("/srv/root-A"))
    units_b = tmp_path / "units-b"
    units_b.mkdir()
    (units_b / "claudlobby-plane-daemon.service").write_text(
        composed_unit("/srv/root-B")
    )

    def run(*extra):
        return subprocess.run(
            [
                "bash",
                str(REPO / "lib" / "install-host-service-systemd.sh"),
                "plane-daemon",
                *extra,
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{stub_bin}:/usr/bin:/bin",
                "HOME": str(home),
                "TIMER_DIR": str(units_b),
                "UNIT_NAME": "claudlobby-plane-daemon",
            },
        )

    refused = run()
    assert refused.returncode != 0, "foreign-owner capture must refuse"
    assert "root-A" in installed.read_text(), "the refusal must not overwrite"
    adopted = run("--adopt")
    assert adopted.returncode == 0, adopted.stderr
    assert "adopting" in adopted.stdout
    assert "root-B" in installed.read_text()


def test_launcher_127_when_python3_cannot_import_claudlobby(tmp_path):
    """PR-#1345 review F6: on a bare host /bin/python3 exists and cannot
    import claudlobby — `python3 -m claudlobby` then exits 1, masquerading
    as a daemon failure. The launcher must probe the import and fall through
    to the honest 127.

    #1652 review (ravi): this carried `/usr/bin:/bin` alongside the stub, so
    it was safe on this host only because no real `claudlobby` sits in either
    -- host layout again, the exact shape #1652 removed from its own sibling.
    A host with claudlobby installed system-wide resolves the CLI at the
    launcher's SECOND rung (before this test's fake python3 is ever reached)
    and hangs identically. No real system directory on PATH closes it, same
    as the fix this test sits beside.
    """
    root = tmp_path / "root"
    root.mkdir()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    import shutil as _shutil

    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    fake_py = stub_dir / "python3"
    fake_py.write_text(
        "#!/bin/bash\n"
        'case "$*" in *"import claudlobby"*) exit 1;; esac\n'
        'echo "MUST NOT EXEC: $*"; exit 99\n'
    )
    os.chmod(fake_py, 0o755)
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": str(stub_dir), "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "MUST NOT EXEC" not in r.stdout
    assert "no claudlobby CLI resolvable" in r.stderr


def test_example_system_yaml_ships_the_daemon_ARMED():
    """The defaults flip (chunk N): the ingest daemon ships ON.

    The plane has been the estate's only record since the F18 closure, so a
    dormant daemon meant every emit paid an interpreter spawn while the
    behavior itself ran regardless — a default that bought nothing and cost
    latency on the cheapest hardware the north star names. The COMPOSE-time
    dormancy machinery is untouched and still pinned by the tests above; only
    the shipped value moved."""
    text = (REPO / "system.yaml.example").read_text()
    assert "plane-daemon:" in text
    block = text.split("plane-daemon:", 1)[1]
    assert block.splitlines()[1].strip() == "enroll: true"
    assert "unit: service" in block


def test_launcher_execs_resolved_cli_with_serve_args(tmp_path):
    """#1652 review (ravi): isolated PATH + timeout=, same as the launcher's
    other tests -- the stub's fake `claudlobby` wins PATH resolution over
    anything real further down regardless, but leaving real system
    directories on PATH is the pattern that bit #1652 once already."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    import shutil as _shutil

    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    stub = stub_dir / "claudlobby"
    stub.write_text('#!/bin/bash\necho "CLI-ARGS:$*"\n')
    os.chmod(stub, 0o755)
    root = tmp_path / "root"
    root.mkdir()
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": str(stub_dir),
            "CLAUDLOBBY_ROOT": str(root),
            "PLANE_SOCKET": "/tmp/x.sock",
        },
    )
    assert r.returncode == 0, r.stderr
    # --root is GLOBAL (precedes the subcommand) — the smoke run caught the
    # inverted order as a real argparse error where this stub accepted it.
    assert f"CLI-ARGS:--root {root} plane serve --socket /tmp/x.sock" in r.stdout


def test_launcher_prefers_the_root_venv(tmp_path):
    """#1652 review (ravi): isolated PATH + timeout=, same as the launcher's
    other tests. Rung 1 (the venv CLI) wins unconditionally here, so nothing
    on PATH past the stub can change this test's own verdict -- but leaving
    real system directories on PATH is the pattern #1652 removed elsewhere
    in this file, and there is no reason this one test should still depend
    on it."""
    root = tmp_path / "root"
    (root / ".venv" / "bin").mkdir(parents=True)
    venv_cli = root / ".venv" / "bin" / "claudlobby"
    venv_cli.write_text("#!/bin/bash\necho VENV-CLI\n")
    os.chmod(venv_cli, 0o755)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    import shutil as _shutil

    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": str(stub_dir), "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 0, r.stderr
    assert "VENV-CLI" in r.stdout


def test_launcher_127_when_nothing_resolves(tmp_path):
    """PATH resolves nothing but the one coreutil the launcher itself needs
    (`dirname`) -- no claudlobby CLI, no python3 at all.

    #1652: this used to ALSO carry `/bin` on the theory that macOS keeps
    python3 in /usr/bin, so excluding /usr/bin was enough. That is a claim
    about macOS, not about this test's own premise: on a usrmerge Linux host
    (Debian/Ubuntu/RPi OS -- not a rare configuration) `/bin` IS `/usr/bin`,
    so `/bin/python3` resolves there too, `command -v python3` succeeds, and
    if this account has claudlobby importable via user site-packages (an
    editable `pip install --user -e .` is exactly what a dev checkout has --
    confirmed live, and confirmed to survive even a fully EMPTIED environment,
    since Python's user-site lookup falls back to the UID's own home
    directory via `pwd` when HOME is unset) the launcher's third rung
    SUCCEEDS and `exec`s a REAL, long-running `plane serve` daemon in place
    of this test process. `capture_output=True` then blocks forever waiting
    for stdout/stderr to close, which a live daemon never does -- the suite
    hangs, and killing the run leaves the daemon behind (it inherited this
    process's PID via exec, so nothing besides this process was ever
    tracking it).

    A test whose premise depends on which real system directories happen NOT
    to contain python3 is a test about host layout, not about the launcher.
    So: no real system directory on PATH at all -- only an isolated stub
    holding the one binary actually needed. `command -v python3` then fails
    by construction, on any host, regardless of usrmerge or what is
    pip-installed for the account running it.

    `timeout=` is defense in depth, not the fix: if some OTHER host quirk
    this reasoning has not anticipated ever makes a rung resolve again, the
    test fails fast with a clear TimeoutExpired instead of hanging the suite
    -- and `subprocess.run`'s own documented timeout behavior kills the
    child (by PID, which `exec` preserves) rather than leaving it running,
    so a regression can no longer strand a real daemon either.
    """
    root = tmp_path / "root"
    root.mkdir()
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    import shutil as _shutil

    # SYMLINK, never copy (#1372 review verification note): a copied macOS
    # system binary can wedge uninterruptibly under SIP/quarantine and hung
    # the whole suite on the reviewer's host.
    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": str(stub_dir), "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "no claudlobby CLI resolvable" in r.stderr


def test_launcher_127_when_nothing_resolves_even_with_user_site_reachable(tmp_path):
    """The regression case, reproduced directly rather than argued: python3
    genuinely on PATH (a usrmerge-shaped layout, or any host where /bin and
    /usr/bin overlap) must still fall through to 127 -- because the import
    probe itself must find claudlobby unimportable, not because python3 is
    unreachable. `PYTHONNOUSERSITE=1` is what actually defeats the mechanism
    #1652 found live on this host: an editable dev install reachable via
    user site-packages regardless of PATH, venv, or even HOME being unset.

    CI review (dara, live): PYTHONNOUSERSITE=1 is not a universal answer to
    "can this python3 import claudlobby" -- it disables only the SEPARATE
    user-site directory. On GitHub Actions, `actions/setup-python` + a bare
    `pip install -e '.[dev]'` (no --user) lands claudlobby in that
    interpreter's REGULAR site-packages, which PYTHONNOUSERSITE never
    touches -- so this test's own launcher call genuinely hung to its 10s
    ceiling there (caught by CI, not asserted around). Same class of mistake
    as #1652 itself, one layer down: a host-specific mitigation was written
    as though it were host-independent. So the premise is now verified
    directly, with a positive control that fails LOUD and FAST if it's
    false, rather than assumed and left to the launcher's own timeout to
    discover it 10 seconds later: probe PYTHONNOUSERSITE against THIS
    real_python3 before ever invoking the launcher, and skip (not fail) with
    the reason on a host where it doesn't hold -- a host with claudlobby
    reachable through regular site-packages is not this test's regression
    case, and forcing it through anyway only reproduces a slow, uninformative
    timeout instead of an honest "not applicable here".

    READ THIS BEFORE TRUSTING A GREEN CI RUN ON THIS FILE: this specific
    test SKIPS on GitHub Actions, by construction -- CI's install (regular
    site-packages, not user-site) is exactly the shape the paragraph above
    describes as "not this test's regression case". So "CI green" here does
    NOT mean this test's own rc==127 assertion ran there; it means the
    precondition correctly declined to run it. The regression this test
    exists to catch is exercised on a host shaped like the one #1652 was
    found on (a user-site editable install), not on CI -- if that ever
    changes, this test starts asserting instead of skipping, silently and
    correctly, with no edit needed here."""
    root = tmp_path / "root"
    root.mkdir()
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    import shutil as _shutil

    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    real_python3 = _shutil.which("python3")
    if real_python3 is None:
        pytest.skip("no real python3 on this host to prove the regression case with")

    # cwd=root (an empty dir with no claudlobby/ subdirectory), matching the
    # real launcher's own probe (`cd "$ROOT" && python3 -c "import
    # claudlobby"`) exactly -- WITHOUT it this probe silently tests a THIRD
    # thing: Python's own sys.path[0]='' resolves `import claudlobby` against
    # the CURRENT WORKING DIRECTORY first, and pytest's cwd is a checkout of
    # THIS repo, which IS a directory containing a claudlobby/ package. Found
    # live: this probe, run without cwd=, reported "importable" on a host
    # where PYTHONNOUSERSITE genuinely does defeat the user-site mechanism --
    # a false positive from the probe having nothing to do with the question
    # it was written to ask.
    probe = subprocess.run(
        [real_python3, "-c", "import claudlobby"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(root),
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    if probe.returncode == 0:
        pytest.skip(
            "PYTHONNOUSERSITE=1 does not make claudlobby unimportable for "
            f"{real_python3} on this host -- it is reachable through regular "
            "site-packages (e.g. a bare `pip install`, no --user), not the "
            "user-site mechanism this test targets. Not this test's "
            "regression case here."
        )

    os.symlink(real_python3, stub_dir / "python3")
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": str(stub_dir),
            "CLAUDLOBBY_ROOT": str(root),
            "PYTHONNOUSERSITE": "1",
        },
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "no claudlobby CLI resolvable" in r.stderr
