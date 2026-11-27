"""One reader of the Claude Code version, and one launch PATH (#1772).

`measure_claude_version` (lib-common) is the only thing that reads a version:
the binary RAN and the first line of its stdout carries X.Y.Z, or it is
could-not-measure with a reason. Every consumer goes through it: the update
job, the query door `claudlobby/_runtime_scripts/claude-version.sh` and so the registry scan, the eval's
version pin, the onboarding seeds and the permissions ladder's record. Each
one REFUSES when the binary cannot run, rather than recording a stand-in value.
Six copies of the reader existed, and three of them turned could-not-measure
into a value ("unavailable", "dry-run", 0.0.0).

`fleet_launch_path` is the PATH start-bot.sh exports; the update job resolves
the fleet's claude under it instead of a hand copy of the order.

Everything here is hermetic: every binary is a stub named by path or by
CLAUDE_BIN, because a host's own /usr/bin/claude sits first on the launch PATH
and would otherwise answer.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby.claude_version import VERSION_PATTERN
from tests.conftest import _write_exec, constructed_env
from tests.test_update_claude_code_verify import broken_stub, healthy

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"
HARNESS = REPO / "harness"
DOOR = LIB / "claude-version.sh"

#: Every binary shape the reader has to classify: name -> (script, verdict).
#: A verdict is the version, or a fragment the reason must carry.
SHAPES = {
    "healthy": (healthy("2.1.281"), "2.1.281"),
    "stub on stderr, exit 1": (
        broken_stub("stderr"),
        "exited 1: Error: claude native binary",
    ),
    "stub on stdout, exit 1": (
        broken_stub("stdout"),
        "exited 1: Error: claude native binary",
    ),
    "a version, then exit 1": (
        '#!/bin/bash\necho "2.1.281 (Claude Code)"\nexit 1\n',
        "exited 1",
    ),
    "a warning, then the version": (
        '#!/bin/bash\necho "Warning: slow disk"\necho "2.1.281"\n',
        "printed no parseable version: Warning: slow disk",
    ),
    "runs, prints nothing": ("#!/bin/bash\nexit 0\n", "printed no parseable version"),
    "a version on stderr only, exit 0": (
        '#!/bin/bash\necho "2.1.281 (Claude Code)" >&2\nexit 0\n',
        "printed no parseable version: 2.1.281 (Claude Code)",
    ),
}


def _stub(tmp_path: Path, name: str, script: str) -> Path:
    p = tmp_path / "bin" / re.sub(r"[^a-z0-9]+", "-", name)
    p.parent.mkdir(exist_ok=True)
    _write_exec(p, script)
    return p


def _run(tmp_path: Path, argv: list[str], *, _timeout=120, **env) -> subprocess.CompletedProcess:
    """argv in a constructed env, with a throwaway HOME and CLAUDLOBBY_ROOT."""
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=constructed_env(**{"HOME": tmp_path / "home", "CLAUDLOBBY_ROOT": root, **env}),
    )
    try:
        stdout, stderr = process.communicate(timeout=_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _bash(tmp_path: Path, code: str, **env) -> subprocess.CompletedProcess:
    """Run `code` with the real lib-common sourced."""
    return _run(tmp_path, ["bash", "-c", f'. "{LIB}/lib-common.sh"; set +e; {code}'], **env)


def _door(tmp_path: Path, *args: str, **env) -> subprocess.CompletedProcess:
    return _run(tmp_path, ["bash", str(DOOR), *args], **env)


def _bash_verdict(tmp_path: Path, binary: str) -> tuple[str | None, str | None]:
    r = _bash(
        tmp_path,
        f'if measure_claude_version "{binary}"; then printf "V=%s" "$CLAUDE_VERSION"; '
        'else printf "W=%s" "$CLAUDE_VERSION_WHY"; fi',
    )
    assert r.returncode == 0, r.stderr
    kind, _, text = r.stdout.partition("=")
    return (text, None) if kind == "V" else (None, text)


def _shell_function(script: str, name: str) -> str:
    """A native or measurement script's function `name`, through its closing
    brace: to run one function of a script that cannot be sourced, or to scope a
    search to one function."""
    text = ((LIB if script == "lib-common.sh" else HARNESS) / script).read_text()
    start = text.index(f"\n{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


def _managed_probe():
    shell = _shell_function("lib-common.sh", "measure_claude_version")
    return shell.split("        probe='", 1)[1].split("\n'\n", 1)[0]


# --- the door --------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_door_prints_a_version_or_nothing(tmp_path, shape):
    script, verdict = SHAPES[shape]
    r = _door(tmp_path, str(_stub(tmp_path, shape, script)))
    if re.fullmatch(VERSION_PATTERN, verdict):
        assert (r.returncode, r.stdout) == (0, f"{verdict}\n"), r.stderr
        assert r.stderr == ""
    else:
        # Could not measure: NOTHING on stdout, whatever the binary printed there.
        assert (r.returncode, r.stdout) == (3, ""), (r.stdout, r.stderr)
        assert verdict in r.stderr, r.stderr


def test_a_binary_that_hangs_is_could_not_measure_within_the_bound(tmp_path):
    hangs = _stub(tmp_path, "hangs", "#!/bin/bash\nsleep 30\n")
    start = time.monotonic()
    r = _door(tmp_path, str(hangs), CLAUDE_VERSION_TIMEOUT_S="1", _timeout=10)
    assert (r.returncode, r.stdout) == (3, ""), r.stderr
    assert "did not finish within 1s" in r.stderr, r.stderr
    assert time.monotonic() - start < 8  # scheduling tolerance, not two bare 30s runs


def test_missing_timeout_refuses_without_running_the_binary(tmp_path):
    marker = tmp_path / "executed"
    binary = _stub(tmp_path, "must-not-run", f'#!/bin/bash\ntouch "{marker}"\necho 2.1.281\n')
    r = _bash(tmp_path, f'_TIMEOUT_BIN=""; measure_claude_version "{binary}"; '
              'printf "%s|[%s]|%s" "$?" "$CLAUDE_VERSION" "$CLAUDE_VERSION_WHY"', _timeout=10)
    assert r.returncode == 0 and r.stdout.startswith("1|[]|timeout/gtimeout unavailable"), (r.stdout, r.stderr)
    assert not marker.exists()


#: One input per path on which measure_claude_version returns 1: each failing
#: shape, a hang, and a caller whose own resolution found nothing (None).
UNMEASURED = {
    **{s: sc for s, (sc, v) in SHAPES.items() if not re.fullmatch(VERSION_PATTERN, v)},
    "hangs": "#!/bin/bash\nsleep 2\n",
    "nothing resolved": None,
}


@pytest.mark.parametrize("shape", sorted(UNMEASURED))
def test_could_not_measure_leaves_claude_version_empty(tmp_path, shape):
    """The documented contract, on every path that returns 1: CLAUDE_VERSION is
    EMPTY, never a sentinel, even straight after a measurement that succeeded.
    Every consumer branches on the return code, so a stand-in left there passes
    all of them, and is read as a version by the first caller that prints it."""
    script = UNMEASURED[shape]
    binary = "" if script is None else str(_stub(tmp_path, shape, script))
    ok = _stub(tmp_path, "healthy", healthy("2.1.281"))
    # Only the hang is measured under a short bound: under one, a binary that is
    # merely slow to start would take the timeout path instead of its own.
    bound = "CLAUDE_VERSION_TIMEOUT_S=0.2 " if shape == "hangs" else ""
    r = _bash(
        tmp_path,
        f'measure_claude_version "{ok}"; before="$?:$CLAUDE_VERSION"; '
        f'{bound}measure_claude_version "{binary}"; '
        'printf "%s|%s|[%s]" "$before" "$?" "${CLAUDE_VERSION-UNSET}"',
    )
    assert r.stdout == "0:2.1.281|1|[]", (r.stdout, r.stderr)


def test_the_door_refuses_a_binary_that_is_not_there(tmp_path):
    r = _door(tmp_path, str(tmp_path / "nope" / "claude"))
    assert (r.returncode, r.stdout) == (3, "")
    assert "exited 127" in r.stderr, r.stderr


def test_an_empty_binary_is_a_caller_that_resolved_nothing(tmp_path):
    # Not "no argument": the fleet's binary is never substituted for it.
    r = _door(tmp_path, "", CLAUDE_BIN=_stub(tmp_path, "healthy", healthy("2.1.281")))
    assert (r.returncode, r.stdout) == (3, "")
    assert "no claude binary resolved" in r.stderr


def test_the_door_takes_at_most_one_binary(tmp_path):
    r = _door(tmp_path, "a", "b")
    assert r.returncode == 2 and "usage" in r.stderr


def test_with_no_argument_the_door_measures_what_the_fleet_launches(tmp_path):
    pinned = _stub(tmp_path, "pinned", healthy("2.1.279"))
    r = _door(tmp_path, CLAUDE_BIN=pinned)
    assert (r.returncode, r.stdout) == (0, "2.1.279\n"), r.stderr


def test_with_no_argument_the_door_follows_the_staged_fleet_link(tmp_path):
    """fleet_claude_bin's interface: with no CLAUDE_BIN, the staged link, when it
    resolves to an executable, is what the fleet launches, so it is what the
    door measures."""
    target = _stub(tmp_path, "staged", healthy("2.1.280"))
    link = tmp_path / "root" / "state" / "bin" / "claude"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    r = _door(tmp_path)
    assert (r.returncode, r.stdout) == (0, "2.1.280\n"), r.stderr


# --- the Python side: it calls the door, and carries exactly its verdict ------------


def _paths(tmp_path: Path, lib: Path = LIB):
    from tests.package_fixtures import source_package
    from claudlobby.paths import Paths

    root = tmp_path / "proot"
    root.mkdir(exist_ok=True)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(lib)
    return Paths(root=root, package=replace(source_package(), native=root / "lib"))


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_python_verdict_is_the_bash_verdict(tmp_path, shape, monkeypatch):
    from claudlobby import claude_version

    script, _ = SHAPES[shape]
    binary = str(_stub(tmp_path, shape, script))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    m = claude_version.measure(_paths(tmp_path), binary)
    assert (m.version, m.why) == _bash_verdict(tmp_path, binary)
    assert m.measured == (m.version is not None)


def test_the_python_side_measures_the_fleet_binary_through_the_door(
    tmp_path, monkeypatch
):
    from claudlobby import claude_version

    monkeypatch.setenv("CLAUDE_BIN", str(_stub(tmp_path, "pinned", healthy("2.1.279"))))
    m = claude_version.measure(_paths(tmp_path))
    assert (m.version, m.why) == ("2.1.279", None)


def test_an_unreachable_door_is_a_reason_never_a_raise_or_a_value(tmp_path):
    from claudlobby import claude_version

    m = claude_version.measure(_paths(tmp_path, lib=tmp_path / "no-lib"))
    assert m.version is None
    assert "version door" in m.why and "not there" in m.why


def test_a_door_that_breaks_its_contract_is_not_a_version(tmp_path):
    """A door exiting 0 with something that is not X.Y.Z is still unmeasured."""
    from claudlobby import claude_version

    lib = tmp_path / "fake-lib"
    lib.mkdir()
    _write_exec(lib / "claude-version.sh", "#!/bin/bash\necho unknown\nexit 0\n")
    m = claude_version.measure(_paths(tmp_path, lib=lib), "/x/claude")
    assert m.version is None
    assert "exited 0" in m.why and "'unknown'" in m.why


# --- the registry records a verdict, never a value -----------------------------------


def _host_system(tmp_path, monkeypatch, script: str) -> dict:
    from claudlobby.plane.contracts import HostPayload
    from claudlobby.plane.registry_emit import host_payload

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_BIN", str(_stub(tmp_path, "fleet", script)))
    payload = host_payload(_paths(tmp_path))
    HostPayload.model_validate(payload)  # what ingest does at the door
    return payload["system"]


def test_the_registry_records_the_measured_version(tmp_path, monkeypatch):
    system = _host_system(tmp_path, monkeypatch, healthy("2.1.281"))
    assert system["claude_version"] == "2.1.281"
    # Absent, not null: a measured keyframe keeps the shape an older daemon takes.
    assert "claude_version_unmeasured" not in system


def test_the_registry_records_why_a_version_could_not_be_measured(
    tmp_path, monkeypatch
):
    system = _host_system(tmp_path, monkeypatch, broken_stub("stdout"))
    assert system["claude_version"] is None
    assert (
        "exited 1: Error: claude native binary not installed"
        in system["claude_version_unmeasured"]
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"claude_version": "unavailable"},
        {"claude_version": "2.1.281 (Claude Code)"},
        {"claude_version": "Error: claude native binary not installed."},
        {"claude_version": None},
        {"claude_version": "2.1.281", "claude_version_unmeasured": "why"},
    ],
)
def test_the_contract_refuses_a_stand_in_or_a_verdict_without_its_reason(bad):
    from pydantic import ValidationError

    from claudlobby.plane.contracts import _HostSystem

    base = {"claudlobby_version": "x", "python_version": "3", "defaults_tier_hash": "a"}
    with pytest.raises(ValidationError):
        _HostSystem.model_validate({**base, **bad})


# --- the harnesses refuse rather than record a stand-in -----------------------------


def test_the_onboarding_seed_refuses_a_binary_that_cannot_run(tmp_path):
    cfg, creds = tmp_path / "cfg", tmp_path / "creds.json"
    cfg.mkdir()
    creds.write_text("{}")
    stub = _stub(tmp_path, "broken", broken_stub("stderr"))
    r = _bash(
        tmp_path,
        f'seed_claude_auth_and_trust "{cfg}" /x "{stub}" "{creds}"; echo "rc=$?"',
    )
    assert "rc=3" in r.stdout, (r.stdout, r.stderr)
    assert "refusing to seed for a claude that cannot run" in r.stderr
    assert "claude native binary not installed" in r.stderr
    # Nothing written, credentials included.
    assert sorted(p.name for p in cfg.iterdir()) == []


def test_the_onboarding_seed_records_the_measured_version(tmp_path):
    cfg, creds = tmp_path / "cfg", tmp_path / "creds.json"
    cfg.mkdir()
    creds.write_text("{}")
    stub = _stub(tmp_path, "healthy", healthy("2.1.281"))
    r = _bash(
        tmp_path,
        f'seed_claude_auth_and_trust "{cfg}" /x "{stub}" "{creds}"; echo "rc=$?"',
    )
    assert "rc=0" in r.stdout, r.stderr
    seeded = json.loads((cfg / ".claude.json").read_text())
    assert seeded["lastOnboardingVersion"] == "2.1.281"
    assert seeded["projects"]["/x"]["hasTrustDialogAccepted"] is True


#: A seed call whose continuation line stops the caller: it dies, or exits
#: NONZERO. An `exit 0` there is a refusal that reads as success to whatever ran
#: the harness.
SEED_REFUSAL = re.compile(
    r"seed_claude_auth_and_trust [^\n]*\\\n\s*\|\| (?:\{[^}]*exit [1-9]; \}|die )"
)


def _seed_calls_stop(text: str) -> list[bool]:
    """For each seed call in a script, whether the line after it stops the caller.
    Each call is matched from its own start, so no call is credited with the
    refusal of the call after it."""
    return [
        SEED_REFUSAL.match(text, m.start(1)) is not None
        for m in re.finditer(r"^[ \t]*(seed_claude_auth_and_trust )", text, re.M)
    ]


def test_every_seed_caller_stops_on_the_refusal():
    """A refusal a caller ignores is a stand-in by another route: the ladder runs
    without `set -e`, and would boot on with no onboarding seeded."""
    callers = {}
    for script in sorted(path for directory in (LIB, HARNESS)
                         for path in directory.glob("*.sh")):
        if stops := _seed_calls_stop(script.read_text()):
            callers[script.name] = stops
    ignoring = sorted(name for name, stops in callers.items() if not all(stops))
    assert ignoring == [], f"{ignoring} ignore the seed's refusal"
    assert sorted(callers) == [
        "ab-comms-eval.sh",
        "boot-strand-sampler.sh",
        "freshbox-boot-gate.sh",
        "rehearse-permissions-ladder.sh",
    ]


_SEED_CALL = 'seed_claude_auth_and_trust "$CFG" "$DIR" "$BIN" "$CREDS" \\\n'


@pytest.mark.parametrize(
    "script, stops",
    [
        (_SEED_CALL + '  || { say "FATAL: claude cannot run"; exit 2; }\n', [True]),
        (_SEED_CALL + '  || die "claude cannot run"\n', [True]),
        (_SEED_CALL + '  || { say "FATAL: claude cannot run"; exit 0; }\n', [False]),
        (_SEED_CALL + '  || { say "FATAL: claude cannot run"; }\n', [False]),
        (_SEED_CALL + "  || true\n", [False]),
        (_SEED_CALL + "  || { exit 0; }\n" + _SEED_CALL + '  || die "x"\n', [False, True]),
    ],
    ids=["exit 2", "die", "exit 0", "no exit", "or true", "the next call's refusal"],
)
def test_the_seed_ratchet_takes_only_a_refusal_that_fails(script, stops):
    """The ratchet's control. The harnesses' refusals sit after their real-boot
    setup, where no hermetic test executes them, so this ratchet is all that keeps
    an `exit 0` out of them."""
    assert _seed_calls_stop(script) == stops


def _ladder_record(tmp_path: Path, binary: Path) -> str:
    """The value of the ladder's `claude --version` record, from its own
    record_preconditions run with lib-common sourced. Only CLAUDE_BIN reaches that
    line: the other globals only have to be bound (the ladder runs under `set -u`),
    and jq, which feeds only the other lines, is stubbed out."""
    fn = _shell_function("rehearse-permissions-ladder.sh", "record_preconditions")
    unused = tmp_path / "unused"
    r = _bash(
        tmp_path,
        f"jq() {{ :; }}{fn}record_preconditions cell",
        CLAUDE_BIN=binary,
        FAKE_CFG=unused,
        FAKE_HOME=unused,
        LOC2=unused,
        LOC3=unused,
        PLACEMENT="in-workspace",
        TARGET=unused,
        LOG="/dev/null",
    )
    records = re.findall(r"^claude --version +: (.*)$", r.stdout, re.M)
    assert len(records) == 1, (r.stdout, r.stderr)
    return records[0]


def test_the_ladder_records_the_version_or_why_there_is_none(tmp_path):
    """Each cell's record carries the reader's verdict: the version, or CANNOT RUN
    with the reader's own reason. Never a stand-in, and never the first line of
    whatever the binary printed, which is how an error once read as a version.
    Reachable when the binary dies between the seed and a cell."""
    ok = _stub(tmp_path, "healthy", healthy("2.1.281"))
    assert _ladder_record(tmp_path, ok) == "2.1.281"
    broken = _stub(tmp_path, "broken", broken_stub("stdout"))
    _, why = _bash_verdict(tmp_path, str(broken))
    assert why and _ladder_record(tmp_path, broken) == f"CANNOT RUN ({why})"


def test_the_send_size_probe_refuses_before_building_anything(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    marker = tmp_path / "tool-executed"
    for tool in ("tmux", "jq"):
        _stub(tmp_path, tool, f'#!/bin/sh\nprintf invoked > "{marker}"\nexit 97\n')
    r = subprocess.run(
        ["bash", str(HARNESS / "send-size-probe.sh"), "--n", "1"],
        capture_output=True,
        text=True,
        timeout=120,
        env=constructed_env(
            PATH=str(tmp_path / "bin") + ":" + os.environ["PATH"],
            SEND_PROBE_REAL="1",
            CLAUDE_BIN=_stub(tmp_path, "broken", broken_stub("stderr")),
            HOME=tmp_path / "home",
            TMPDIR=tmp,
        ),
    )
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "cannot run" in r.stderr and "claude native binary not installed" in r.stderr
    assert list(tmp.iterdir()) == [], "the probe built scratch state before refusing"
    assert not marker.exists(), "refusal must precede even fixture tool execution"


def _eval(tmp_path, claude: str, *args, **env):
    """The eval harness, with `claude` on its PATH the one a real cell would run."""
    bindir = tmp_path / "evalbin"
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / "claude", claude)
    return subprocess.run(
        ["bash", str(HARNESS / "ab-comms-eval.sh"), *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=constructed_env(
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            HOME=os.environ["HOME"],
            **env,
        ),
    )


def test_a_real_eval_refuses_when_its_claude_cannot_run(tmp_path):
    r = _eval(
        tmp_path, broken_stub("stderr"), "--experiment", "coverage-honesty", "--reps", "1",
        AB_EVAL_REAL="1",
    )
    assert r.returncode == 1, (r.stdout[-800:], r.stderr[-800:])
    assert "could not be measured" in r.stderr
    assert "claude native binary not installed" in r.stderr
    assert "COVERAGE_AB_RESULT" not in r.stdout


def test_a_dry_run_pins_dry_run_whatever_binary_is_installed(tmp_path):
    """A dry run makes no model call, so a runnable claude on PATH is not part of
    its evidence and must not become its pin."""
    binary = _stub(tmp_path, "claude", healthy("2.1.281"))
    pin = _shell_function("ab-comms-eval.sh", "pin_claude_version")
    r = _bash(
        tmp_path, f'{pin}DRY_RUN=1; pin_claude_version; printf "claude=%s" "$CLAUDE_VER"',
        PATH=f"{binary.parent}{os.pathsep}{os.environ['PATH']}",
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-1500:]
    assert "claude=dry-run" in out, out[-1500:]
    assert "claude=2.1.281" not in out


# --- the update job measures the binary on its own seam, before and after -----------


def test_the_update_job_measures_the_fleet_binary_on_its_launch_path(tmp_path, *, scratch_plane_env):
    """Versions no real host has, so the only way the job can report them is by
    measuring the stubs on its launch path (CLAUDE_UPDATE_FLEET_PATH, its seam).
    A job that measured on the real launch PATH instead would report the host's
    own claude, which on a host with one can equal a test's expected version and
    pass by coincidence."""
    from tests.test_update_claude_code_staged import StagedHost

    h = StagedHost(tmp_path, scratch_plane_env=scratch_plane_env, system=healthy("7.7.7"))
    _write_exec(tmp_path / "inplace", healthy("8.8.8"))
    r = h.run(armed=False, NPM_STAGE=tmp_path / "inplace", NPM_STAGE_TARGET=h.system)
    assert r.returncode == 0, (r.stderr, h.log())
    assert "current: 7.7.7" in h.log(), h.log()
    assert "version changed: 7.7.7 → 8.8.8" in h.log(), h.log()


# --- one launch PATH -----------------------------------------------------------------


@pytest.mark.parametrize("homebrew", ["", "/opt/homebrew"])
def test_the_launch_path_is_the_order_start_bot_exported(tmp_path, homebrew):
    """Byte for byte the literal start-bot.sh exported before it moved here, so
    the move changes no bot's PATH. System dirs FIRST (#635)."""
    home = tmp_path / "home"
    expected = (
        f"/usr/local/bin:/usr/bin:/bin:{home}/.local/bin:{home}/.bun/bin:"
        f"{home}/.npm-global/bin" + (f":{homebrew}/bin" if homebrew else "")
    )
    r = _bash(tmp_path, f'_HOMEBREW="{homebrew}"; fleet_launch_path')
    assert r.stdout == expected, r.stderr


def test_start_bot_and_the_update_job_take_the_launch_path_from_the_one_helper():
    order = re.compile(r"/usr/local/bin:/usr/bin:/bin:\$HOME/\.local/bin")
    spelled = sorted(
        p.name
        for directory in (LIB, HARNESS)
        for p in directory.iterdir()
        if p.is_file() and order.search(p.read_text(errors="replace"))
    )
    assert spelled == ["lib-common.sh"], spelled
    assert 'PATH="$(fleet_launch_path)"' in (LIB / "start-bot.sh").read_text()
    assert "fleet_claude_path" in (LIB / "update-claude-code.sh").read_text()


def test_the_composer_prepends_selected_release_to_the_same_tool_path(tmp_path, monkeypatch):
    """The release CLI wins; the remaining tool order matches bot sessions."""
    import claudlobby.composer as composer

    selected = tmp_path / "release" / "bin" / "claudlobby"
    monkeypatch.setattr(composer, "selected_cli", lambda: selected)

    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"; fleet_launch_path'],
        capture_output=True,
        text=True,
        timeout=60,
        env=constructed_env(HOME=str(Path.home())),
    )
    assert r.returncode == 0, r.stderr
    assert composer._scheduler_tool_path() == f"{selected.parent}:{r.stdout}"


def test_a_bare_name_resolves_on_the_launch_path_not_the_callers(tmp_path):
    decoy = tmp_path / "caller-bin"
    launch = tmp_path / "launch-bin"
    for d, v in ((decoy, "1.0.0"), (launch, "2.1.281")):
        d.mkdir()
        _write_exec(d / "claude", healthy(v))
    r = _bash(
        tmp_path,
        f'fleet_claude_path "{launch}"',
        PATH=f"{decoy}{os.pathsep}{os.environ['PATH']}",
    )
    assert r.stdout == str(launch / "claude"), r.stderr


def test_a_pinned_path_is_measured_as_named(tmp_path):
    pinned = _stub(tmp_path, "pinned", healthy("2.1.279"))
    r = _bash(
        tmp_path, f'fleet_claude_path "{tmp_path / "launch-bin"}"', CLAUDE_BIN=pinned
    )
    assert r.stdout == str(pinned)


def test_a_bare_pin_resolves_where_a_pane_would_resolve_it(tmp_path):
    launch = tmp_path / "launch-bin"
    launch.mkdir()
    _write_exec(launch / "claude-beta", healthy("2.2.0"))
    r = _bash(tmp_path, f'fleet_claude_path "{launch}"', CLAUDE_BIN="claude-beta")
    assert r.stdout == str(launch / "claude-beta")


def test_nothing_on_the_launch_path_resolves_to_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _bash(tmp_path, f'p="$(fleet_claude_path "{empty}")"; printf "[%s]" "$p"')
    assert r.stdout == "[]"


# --- one reader ------------------------------------------------------------------------


#: Reads that are CHECKS, not readers: each compares a binary's output with a
#: version it expects and fails closed on anything else, so none can record a
#: stand-in. The staged-update rehearsal's own assertions.
CHECKS_NOT_READERS = {"rehearse-staged-claude-update.sh"}


def test_no_other_script_reads_a_claude_version():
    """A ratchet: the next hand-rolled `--version` read is a failing test, not a
    seventh copy. Behaviour is pinned by the tests above; this stops a new copy."""
    read = re.compile(r'(?:"\$\{?\w+\}?"|(?:^|(?<=[\s(|;&`]))claude)\s+--version\b', re.M)
    offenders = {
        p.name
        for directory in (LIB, HARNESS)
        for p in directory.iterdir()
        if p.is_file()
        and p.suffix in {".sh", ""}
        and read.search(p.read_text(errors="replace"))
    }
    assert offenders - CHECKS_NOT_READERS == {"lib-common.sh"}, sorted(offenders)
    # ...and in lib-common, only inside measure_claude_version.
    lc = (LIB / "lib-common.sh").read_text()
    body = _shell_function("lib-common.sh", "measure_claude_version")
    assert len(read.findall(lc)) == len(read.findall(body)) > 0
    py = [
        p.relative_to(REPO)
        for p in (REPO / "claudlobby").rglob("*.py")
        # Runtime scripts are covered above and build copies of assets retain
        # their source owners; this half checks the Python implementation package.
        if p.relative_to(REPO / "claudlobby").parts[0] not in {"_runtime_scripts", "_resources"}
        # Both entries parse Claudlobby's own --version option; neither reads
        # the Claude binary's version. The stable launcher must route it too.
        and '"--version"' in p.read_text() and p.name not in {"__main__.py", "installation.py"}
    ]
    assert py == [], py


def _version_tool_path(tmp_path):
    """Expose the real timeout utility only on a private composed tool path."""
    import shutil
    timeout = shutil.which("timeout") or shutil.which("gtimeout")
    if timeout is None:
        pytest.skip("real timeout/gtimeout is required for bounded version measurement")
    tools = tmp_path / "composed-tools"
    tools.mkdir()
    (tools / "timeout").symlink_to(Path(timeout).resolve())
    return str(tools) + ":/usr/bin:/bin"


def test_explicit_launch_path_bounds_the_actual_version_door_under_minimal_updater_path(tmp_path, monkeypatch):
    from claudlobby import claude_version
    canonical = _version_tool_path(tmp_path)
    binary = _stub(tmp_path, "configured-private-provider", healthy("2.1.318"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = claude_version.measure(_paths(tmp_path), str(binary), launch_path=canonical)
    assert (result.version, result.why) == ("2.1.318", None)
    assert os.environ["PATH"] == "/usr/bin:/bin"


def _managed_shell_context(tmp_path):
    home = tmp_path / "home"
    family = home / ".local/share/claudlobby-browser"
    python = family / "runtime/profile/python/bin/python3.12"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    return {"HOME": home, "CLAUDLOBBY_ROOT": family / "data",
            "CLAUDLOBBY_INSTALLATION": family / "install",
            "CLAUDLOBBY_NATIVE_PYTHON": python}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_managed_python_bounds_existing_version_door(tmp_path, shape):
    context = _managed_shell_context(tmp_path)
    binary = _stub(tmp_path, shape, SHAPES[shape][0])
    code = f'_OS=Darwin; _TIMEOUT_BIN=""; if measure_claude_version "{binary}"; then printf "V=%s" "$CLAUDE_VERSION"; else printf "W=%s" "$CLAUDE_VERSION_WHY"; fi'
    r = _run(tmp_path, ["bash", "-c", f'. "{LIB}/lib-common.sh"; {code}'], **context)
    verdict = SHAPES[shape][1]
    assert r.returncode == 0, r.stderr
    assert r.stdout == "V=" + verdict if re.fullmatch(VERSION_PATTERN, verdict) else verdict in r.stdout


def test_managed_python_closes_stdin_kills_descendants_and_caps_output(tmp_path):
    context = _managed_shell_context(tmp_path)
    marker = tmp_path / "late-child"
    term_ignoring = tmp_path / "term-ignoring-child"
    hangs = _stub(tmp_path, "hangs-child", f"""#!/bin/bash
(sleep 2; touch "{marker}") &
(trap "" TERM; sleep 2; touch "{term_ignoring}") </dev/null >/dev/null 2>&1 &
read -r data && exit 9
sleep 30
""")
    prefix = f'. "{LIB}/lib-common.sh"; _OS=Darwin; _TIMEOUT_BIN=""; '
    eof = _stub(tmp_path, "stdin-closed", '#!/bin/bash\nif read -r data; then exit 8; fi\necho 2.1.281\n')
    r = _run(tmp_path, ["bash", "-c", prefix + f'printf "input\\n" | measure_claude_version "{eof}"; printf "%s" "$?"'], **context)
    assert r.stdout == "0", (r.stdout, r.stderr)
    started = time.monotonic()
    r = _run(tmp_path, ["bash", "-c", prefix + f'measure_claude_version "{hangs}" || printf "%s" "$CLAUDE_VERSION_WHY"'], CLAUDE_VERSION_TIMEOUT_S="0.2", **context)
    assert "did not finish within 0.2s" in r.stdout, (r.stdout, r.stderr)
    assert time.monotonic() - started < 4
    time.sleep(2.2)
    assert not marker.exists()
    assert not term_ignoring.exists()
    flood = _stub(tmp_path, "flood", '#!/bin/bash\nwhile :; do printf "%01000d" 0; printf "%01000d" 0 >&2; done\n')
    r = _run(tmp_path, ["bash", "-c", prefix + f'measure_claude_version "{flood}" || printf "%s" "$CLAUDE_VERSION_WHY"'], CLAUDE_VERSION_TIMEOUT_S="2", _timeout=8, **context)
    assert "exited 125" in r.stdout, (r.stdout, r.stderr)
    assert len(r.stdout) < 600


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGKILL"])
@pytest.mark.parametrize("platform, checks, measured", [
    pytest.param("darwin", "denied,gone", True, id="darwin-group-disappears"),
    pytest.param("darwin", "present", False, id="darwin-group-still-present"),
    pytest.param("darwin", "denied", False, id="darwin-check-stays-denied"),
    pytest.param("darwin", "unknown", False, id="darwin-check-returns-another-error"),
    pytest.param("linux", "gone", False, id="linux-preserves-original-permission-error"),
])
def test_managed_version_cleanup_requires_confirmation_the_group_is_gone(tmp_path, signal_name, platform, checks, measured):
    probe = _managed_probe()
    binary = _stub(tmp_path, "healthy", healthy("2.1.318"))
    # The provider exits normally. Simulate the kernel response to one cleanup signal.
    scenario = """
import errno, itertools, os, selectors, signal, subprocess, sys
# Load the native process and selector modules before overriding the platform flag.
probe, signal_name, platform, sequence = sys.argv[1:5]
sys.argv = ["-c", *sys.argv[5:]]
sys.platform = platform
outcomes = sequence.split(",")
answers = itertools.chain(outcomes, itertools.repeat(outcomes[-1]))
def fake_killpg(pid, sig):
    if sig == getattr(signal, signal_name):
        raise PermissionError(errno.EPERM, "original group error")
    if sig == signal.SIGTERM:
        return  # Reach the SIGKILL case after a successful SIGTERM.
    if sig == signal.SIGKILL:
        raise AssertionError("signalled a group already confirmed gone")
    if sig == 0:
        outcome = next(answers)
        if outcome == "gone":
            raise ProcessLookupError(errno.ESRCH, "group exited")
        if outcome == "denied":
            raise PermissionError(errno.EPERM, "group check denied")
        if outcome == "unknown":
            raise OSError(errno.EINVAL, "unknown group check result")
        return
    raise AssertionError(f"unexpected signal {sig}")
os.killpg = fake_killpg
exec(probe)
"""
    result = _run(tmp_path, [sys.executable, "-I", "-S", "-c", scenario,
                             probe, signal_name, platform, checks, "2", str(binary)], _timeout=5)
    if measured:
        assert result.returncode == 0, result.stderr
        assert "2.1.318" in result.stdout
    else:
        assert result.returncode != 0
        assert "original group error" in result.stderr


def test_managed_version_timeout_reaps_a_late_leader(tmp_path):
    probe = _managed_probe()
    binary = _stub(tmp_path, "wait-for-termination", "#!/bin/bash\nexec sleep 30\n")
    # Use a real timed-out process. Inject Darwin's refused signal on either CI
    # platform, and require the actual leader to be reaped before accepting ESRCH.
    scenario = """
import errno, os, selectors, signal, subprocess, sys
probe = sys.argv.pop(1)
sys.platform = "darwin"
poll, killpg = subprocess.Popen.poll, os.killpg
leader = None
def defer_first_poll(process):
    global leader
    leader = process
    subprocess.Popen.poll = poll
    return None
def observe_killpg(pid, sig):
    if sig == signal.SIGKILL:
        assert leader is not None and leader.pid == pid and leader.returncode is None
        raise PermissionError(errno.EPERM, "group is still exiting")
    if sig == 0:
        if leader.returncode is None:
            return
        print("checked group after reaping leader", file=sys.stderr)
    return killpg(pid, sig)
subprocess.Popen.poll = defer_first_poll
os.killpg = observe_killpg
exec(probe)
"""
    result = _run(tmp_path, [sys.executable, "-I", "-S", "-c", scenario, probe, "0.2", str(binary)], _timeout=5)
    assert result.returncode == 124, result.stderr
    assert "checked group after reaping leader" in result.stderr


def test_managed_context_propagates_without_changing_provider_precedence(tmp_path, monkeypatch):
    import plistlib
    import claudlobby.composer as composer
    import claudlobby.installation as installation
    from claudlobby.config import BotConfig, FleetConfig
    from claudlobby.paths import Paths
    from tests.package_fixtures import git_package
    context = _managed_shell_context(tmp_path)
    profile = Path(context["CLAUDLOBBY_NATIVE_PYTHON"]).parents[2]
    managed = {"HOME": str(context["HOME"]), "PATH": f"{profile}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
               "CLAUDLOBBY_NATIVE_PYTHON": str(context["CLAUDLOBBY_NATIVE_PYTHON"]),
               "TMUX_BIN": str(profile / "tmux/bin/tmux"), "GIT_CONFIG_GLOBAL": "/dev/null"}
    monkeypatch.setattr(installation, "managed_browser_environment", lambda selected: managed.copy())
    root = Path(context["CLAUDLOBBY_ROOT"])
    paths = Paths(root=root, package=git_package(root))
    bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
    fleet = FleetConfig(name="fixture", service_prefix="fixture", manager="worker", bots={"worker": bot})
    env = plistlib.loads(composer.compose_launchd_plist(bot, fleet, paths).encode())["EnvironmentVariables"]
    assert env["TMUX_BIN"] == managed["TMUX_BIN"]
    assert env["PATH"].split(":")[0] == str(profile / "bin")
    conf = composer.compose_bot_conf(bot, fleet, paths)
    for key in ("TMUX_BIN", "CLAUDLOBBY_NATIVE_PYTHON", "GIT_CONFIG_GLOBAL"):
        assert f"export {key}=" in conf
    timer = tmp_path / "timers"; timer.mkdir()
    composer._write_timer_units(timer, "fixture.probe", "probe", {"type": "interval", "seconds": 60, "startup": 10},
                                "$CLAUDLOBBY_NATIVE_DIR/keepalive-all.sh", "oneshot", "fixture", paths)
    timer_env = plistlib.loads((timer / "fixture.probe.plist").read_bytes())["EnvironmentVariables"]
    assert timer_env["TMUX_BIN"] == managed["TMUX_BIN"]
    assert timer_env["CLAUDLOBBY_NATIVE_PYTHON"] == managed["CLAUDLOBBY_NATIVE_PYTHON"]
    assert timer_env["PATH"].split(":")[:2] == [str(paths.package.installation.cli.parent), str(profile / "bin")]
    r = _run(tmp_path, ["bash", "-c", f'. "{LIB}/lib-common.sh"; _OS=Darwin; fleet_launch_path'], **context)
    assert r.stdout.startswith(str(profile / "bin") + ":/usr/local/bin:/usr/bin:/bin:")
    # A context drift must not admit a guessed profile or its Python bounder.
    context["CLAUDLOBBY_INSTALLATION"] = tmp_path / "other-install"
    r = _run(tmp_path, ["bash", "-c", f'. "{LIB}/lib-common.sh"; _OS=Darwin; _TIMEOUT_BIN=""; measure_claude_version /must-not-exec || printf "%s" "$CLAUDE_VERSION_WHY"'], **context)
    assert "unavailable" in r.stdout


def test_managed_current_reentry_keeps_python_and_installed_cli(tmp_path, monkeypatch):
    import claudlobby.active_config as active
    import claudlobby.installation as policy
    import claudlobby.runtime_admission as admission
    from claudlobby.context import native_environment
    from claudlobby.paths import Paths
    from tests.package_fixtures import git_package
    context = _managed_shell_context(tmp_path)
    root = Path(context["CLAUDLOBBY_ROOT"])
    root.mkdir(parents=True)
    package = git_package(root)
    _write_exec(package.installation.cli, '#!/bin/sh\nprintf "installed-cli"\n')
    paths = Paths(root, package=package)
    managed = {"HOME": str(context["HOME"]), "PATH": str(Path(context["CLAUDLOBBY_NATIVE_PYTHON"]).parents[2] / "bin") + ":/usr/bin:/bin",
               "CLAUDLOBBY_NATIVE_PYTHON": str(context["CLAUDLOBBY_NATIVE_PYTHON"])}
    monkeypatch.setattr(policy, "managed_browser_environment", lambda installation: managed.copy())
    monkeypatch.setattr(active, "runtime_read_stamp", lambda *args, **kwargs: object())
    monkeypatch.setattr(admission, "runtime_route_matches", lambda *args, **kwargs: True)
    current = admission.GitCurrentRuntime(package.installation, "a" * 40, "fixture", {})
    env = native_environment(paths, current=current)
    assert env["CLAUDLOBBY_NATIVE_PYTHON"] == managed["CLAUDLOBBY_NATIVE_PYTHON"]
    assert env["CLAUDLOBBY_NATIVE_ADMITTED"] == "1"
    assert env["CLAUDLOBBY_CLI"] == str(package.installation.cli)
    r = _run(tmp_path, ["bash", "-c", f'. "{LIB}/lib-common.sh"; claudlobby_cli --fixture'], **env)
    assert (r.returncode, r.stdout) == (0, "installed-cli"), r.stderr
    monkeypatch.setattr(policy, "managed_browser_environment", lambda installation: {"CLAUDLOBBY_ROOT": "/invented"})
    with pytest.raises(ValueError, match="conflicts with native identity"):
        native_environment(paths, current=current)


def test_managed_tools_do_not_select_runtime_emission_policy(tmp_path, monkeypatch):
    from claudlobby.context import native_environment
    from claudlobby.paths import Paths
    from tests.package_fixtures import git_package
    monkeypatch.setattr("claudlobby.installation.managed_browser_environment",
                        lambda installation: {"PLANE_EMIT_DISABLED": "1", "TMUX_BIN": "/fixed/tmux"})
    env = native_environment(Paths(tmp_path, package=git_package(tmp_path)))
    assert env["TMUX_BIN"] == "/fixed/tmux"
    assert "PLANE_EMIT_DISABLED" not in env


def test_the_python_door_measures_a_browser_installations_pinned_provider(
    tmp_path, monkeypatch
):
    """start-bot's `_native-session launch` (require_provider) measures through
    this door in a BUILT env under the composed launch PATH, before any pane
    exists. The browser installation's pin is found from the installation the
    paths carry, and a claude on that launch PATH is never measured instead."""
    from claudlobby import claude_version
    from claudlobby.installation import Installation
    from claudlobby.paths import Paths
    from tests.package_fixtures import source_package

    home = tmp_path / "home"
    family = home / ".local/share/claudlobby-browser"
    root = family / "data"
    root.mkdir(parents=True)
    (family / "install").mkdir()
    (family / "provider-bin").mkdir()
    _write_exec(family / "provider-bin" / "claude", healthy("2.1.325"))
    launch = tmp_path / "launch-bin"
    launch.mkdir()
    _write_exec(launch / "claude", healthy("9.9.9"))
    tools = _version_tool_path(tmp_path)
    package = replace(
        source_package(),
        native=LIB,
        installation=Installation(family / "install", root),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    result = claude_version.measure(
        Paths(root=root, package=package), launch_path=f"{launch}:{tools}"
    )
    assert (result.version, result.why) == ("2.1.325", None)
