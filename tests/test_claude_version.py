"""One reader of the Claude Code version, and one launch PATH (#1772).

`measure_claude_version` (lib-common) is the only thing that reads a version:
the binary RAN and the first line of its stdout carries X.Y.Z, or it is
could-not-measure with a reason. Every consumer goes through it: the update
job, the query door `lib/claude-version.sh` and so the registry scan, the eval's
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
import subprocess
from pathlib import Path

import pytest

from claudlobby.claude_version import VERSION_PATTERN
from tests.conftest import _write_exec, constructed_env
from tests.test_update_claude_code_verify import broken_stub, healthy

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
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


def _run(tmp_path: Path, argv: list[str], **env) -> subprocess.CompletedProcess:
    """argv in a constructed env, with a throwaway HOME and CLAUDLOBBY_ROOT."""
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        env=constructed_env(HOME=tmp_path / "home", CLAUDLOBBY_ROOT=root, **env),
    )


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
    r = _door(tmp_path, str(hangs), CLAUDE_VERSION_TIMEOUT_S="1")
    assert (r.returncode, r.stdout) == (3, ""), r.stderr
    assert "did not finish within 1s" in r.stderr, r.stderr


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
    from claudlobby.paths import Paths

    root = tmp_path / "proot"
    root.mkdir(exist_ok=True)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(lib)
    return Paths(root=root)


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


def test_every_seed_caller_stops_on_the_refusal():
    """A refusal a caller ignores is a stand-in by another route: the ladder runs
    without `set -e`, and would boot on with no onboarding seeded."""
    callers = []
    for script in sorted(LIB.glob("*.sh")):
        text = script.read_text()
        for m in re.finditer(r"^\s*seed_claude_auth_and_trust .*$", text, re.M):
            callers.append(script.name)
            tail = text[m.start() : m.start() + 400]
            assert re.search(r"\\\n\s*\|\| (?:\{[^}]*exit [0-9]; \}|die )", tail), (
                f"{script.name} ignores the seed's refusal"
            )
    assert sorted(set(callers)) == [
        "ab-comms-eval.sh",
        "boot-strand-sampler.sh",
        "freshbox-boot-gate.sh",
        "rehearse-permissions-ladder.sh",
    ]


def test_the_send_size_probe_refuses_before_building_anything(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    r = subprocess.run(
        ["bash", str(LIB / "send-size-probe.sh"), "--n", "1"],
        capture_output=True,
        text=True,
        timeout=120,
        env=constructed_env(
            SEND_PROBE_REAL="1",
            CLAUDE_BIN=_stub(tmp_path, "broken", broken_stub("stderr")),
            HOME=tmp_path / "home",
            TMPDIR=tmp,
        ),
    )
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "cannot run" in r.stderr and "claude native binary not installed" in r.stderr
    assert list(tmp.iterdir()) == [], "the probe built scratch state before refusing"


def _eval(tmp_path, claude: str, *args, **env):
    """The eval harness, with `claude` on its PATH the one a real cell would run."""
    bindir = tmp_path / "evalbin"
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / "claude", claude)
    return subprocess.run(
        ["bash", str(LIB / "ab-comms-eval.sh"), *args],
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
    r = _eval(
        tmp_path, healthy("2.1.281"), "--dry-run", "--experiment", "coverage-honesty",
        "--reps", "1",
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-1500:]
    assert "claude=dry-run" in out, out[-1500:]
    assert "claude=2.1.281" not in out


# --- the update job measures the binary on its own seam, before and after -----------


def test_the_update_job_measures_the_fleet_binary_on_its_launch_path(tmp_path):
    """Versions no real host has, so the only way the job can report them is by
    measuring the stubs on its launch path (CLAUDE_UPDATE_FLEET_PATH, its seam).
    A job that measured on the real launch PATH instead would report the host's
    own claude, which on a host with one can equal a test's expected version and
    pass by coincidence."""
    from tests.test_update_claude_code_staged import StagedHost

    h = StagedHost(tmp_path, system=healthy("7.7.7"))
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
        for p in LIB.iterdir()
        if p.is_file() and order.search(p.read_text(errors="replace"))
    )
    assert spelled == ["lib-common.sh"], spelled
    assert 'PATH="$(fleet_launch_path)"' in (LIB / "start-bot.sh").read_text()
    assert "fleet_claude_path" in (LIB / "update-claude-code.sh").read_text()


def test_the_composer_puts_timers_on_the_same_launch_path():
    """Composed timer units carry the composer's Python spelling of the order
    (reload-fleet's `claude plugin update` runs under it), so it is pinned to the
    bash one byte for byte: a timer and a pane must resolve the same claude."""
    from claudlobby.composer import _scheduler_tool_path

    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"; fleet_launch_path'],
        capture_output=True,
        text=True,
        timeout=60,
        env=constructed_env(HOME=str(Path.home())),
    )
    assert r.returncode == 0, r.stderr
    assert _scheduler_tool_path() == r.stdout


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
        for p in LIB.iterdir()
        if p.is_file()
        and p.suffix in {".sh", ""}
        and read.search(p.read_text(errors="replace"))
    }
    assert offenders - CHECKS_NOT_READERS == {"lib-common.sh"}, sorted(offenders)
    # ...and in lib-common, only inside measure_claude_version.
    lc = (LIB / "lib-common.sh").read_text()
    body = lc[lc.index("measure_claude_version() {") :]
    body = body[: body.index("\n}\n")]
    assert len(read.findall(lc)) == len(read.findall(body)) > 0
    py = [
        p.relative_to(REPO)
        for p in (REPO / "claudlobby").rglob("*.py")
        if '"--version"' in p.read_text() and p.name != "__main__.py"
    ]
    assert py == [], py
