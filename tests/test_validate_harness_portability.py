"""Execute actual validation-harness fragments in owned scratch environments.

These controls need no tmux server, launchd, systemd, network, or Plane writes.
The two process controls use real session groups and native bridge ancestry;
all diagnostic controls keep the original failing assertions observable.
The same module can be overlaid on the parent checkout for RED evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "lib" / "validate-bot-change.sh"


def _between(start: str, end: str) -> str:
    source = HARNESS.read_text()
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def _helpers() -> str:
    # The parent has no helpers; its original fragments still execute so RED
    # identifies missing behavior, rather than failing during test collection.
    source = HARNESS.read_text()
    start = "# --- Harness-only diagnostics and owned foreign-tree cleanup"
    return _between(start, "# --- End harness-only helpers") if start in source else ""


def _environment(tmp_path: Path, **extra: str) -> dict[str, str]:
    for name in ("home", "tmp", "root"):
        (tmp_path / name).mkdir(exist_ok=True)
    return constructed_env(HOME=tmp_path / "home", TMPDIR=tmp_path / "tmp",
                           CLAUDLOBBY_ROOT=tmp_path / "root", **extra)


def _run(tmp_path: Path, body: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _helpers() + "\n" + body],
        env=_environment(tmp_path, **extra), cwd=tmp_path, capture_output=True,
        text=True, timeout=30,
    )


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def test_existing_vault_directories_do_not_require_gnu_realpath(tmp_path):
    physical = tmp_path / "physical directory"
    physical.mkdir()
    alias = tmp_path / "alias directory"
    alias.symlink_to(physical, target_is_directory=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "realpath", "echo 'realpath: illegal option -- m' >&2\nexit 64\n")
    setup = _between('_VG_ROOT="$(mktemp -d)"', "_vg_hook()")
    # Supply a known existing symlink, then execute all three shipped path reads.
    setup = setup.replace('_VG_ROOT="$(mktemp -d)"', f"_VG_ROOT={shlex.quote(str(alias))}")
    nested = _between('mkdir -p "$_VG_ROOT/vault/.git"', '_vg_nested=')
    result = _run(tmp_path, setup + nested + '\nprintf "%s\\n" "$_VG_VAULT" "$_VG_PROJ" "$_VG_NESTED"',
                  PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert result.returncode == 0, f"existing-directory canonicalization failed: {result.stderr}"
    assert result.stdout.splitlines() == [str(physical / relative) for relative in
                                         ("vault", "projects/repo", "vault/home/f/bots/b/projects/repo")]


@pytest.mark.parametrize("push_received", [False, True], ids=["missing-push", "positive-control"])
def test_activity_push_failure_keeps_assertions_and_prints_diagnostics(tmp_path, push_received):
    bots = tmp_path / "bots"
    for name in ("control", "idle", "nomark"):
        (bots / name / "data").mkdir(parents=True)
    pulse = tmp_path / "activity-pulse.stderr"
    pulse.write_text("fixture delivery stderr\n")
    (tmp_path / "activity-pulse.stdout").write_text("fixture delivery stdout\n")
    pane = "control activity_stuck" if push_received else "no delivery"
    prefix = f"""
ROOT={shlex.quote(str(tmp_path))}
F3_BOTS={shlex.quote(str(bots))}
MGR=fixture-manager; TMUX_TMPDIR="$TMPDIR"; _s_pulse_rc=7
SCTL=control; SIDLE=idle; SNOMARK=nomark
s_ctl_ev='{{"type":"activity_stuck"}}'; s_idle_ev=''; s_nomark_ev=''
s_mgr_pane={shlex.quote(pane)}
harness_check() {{ printf 'CHECK %s=%s\\n' "$1" "$2"; }}
vsock() {{ printf 'tmux-%s' "$1"; }}
tmux() {{
    case "$1" in
        list-panes) echo 'pid=123 dead=0 width=80 height=24' ;;
        capture-pane) echo "diagnostic-only: $*" ;;
    esac
}}
"""
    body = _between("# --- the controls run FIRST:", "# ===========================================================================\n# #934 S3")
    result = _run(tmp_path, prefix + body)
    assert result.returncode == 0, result.stderr
    verdict = "yes" if push_received else "no"
    assert f"CONTROL: the control bot [FLEET-PULSE] push DOES reach the manager pane={verdict}" in result.stdout
    assert f"S1 RED: no [FLEET-PULSE] manager push for the suppressed bot (control pushed)={verdict}" in result.stdout
    if push_received:
        assert "DIAGNOSTIC:" not in result.stdout
    else:
        assert "DIAGNOSTIC: #934 S1/S2 fixture state" in result.stdout, "push-only failure lost its diagnostics"
        assert "fixture delivery stderr" in result.stdout
        assert "fleet-pulse rc 7" in result.stdout
        assert "pid=123 dead=0 width=80 height=24" in result.stdout
        assert "manager visible pane" in result.stdout
        assert "manager joined history; diagnostic only" in result.stdout



def test_reload_push_failure_keeps_the_failed_check_and_action_output(tmp_path):
    lib = tmp_path / "reload-lib"
    stub = tmp_path / "stub-bin"
    bot = tmp_path / "bot"
    lib.mkdir()
    stub.mkdir()
    (bot / "data").mkdir(parents=True)
    _executable(lib / "reload-fleet.sh", "echo 'reload stdout'; echo 'reload stderr' >&2; exit 7\n")
    prefix = f"""
ROOT={shlex.quote(str(tmp_path))}; LIB_DIR={shlex.quote(str(lib))}
STUB_BIN={shlex.quote(str(stub))}; BOT_DIR={shlex.quote(str(bot))}
FLEET=fixture; MGR=fixture-manager; TMUX_TMPDIR="$TMPDIR"
harness_check() {{ printf 'CHECK %s=%s\\n' "$1" "$2"; }}
val_events() {{ echo '{{"type":"reload_failed"}}'; }}
val_diag() {{ "$@"; }}
vsock() {{ printf 'tmux-%s' "$1"; }}
tmux() {{ echo 'no manager delivery'; }}
"""
    body = _between("# Loud-fail: a failing 'claude plugin update'", "# ===========================================================================\n# F2(b)")
    result = _run(tmp_path, prefix + body)
    assert result.returncode == 0, result.stderr
    assert "alerts the manager on failure (shared emit_failure_alert)=no" in result.stdout
    assert "DIAGNOSTIC: reload-fleet manager push (rc 7)" in result.stdout, "reload push failure lost action output"
    assert "reload stdout" in result.stdout
    assert "reload stderr" in result.stdout
    assert "manager visible pane" in result.stdout

def test_plane_dispatch_retains_each_leg_and_its_original_exit_code(tmp_path):
    lib = tmp_path / "dispatch-lib"
    lib.mkdir()
    _executable(lib / "dispatch-task.sh", 'printf "out:%s\\n" "$3"\nprintf "err:%s\\n" "$3" >&2\nexit "${PROBE_RC:-0}"\n')
    function = _between("    _pl_dispatch() {", "    _pl_count()")
    prefix = f"PL_ROOT={shlex.quote(str(tmp_path))}; PL_LIB={shlex.quote(str(lib))}\nPL_CLI=unused; PL_SOCK=unused\n"
    result = _run(tmp_path, prefix + function + """
_pl_dispatch "" "first"
rc=0; _pl_dispatch "PROBE_RC=7" "second" || rc=$?
printf 'SECOND_RC=%s\n' "$rc"
if declare -F val_plane_diagnostics >/dev/null; then val_plane_diagnostics; fi
""")
    assert result.returncode == 0, result.stderr
    assert "SECOND_RC=7" in result.stdout
    assert (tmp_path / "err").read_text() == "err:second\n"
    records = tmp_path / "dispatch-legs"
    assert records.is_dir(), "earlier dispatch evidence was overwritten instead of retained"
    for number, text, rc in ((1, "first", 0), (2, "second", 7)):
        assert (records / f"{number}.stdout").read_text() == f"out:{text}\n"
        assert (records / f"{number}.stderr").read_text() == f"err:{text}\n"
        assert (records / f"{number}.rc").read_text() == f"{rc}\n"
        assert "wedge before: absent" in (records / f"{number}.meta").read_text()
        assert f"err:{text}" in result.stdout, "captured log must retain evidence before scratch purge"


def _live_group(pgid: int) -> list[int]:
    rows = subprocess.check_output(["ps", "-axo", "pid=,pgid=,stat="], text=True)
    return [int(pid) for pid, group, state in (row.split() for row in rows.splitlines())
            if int(group) == pgid and not state.startswith("Z")]


def _reap_test_group(path: Path) -> None:
    # Independent test-owned backstop: a failing parent or cleanup mutant must
    # not leave its harmless control processes behind on the verification host.
    if not path.exists():
        return
    root = int(path.read_text())
    assert root > 1 and root != os.getpgrp()
    for _ in range(100):
        if not _live_group(root):
            return
        try:
            os.killpg(root, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            # Darwin can refuse a zombie-only group between the probe and kill.
            if _live_group(root):
                raise
        time.sleep(0.05)
    assert not _live_group(root), "test-owned group cleanup failed"


def test_foreign_tree_uses_native_session_and_real_lineage_without_setsid_tool(tmp_path):
    bindir = tmp_path / "poison-bin"
    bindir.mkdir()
    attempted = tmp_path / "external-setsid-called"
    _executable(bindir / "setsid", f"touch {shlex.quote(str(attempted))}\necho 'external setsid unavailable' >&2\nexit 127\n")
    rb = tmp_path / "foreign"
    bot = rb / "bot"
    (bot / "state").mkdir(parents=True)
    (bot / "bot.conf").write_text(f'TELEGRAM_BOT_HANDLE=fixture\nTELEGRAM_STATE_DIR="{bot / "state"}"\n')
    root_pid = tmp_path / "root-pid"
    prefix = f"""
. {shlex.quote(str(REPO / 'lib/lib-common.sh'))}
RB_ROOT={shlex.quote(str(rb))}; RB_DIR={shlex.quote(str(bot))}
"""
    startup = _between('_SC_BIN="$RB_ROOT/scopebin"', '# The bot-scoped question')
    # Keep the real precondition and use its scored output as the RED assertion.
    body = prefix + 'harness_check() { printf "CHECK %s=%s\\n" "$1" "$2"; }\n' + startup
    body += f'printf "%s" "$_SC_ROOT_PID" > {shlex.quote(str(root_pid))}\n'
    body += """
printf 'BOT_STATE=%s\n' "$(bridge_state "$RB_DIR" fixture-token || true)"
printf 'OWN_STATE=%s\n' "$(bridge_state "$RB_DIR" fixture-token "$_SC_ROOT_PID" || true)"
printf 'FOREIGN_STATE=%s\n' "$(bridge_state "$RB_DIR" fixture-token "$$" || true)"
ps -o pid=,ppid=,pgid=,comm= -p "$_SC_ROOT_PID"
if declare -F val_stop_scope_tree >/dev/null; then val_stop_scope_tree
else kill -9 -"$_SC_ROOT_PID" 2>/dev/null || true; wait "$_SC_ROOT_PID" 2>/dev/null || true; fi
"""
    try:
        result = _run(tmp_path, body, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        assert result.returncode == 0, result.stderr
        assert "fixture precondition)=yes" in result.stdout, f"native foreign-tree fixture did not become ready: {result.stdout} {result.stderr}"
        assert not attempted.exists(), "fixture still depends on the external setsid program"
        assert "BOT_STATE=up" in result.stdout
        assert "OWN_STATE=up" in result.stdout
        assert "FOREIGN_STATE=not_mine" in result.stdout
        assert not _live_group(int(root_pid.read_text())), "normal foreign-tree cleanup leaked executing members"
    finally:
        _reap_test_group(root_pid)


def test_mid_scenario_abort_reaps_the_owned_foreign_group(tmp_path):
    rb = tmp_path / "foreign"
    rb.mkdir()
    root_pid = tmp_path / "abort-root-pid"
    ready = tmp_path / "ready.json"
    sleeper = rb / "sleeper.py"
    sleeper.write_text(
        "import json, os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(ready)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(45)\n"
    )
    cleanup = _between("cleanup() {", "trap cleanup EXIT")
    prefix = f"""
ROOT={shlex.quote(str(rb))}; TMUX_TMPDIR="$TMPDIR/unused-sockets"
BOT=; MGR=; IBOT=; BUSY=; SBOT=; MBOT=
pass=1; fail=0
"""
    body = prefix + cleanup + f"""
trap cleanup EXIT
{shlex.quote(sys.executable)} -c 'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \\
    {shlex.quote(sys.executable)} {shlex.quote(str(sleeper))} >/dev/null 2>&1 &
_SC_ROOT_PID=$!
printf '%s' "$_SC_ROOT_PID" > {shlex.quote(str(root_pid))}
for _ in $(seq 1 100); do
    [ -s {shlex.quote(str(ready))} ] && break
    sleep 0.05
done
[ -s {shlex.quote(str(ready))} ]
exit 42
"""
    try:
        result = _run(tmp_path, body)
        assert ready.exists(), f"control tree never started: {result.stderr}"
        assert result.returncode == 42, result.stderr
        assert "ABORTED (rc 42)" in result.stdout
        assert len(json.loads(ready.read_text())) == 2
        assert not _live_group(int(root_pid.read_text())), "EXIT cleanup leaked the owned foreign tree"
    finally:
        _reap_test_group(root_pid)
