"""memory-thrash.py: the swap thrash rule the per-minute host probe runs (#1645 P2).

The replay cases run the script over the week of swap-out counters the probe
recorded on a Raspberry Pi 5 host before and after its 2026-11-07 reset
(tests/fixtures/memory-thrash/swap-out-week.txt), written as `claudlobby --json
plane samples host.swap_pages` prints them. Others drive `check` one reading at a
time through its clock seam (MEMORY_THRASH_NOW), and `bots` over a /proc and bots
dirs of their own. The probe cases run the real plane-host-probe.sh over a /proc of their own
(HOST_PROBE_PROC), with the real alert path: a private Plane daemon records the
alert, and a stub curl stands in for Telegram.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import _write_exec, constructed_env, load_lib_module, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "claudlobby/_runtime_scripts/memory-thrash.py"
PROBE = REPO / "claudlobby/_runtime_scripts/plane-host-probe.sh"
WEEK = REPO / "tests/fixtures/memory-thrash/swap-out-week.txt"
APP = "/user.slice/user-1000.slice/user@1000.service/app.slice"
RULE = "line_pages = 100\nminutes = 2\nline_pages_max = 1000\nminutes_max = 3\n"
HOST_RULE = "line_pages = 100000\nminutes = 2\nline_pages_max = 150000\nminutes_max = 3\n"
BAD_RULE = RULE.replace("minutes = 2", "minutes = 1.5")


def _week() -> list[tuple[float, int]]:
    """(epoch, counter) per sample, decoded from the fixture's deltas."""
    rows, at, out = [], None, None
    for line in WEEK.read_text().splitlines():
        if line.startswith("#"):
            continue
        a, b = (int(field) for field in line.split())
        at, out = (a, b) if at is None else (at + a, out + b)
        rows.append((at / 100.0, out))
    return rows


@pytest.fixture(scope="module")
def week_samples(tmp_path_factory) -> Path:
    """The week as `plane samples --json` prints it, written once for the module."""
    path = tmp_path_factory.mktemp("week") / "samples.json"
    samples = [{"occurred_at": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                "status": None, "value": {"in": 0, "out": out}} for at, out in _week()]
    path.write_text(json.dumps({"schema": 1, "ok": True, "data": {
        "kind": "host", "metric": "host.swap_pages", "samples": samples}}))
    return path


def _run(*args: str, check=True, now=None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_THRASH_")}
    if now is not None:
        env["MEMORY_THRASH_NOW"] = str(now)
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), *args],
                            capture_output=True, text=True, timeout=60, env=env)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _replay(tmp_path, samples: Path, lines: str = HOST_RULE, *extra: str) -> list[str]:
    path = tmp_path / "lines"
    path.write_text(lines)
    return _run("replay", "--samples", str(samples), "--lines", str(path), *extra).stdout.splitlines()


def test_the_week_fires_once_two_minutes_before_the_reset(tmp_path, week_samples):
    # The fixture keeps each time to a hundredth of a second, so its two rates sit
    # within 0.02% of the 154,188 and 193,892 the plane's own times give.
    lines = _replay(tmp_path, week_samples)
    assert lines[:-1] == ["alert\t2026-11-07T04:36:48Z\t154209\t193875"]
    assert lines[-1] == ("1 alerts over 8382 samples, 2026-11-02T22:46:42Z to 2026-11-09T20:59:49Z,"
                         " line 100000 pages a minute for 2 readings")


def test_a_one_minute_rule_fires_on_four_episodes(tmp_path, week_samples):
    # The control: the two single minutes over the line, the window's first pair
    # (two readings 16 s apart after a two-hour gap) and the run before the reset.
    alerts = [line.split("\t")[1] for line in _replay(tmp_path, week_samples, HOST_RULE,
                                                      "--minutes", "1")[:-1]]
    assert alerts == ["2026-11-02T22:46:58Z", "2026-11-06T02:45:48Z",
                      "2026-11-06T07:04:48Z", "2026-11-07T04:35:48Z"]


def test_the_week_stays_quiet_with_the_line_over_its_highest_minute(tmp_path, week_samples):
    rule = HOST_RULE.replace("100000", "500000").replace("150000", "500000")
    assert _replay(tmp_path, week_samples, rule)[:-1] == []


def test_replay_needs_a_rule(tmp_path, week_samples):
    path = tmp_path / "lines"
    path.write_text("alert = off\n")
    off = _run("replay", "--samples", str(week_samples), "--lines", str(path), check=False)
    assert off.returncode == 2 and "no rule to replay: the alert is off" in off.stderr
    path.write_text(BAD_RULE)
    bad = _run("replay", "--samples", str(week_samples), "--lines", str(path), check=False)
    assert bad.returncode == 2 and f"no rule to replay: refused: {path} line 2: " in bad.stderr


def _check(state: Path, at: float, out=None, skip=None, boot="b1", lines=RULE):
    state.mkdir(exist_ok=True)
    if lines is not None and not (state / "lines").exists():
        (state / "lines").write_text(lines)
    given = ["--skip", skip] if skip is not None else ["--out", str(out)]
    return _run("check", "--state-dir", str(state), "--boot-id", boot, *given, now=at)


def _fields(result) -> list[str]:
    return result.stdout.rstrip("\n").split("\t")


def _verdicts(state: Path, readings, **kw) -> list[list[str]]:
    """Each reading is (seconds, pages swapped out since the reading before it)."""
    out, verdicts = 0, []
    for at, pages in readings:
        out += pages
        verdicts.append(_fields(_check(state, at, out, **kw)))
    return verdicts


def test_a_second_episode_fires_again(tmp_path):
    state = tmp_path / "state"
    v = _verdicts(state, [(0, 0), (60, 500), (120, 500), (180, 500), (240, 50), (300, 500),
                          (360, 500)])
    assert [x[0] for x in v] == ["baseline", "over", "alert", "alert", "ended", "over", "alert"]
    assert v[2] == ["alert", "1970-01-01T00:01:00Z",
                    "swap-out over 100 pages a minute for 2 readings in a row (500, 500)"]
    assert v[4] == ["ended", "1970-01-01T00:01:00Z", "3", "500", "a reading under the line"]
    assert v[6][1] == "1970-01-01T00:05:00Z"


def test_an_alert_repeats_until_a_channel_took_it(tmp_path):
    state = tmp_path / "state"
    episode = _verdicts(state, [(0, 0), (60, 500), (120, 500)])[-1][1]
    assert _run("delivered", "--state-dir", str(state), "--episode", "1970-01-01T00:09:00Z",
                check=False).returncode == 1
    assert _run("delivered", "--state-dir", str(state), "--episode", episode).returncode == 0
    assert _fields(_check(state, 180, 1500)) == ["alerted", episode, "500"]


def test_a_rate_equal_to_the_line_is_not_over_it(tmp_path):
    v = _verdicts(tmp_path / "state", [(0, 0), (60, 100), (120, 100), (180, 100)])
    assert [x[0] for x in v] == ["baseline", "under", "under", "under"]


def test_the_rate_is_per_minute_of_the_time_between_readings(tmp_path):
    # 150 pages over 120 s is 75 a minute, under a line of 100; 150 over 30 s is 300.
    v = _verdicts(tmp_path / "state", [(0, 0), (120, 150), (150, 150)])
    assert v[1] == ["under", "75"] and v[2] == ["over", "300", "1"]


@pytest.mark.parametrize("boot, out, why", [("b2", 1000, "a new boot"),
                                            ("b1", 10, "the counter went back")])
def test_a_new_boot_or_a_counter_that_went_back_starts_again(tmp_path, boot, out, why):
    state = tmp_path / "state"
    _verdicts(state, [(0, 0), (60, 500)])
    assert _fields(_check(state, 120, out, boot=boot)) == ["baseline", why]
    # the run before it is gone: the next minute over the line is the first again
    assert _fields(_check(state, 180, out + 500, boot=boot)) == ["over", "500", "1"]


def test_an_episode_a_reset_ends_is_its_end(tmp_path):
    state = tmp_path / "state"
    _verdicts(state, [(0, 0), (60, 500), (120, 500)])
    ended = _check(state, 180, 5, boot="b2")
    assert _fields(ended) == ["ended", "1970-01-01T00:01:00Z", "2", "500", "a new boot"]
    assert ("episode 1970-01-01T00:01:00Z ended after 2 readings over the line, peak 500 pages"
            " a minute: a new boot") in ended.stderr


def test_a_missing_counter_is_skipped_and_said_once_per_reason(tmp_path):
    state = tmp_path / "state"
    first = _check(state, 0, skip="no /proc/vmstat (macOS)")
    again = _check(state, 60, skip="no /proc/vmstat (macOS)")
    assert first.stdout == again.stdout == "skipped\tno /proc/vmstat (macOS)\n"
    assert "skipped: no /proc/vmstat (macOS)" in first.stderr and again.stderr == ""
    other = _check(state, 120, skip="no pswpout number in /proc/vmstat")
    assert "skipped: no pswpout number in /proc/vmstat" in other.stderr


def test_a_skipped_reading_keeps_the_run(tmp_path):
    state = tmp_path / "state"
    _verdicts(state, [(0, 0), (60, 500)])
    _check(state, 120, skip="no pswpout number in /proc/vmstat")
    assert _fields(_check(state, 180, 1500))[0] == "alert"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only directory")
def test_a_state_dir_it_cannot_write_is_a_skip_it_says(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _run("check", "--state-dir", str(locked / "state"), "--out", "0", now=0)
    finally:
        locked.chmod(0o700)
    assert result.stdout.startswith(f"skipped\tcannot keep the state in {locked / 'state'}: ")
    assert "memory-thrash: skipped: cannot keep the state in" in result.stderr


def test_a_check_says_an_episode_on_its_journal(tmp_path):
    state = tmp_path / "state"
    _verdicts(state, [(0, 0), (60, 500)])
    said = _check(state, 120, 1000).stderr
    assert ("episode 1970-01-01T00:01:00Z: swap-out over 100 pages a minute for 2 readings in a"
            " row (500, 500): alert") in said


@pytest.mark.parametrize("lines, why", [
    (None, "no line for this host: {f} does not exist"),
    (RULE + "alert = off\n", "the alert is off: {f} says alert = off"),
    ("alert = off\n", "the alert is off: {f} says alert = off"),
])
def test_no_file_or_alert_off_is_a_skip_said_once(tmp_path, lines, why):
    state = tmp_path / "state"
    state.mkdir()
    if lines is not None:
        (state / "lines").write_text(lines)
    first, again = _check(state, 0, 0, lines=None), _check(state, 60, 0, lines=None)
    expected = why.format(f=state / "lines")
    assert first.stdout == again.stdout == f"skipped\t{expected}\n"
    assert f"skipped: {expected}" in first.stderr and again.stderr == ""


@pytest.mark.parametrize("lines, why", [
    (RULE + "alert = on\n", "{f} line 5: alert takes one value, off"),
    (RULE + "line = 5\n", "{f} line 5 names no line (the names: line_pages, minutes,"
                          " line_pages_max, minutes_max, alert)"),
    (BAD_RULE, "{f} line 2: minutes needs a positive whole number"),
    (RULE.replace("line_pages = 100", "line_pages = 0"), "{f} line 1: line_pages needs a positive number"),
    (RULE.replace("minutes_max = 3\n", ""), "{f} sets no minutes_max"),
    (RULE.replace("line_pages = 100", "line_pages = 1000000"),
     "{f}: line_pages 1e+06 is over line_pages_max 1000"),
    (RULE.replace("minutes = 2", "minutes = 5"), "{f}: minutes 5 is over minutes_max 3"),
    ("", "{f} sets no line_pages, minutes, line_pages_max, minutes_max"),
])
def test_a_file_that_gives_no_rule_is_a_refusal_for_the_probe_to_page(tmp_path, lines, why):
    state = tmp_path / "state"
    state.mkdir()
    (state / "lines").write_text(lines)
    result = _check(state, 0, 0, lines=None)
    expected = why.format(f=state / "lines")
    assert result.stdout == f"refused\t1970-01-01T00:00:00Z\t{expected}\n"
    assert f"skipped: refused: {expected}" in result.stderr


def test_an_unreadable_file_is_a_refusal(tmp_path):
    state = tmp_path / "state"
    (state / "lines").mkdir(parents=True)
    kind, _since, why = _fields(_check(state, 0, 0, lines=None))
    assert kind == "refused" and why.startswith(f"{state / 'lines'} is unreadable: ")


def test_a_refusal_is_raised_until_a_channel_took_it_and_again_after_the_next_bad_edit(tmp_path):
    state = tmp_path / "state"
    lines = state / "lines"
    _verdicts(state, [(0, 0)])
    lines.write_text(BAD_RULE)
    first = _check(state, 60, 0, lines=None)
    again = _check(state, 120, 0, lines=None)
    assert _fields(first)[:2] == ["refused", "1970-01-01T00:01:00Z"] and _fields(again) == _fields(first)
    assert again.stderr == ""
    assert _run("delivered", "--state-dir", str(state), "--refused", "1970-01-01T00:09:00Z",
                check=False).returncode == 1
    assert _run("delivered", "--state-dir", str(state), "--refused", "1970-01-01T00:01:00Z").returncode == 0
    assert _fields(_check(state, 180, 0, lines=None)) == [
        "skipped", f"refused: {lines} line 2: minutes needs a positive whole number"]
    lines.unlink()
    assert _fields(_check(state, 240, 0, lines=None)) == [
        "skipped", f"no line for this host: {lines} does not exist"]
    lines.write_text(BAD_RULE)
    assert _fields(_check(state, 300, 0, lines=None))[:2] == ["refused", "1970-01-01T00:05:00Z"]
    lines.write_text(RULE)
    assert _fields(_check(state, 360, 0, lines=None)) == ["under", "0"]
    lines.write_text(BAD_RULE)
    assert _fields(_check(state, 420, 0, lines=None))[:2] == ["refused", "1970-01-01T00:07:00Z"]


def test_a_comment_and_blank_lines_are_read_past(tmp_path):
    state = tmp_path / "state"
    _check(state, 0, 0, lines="# this host's line\n\n" + RULE.replace("= 2", "= 2   # two minutes"))
    assert _fields(_check(state, 60, 500)) == ["over", "500", "1"]


def test_status_prints_the_rule_and_the_state(tmp_path):
    state = tmp_path / "state"
    _verdicts(state, [(0, 0), (60, 500)])
    status = json.loads(_run("status", "--state-dir", str(state)).stdout)
    assert status["rule"] == {"line_pages": 100.0, "minutes": 2}
    assert status["state"]["run"]["count"] == 1 and status["state"]["last"]["out"] == 500
    (state / "lines").write_text(BAD_RULE)
    refused = json.loads(_run("status", "--state-dir", str(state)).stdout)["rule"]
    assert refused == f"refused: {state / 'lines'} line 2: minutes needs a positive whole number"


class FakeProc:
    """A /proc with this user's processes: stat, exe, cgroup and smaps_rollup."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        (root / "uptime").write_text("100000.00 1.00\n")

    def add(self, pid, ppid, cgroup, pss_kib, swap_kib=0, rollup=True):
        d = self.root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (node) S {ppid} " + " ".join(["0"] * 17) + " 100 0 0\n")
        os.symlink("/usr/bin/node", d / "exe")
        (d / "comm").write_text("node\n")
        (d / "cgroup").write_text(f"0::{cgroup}\n")
        if rollup:
            (d / "smaps_rollup").write_text(
                f"Rss: {pss_kib * 2} kB\nPss: {pss_kib} kB\nSwap: {swap_kib * 2} kB\n"
                f"SwapPss: {swap_kib} kB\n")
        else:
            (d / "status").write_text(f"Name:\tnode\nVmRSS:\t{pss_kib} kB\nVmSwap:\t{swap_kib} kB\n")


def _bots_dir(root: Path, confs: dict) -> Path:
    """A bots dir whose bots have the bot.conf text given (None: no bot.conf)."""
    bots = root / "bots"
    for name, text in confs.items():
        (bots / name).mkdir(parents=True)
        if text is not None:
            (bots / name / "bot.conf").write_text(text)
    return bots


def test_bots_names_the_largest_bots_and_what_no_bot_holds(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(900, 1, f"{APP}/com.example.alpha.service", 1024 * 1024, 512 * 1024)
    proc.add(901, 900, f"{APP}/com.example.alpha.service", 512 * 1024)
    # a render the bot started, in a scope of its own, is the bot's by parent link
    proc.add(902, 901, f"{APP}/heavy-render-1.scope", 256 * 1024)
    proc.add(910, 1, f"{APP}/com.example.beta.service", 300 * 1024, 100 * 1024, rollup=False)
    proc.add(920, 1, f"{APP}/com.example.gamma.service", 10 * 1024)
    proc.add(930, 1, f"{APP}/plane-daemon.service", 2048 * 1024, 1024 * 1024)
    bots = _bots_dir(tmp_path, {
        "alpha": "BOT_NAME=alpha\nBOT_SERVICE=com.example.alpha\nexport FLEET_NAME=fleet\n",
        "beta": "export BOT_SERVICE='com.example.beta'\nexport FLEET_NAME=\"fleet\"\n",
        "gamma": "BOT_SERVICE=com.example.gamma   # its unit\nexport FLEET_NAME=fleet\n",
        "nounit": "export FLEET_NAME=fleet\n", "noconf": None})
    out = _run("bots", "--proc", str(proc.root), "--top", "2", "--bots-dir", str(bots),
               "--bots-dir", str(tmp_path / "gone")).stdout
    assert out == ("Largest bots by memory: fleet/alpha 2,304 MiB (512 MiB swapped),"
                   " fleet/beta 400 MiB (100 MiB swapped); outside every bot 3,072 MiB"
                   " (1,024 MiB swapped).\n")


def test_bots_prints_nothing_with_no_proc(tmp_path):
    assert _run("bots", "--proc", str(tmp_path / "none")).stdout == ""


# Each one bot.conf; BOT_SERVICE is read from it here and by bot_conf_get.
BOT_CONFS = [
    "BOT_SERVICE=plain", "export BOT_SERVICE=exported", "BOT_SERVICE='single quoted'",
    'BOT_SERVICE="double quoted"', "BOT_SERVICE=cut   # at its comment", 'BOT_SERVICE="open',
    "BOT_SERVICE='mixed\"", "BOT_SERVICE=", "BOT_SERVICE= after a space", "BOT_SERVICE=a=b",
    "BOT_SERVICE_X=other\nBOT_SERVICE=second", "#BOT_SERVICE=commented\nBOT_SERVICE=after",
    " BOT_SERVICE=indented", "BOT_SERVICE=first\nBOT_SERVICE=second", 'BOT_SERVICE=""',
    "BOT_SERVICE=tab\there", "exportBOT_SERVICE=joined", "BOT_NAME=x",
]


def test_a_bot_conf_value_reads_as_bot_conf_get_reads_it(tmp_path):
    """fleet-pulse's browser leg reads the bot units with lib-common.sh's
    bot_conf_get, and `bots` reads them here: the two must agree."""
    dirs = []
    for number, text in enumerate(BOT_CONFS):
        (tmp_path / str(number)).mkdir()
        (tmp_path / str(number) / "bot.conf").write_text(text + "\n")
        dirs.append(str(tmp_path / str(number)))
    shell = subprocess.run(
        ["bash", "-c", '. "$1"; shift; for d in "$@"; do bot_conf_get "$d" BOT_SERVICE ""; '
                       "printf '\\037'; done", "_", str(SCRIPT.parent / "lib-common.sh"), *dirs],
        capture_output=True, text=True, timeout=60, env=constructed_env(CLAUDLOBBY_ROOT=tmp_path))
    assert shell.returncode == 0, shell.stderr
    mt = load_lib_module("memory-thrash")
    here = [mt.conf_values(Path(d), ("BOT_SERVICE",)).get("BOT_SERVICE", "") for d in dirs]
    assert here == shell.stdout.split("\037")[:-1]


CURL_ACCEPTS = "#!/bin/bash\nprintf '%s' '{\"ok\":true,\"result\":{\"message_id\":7}}'\n"
CURL_REJECTS = "#!/bin/bash\nprintf '%s' '{\"ok\":false,\"error_code\":404,\"description\":\"Not Found\"}'\n"


def _probe_host(tmp_path, scratch_plane_env, curl=CURL_ACCEPTS, lines=RULE):
    """A root-mode host whose one bot names the alert chat, a /proc of its own and
    the host's line (no file for None), with the real probe's tools on PATH beside a
    stub curl."""
    root = tmp_path / "root"
    chan = tmp_path / "chan"
    chan.mkdir()
    (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n")
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('export TELEGRAM_GROUP_CHAT_ID="-1001234567890"\n'
                                  f'export TELEGRAM_STATE_DIR="{chan}"\n')
    (root / "state" / "memory-thrash").mkdir(parents=True)
    if lines is not None:
        (root / "state" / "memory-thrash" / "lines").write_text(lines)
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text("boot-1\n")
    (proc / "stat").write_text("cpu 1 2 3 4 5 6 7 8\nbtime 1786878506\nprocs_running 3\nprocs_blocked 7\n")
    (proc / "uptime").write_text("100000.00 1.00\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_exec(stubs / "curl", curl)
    _write_exec(stubs / "hostname", "#!/bin/bash\necho probe-host\n")
    env = constructed_env(PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", HOME=tmp_path / "home",
                          HOST_PROBE_PROC=proc, **scratch_plane_env(root, initialize=True))
    return root, proc, env


def _probe(proc, env, socket, at, out=None):
    vmstat = proc / "vmstat"
    if out is None:
        vmstat.unlink(missing_ok=True)
    else:
        vmstat.write_text(f"pswpin 5\npswpout {out}\n")
    result = subprocess.run(["bash", str(PROBE)], capture_output=True, text=True, timeout=120,
                            env={**env, "PLANE_SOCKET": str(socket), "MEMORY_THRASH_NOW": str(at)})
    assert result.returncode == 0, result.stderr
    return result


def _probe_minutes(root, proc, env, scratch_plane_env, readings):
    with _serving(root, scratch_plane_env) as socket:
        out = 0
        for at, pages in readings:
            out += pages
            _probe(proc, env, socket, at, out)
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    return [row["data"]["reason"] for row in rows if row["type"] == "memory_thrash"], rows


def test_the_probe_raises_one_alert_an_episode(tmp_path, *, scratch_plane_env):
    root, proc, env = _probe_host(tmp_path, scratch_plane_env)
    alerts, _ = _probe_minutes(root, proc, env, scratch_plane_env, [
        (0, 0), (60, 500), (120, 500), (180, 500), (240, 50), (300, 500), (360, 500)])
    assert len(alerts) == 2, alerts
    head, tail = alerts[0].split(", MemAvailable ")
    assert head == ("swap thrash on probe-host: swap-out over 100 pages a minute for 2 readings"
                    " in a row (500, 500)")
    assert tail.split(" MB, ", 1)[1] == (
        "7 processes blocked on IO. Largest bots by memory: none found; outside every bot 0 MiB"
        " (0 MiB swapped). Read the named bots first, then act through the manager's doors:"
        " bot compact, bot restart --fresh or bot stop, once their work is committed.")


def test_the_probe_raises_it_again_while_no_channel_took_it(tmp_path, *, scratch_plane_env):
    root, proc, env = _probe_host(tmp_path, scratch_plane_env, curl=CURL_REJECTS)
    alerts, rows = _probe_minutes(root, proc, env, scratch_plane_env,
                                  [(0, 0), (60, 500), (120, 500), (180, 500)])
    assert len(alerts) == 2, alerts
    assert [r["data"]["for_event"] for r in rows if r["type"] == "alert_delivery_failed"] == [
        "memory_thrash", "memory_thrash"]


def test_the_probe_says_a_missing_counter_once(tmp_path, *, scratch_plane_env):
    root, proc, env = _probe_host(tmp_path, scratch_plane_env)
    with _serving(root, scratch_plane_env) as socket:
        first = _probe(proc, env, socket, 0)
        again = _probe(proc, env, socket, 60)
    said = f"memory-thrash: skipped: no {proc}/vmstat"
    assert said in first.stderr and said not in again.stderr


def test_the_probe_pages_a_refused_line_file_once(tmp_path, *, scratch_plane_env):
    root, proc, env = _probe_host(tmp_path, scratch_plane_env, lines=BAD_RULE)
    alerts, rows = _probe_minutes(root, proc, env, scratch_plane_env,
                                  [(0, 0), (60, 500), (120, 500)])
    unarmed = [r["data"]["reason"] for r in rows if r["type"] == "memory_thrash_unarmed"]
    assert alerts == [] and len(unarmed) == 1, unarmed
    assert unarmed[0].startswith("swap thrash alert off on probe-host: its line file was refused: ")
    assert unarmed[0].endswith(
        " line 2: minutes needs a positive whole number. This host has no swap thrash alert until"
        " the file is fixed; the check reads it every minute, and memory-thrash.py status reads"
        " the rule back.")


def test_the_probe_says_no_line_once_and_raises_nothing(tmp_path, *, scratch_plane_env):
    root, proc, env = _probe_host(tmp_path, scratch_plane_env, lines=None)
    with _serving(root, scratch_plane_env) as socket:
        first = _probe(proc, env, socket, 0, 0)
        again = _probe(proc, env, socket, 60, 500)
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    assert [r["type"] for r in rows if r["type"].startswith("memory_thrash")] == []
    said = "memory-thrash: skipped: no line for this host: "
    assert said in first.stderr and said not in again.stderr
