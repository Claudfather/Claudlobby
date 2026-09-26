"""Disposable-host evidence for #1884; never a runner for the live fleet host.

Two immutable archives are installed in separate venvs. The controller never
edits their test modules except during named candidate mutations, restoring and
hashing the entire tracked archive after each arm. Parent failures are evidence
only when pytest completed; a timeout/denied native operation is INVALID.

The observer records real Popen/tmux results without substituting process rows
or return codes. Environment defaults and refusing clients form the external
isolation boundary; they are identical for parent, candidate and mutants.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

PARENT = "b1f9b65d5c85da9f7c95fadb1aa45e8ed79f2f96"
CANDIDATE = "78ae69ddc0ca3297c31d98c67db65e457f12ddf8"
MODULES = (
    "tests/test_lib_common_tmpdir_fallback.py",
    "tests/test_claude_session_pid.py",
    "tests/test_fleet_pulse_no_events.py",
)
FORBIDDEN = ("curl", "wget", "ssh", "scp", "git", "gh", "claude", "codex",
             "launchctl", "systemctl", "osascript", "open", "npm", "npx", "brew")
UTILITIES = ("awk", "basename", "bash", "cat", "chmod", "cut", "date", "dirname",
             "env", "find", "grep", "head", "id", "ln", "ls", "mkdir", "mktemp",
             "mv", "printf", "ps", "readlink", "rm", "sed", "sh", "sleep", "sort",
             "stat", "tail", "tee", "touch", "tr", "uname", "wc", "xargs")
ANCESTRY_CASES = (
    "test_door_exists_and_is_executable", "test_parses_under_bash",
    "test_resolves_to_an_ancestor_named_claude", "test_refuses_when_no_claude_ancestor",
    "test_refusal_is_not_a_number", "test_never_consults_the_process_table",
    "test_from_walks_the_given_ancestry", "test_rejects_a_non_pid_from", "test_summary_shape",
    "test_the_prose_control_is_live", "test_ambient_claude_processes_do_not_leak_in",
    "test_every_lib_path_a_skill_references_exists_on_disk",
    "test_the_existence_check_rejects_the_shape_that_shipped",
) + tuple(name + "[" + skill + "]" for name in (
    "test_skill_uses_the_door", "test_skill_has_no_process_scan_in_executable_lines",
    "test_the_skill_line_actually_runs") for skill in (
        "selfcheck", "review-status", "status-personal", "eng-status"))


def expected_nodes(role, index):
    prefix = MODULES[index][:-3].replace("/", ".")
    if index == 0:
        first = ("TestTheFixedTemplateWorksOnGnu::test_the_new_template_succeeds_where_the_old_one_failed"
                 if role == "parent" else
                 "TestTheTemplateWorksOnTheNativeUtility::test_old_template_semantics_and_working_primary")
        names = (first,
            "TestThePrimaryFailingFallsThroughToTheFallback::test_fallback_rescues_when_reachable",
            "TestBothAttemptsFailingNamesTheHelper::test_env_tiers_names_lc_tmpdir_not_a_bare_exit",
            "TestBothAttemptsFailingNamesTheHelper::test_lib_common_alone_shows_the_same_diagnostic")
    elif index == 1:
        names = ANCESTRY_CASES + (() if role == "parent" else ("test_fixture_ignores_ambient_startup_files",))
    else:
        names = ("test_pulse_completes_with_no_events_bot[summary-site]",
                 "test_pulse_completes_with_no_events_bot[escalation-site]",
                 "test_a_healthy_bridge_check_fires_no_phantom_script_error",
                 "test_the_handoff_status_is_captured_without_firing_the_trap")
    return {prefix + "." + n if "::" in n else prefix + "::" + n for n in names}


def inventory_valid(role, index, cases):
    """Pin exact node IDs, not a total that can hide one missing/duplicate case."""
    if len(cases) != len(expected_nodes(role, index)) or {c["node"] for c in cases} != expected_nodes(role, index):
        return False
    allowed_parent_red = ("test_the_new_template_succeeds_where_the_old_one_failed",
        "test_env_tiers_names_lc_tmpdir_not_a_bare_exit", "test_lib_common_alone_shows_the_same_diagnostic",
        "test_resolves_to_an_ancestor_named_claude", "test_from_walks_the_given_ancestry", "test_summary_shape",
        "test_pulse_completes_with_no_events_bot[summary-site]", "test_pulse_completes_with_no_events_bot[escalation-site]")
    for case in cases:
        name = case["node"].rsplit("::", 1)[-1]
        if case["status"] == "passed":
            continue
        if case["status"] == "skipped" and name == "test_ambient_claude_processes_do_not_leak_in" and "no live claude processes on this host" in case["detail"]:
            continue
        if role == "parent" and case["status"] in ("failure", "error") and name in allowed_parent_red:
            continue
        return False
    return True


def mutant_valid(label, cases):
    index, suffix, status = {
        "remove-fallback": (0, "::test_fallback_rescues_when_reachable", "failure"),
        "remove-failure-guard": (0, ".TestBothAttemptsFailingNamesTheHelper::", "failure"),
        "raw-comm": (1, "::test_resolves_to_an_ancestor_named_claude", "failure"),
        "wrong-ancestor": (1, "::test_from_walks_the_given_ancestry", "failure"),
        "long-socket": (2, "::test_pulse_completes_with_no_events_bot[", "error"),
        "ambient-startup": (1, "::test_fixture_ignores_ambient_startup_files", "failure"),
    }[label]
    expected = {n for n in expected_nodes("candidate", index) if suffix in n}
    return (len(cases) == len(expected) and {c["node"] for c in cases} == expected
            and all(c["status"] == status for c in cases))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def append(path, value):
    with Path(path).open("a") as out:
        out.write(json.dumps(value) + "\n")


def hosted_only():
    if not (os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("CLAUDLOBBY_NATIVE_FIXTURE_PROOF") == "1"):
        raise RuntimeError("native proof requires an opted-in disposable GitHub-hosted runner")


def owned(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def rows(xml):
    """Keep node names/counts/statuses; a collection error is never a RED."""
    cases = []
    for case in ET.parse(xml).iter("testcase"):
        status = next((s for s in ("error", "failure", "skipped")
                       if case.find(s) is not None), "passed")
        detail = case.find(status) if status != "passed" else None
        cases.append({"node": case.get("classname", "") + "::" + case.get("name", ""),
                      "status": status, "detail": "" if detail is None else "".join(detail.itertext())})
    return cases


def valid_completed(rc, cases, timed_out, log):
    # Setup errors (e.g. the old overlong socket) can be meaningful, collection
    # errors cannot. Requiring all selected node names is enforced separately.
    invalid = ("Operation not permitted", "PermissionError", "TimeoutExpired",
               "INTERNALERROR", "ERROR collecting", "fixture proof refused")
    return (not timed_out and rc in (0, 1) and bool(cases)
            and not any(c["status"] == "skipped" and not (
                c.get("node", "").endswith("::test_ambient_claude_processes_do_not_leak_in")
                and "no live claude processes on this host" in c.get("detail", "")) for c in cases)
            and not any(s in log for s in invalid))


def mutation(name, source):
    """One exact edit per negative control; drift refuses instead of no-op."""
    common = "lib/lib-common.sh"
    ancestry = MODULES[1]
    specs = {
        "remove-fallback": (common,
            "&& ! _LC_TMPDIR=$(mktemp -d -t 'lib-common.XXXXXXXXXX')", "&& true",
            MODULES[0] + "::TestThePrimaryFailingFallsThroughToTheFallback"),
        "remove-failure-guard": (common,
            '    exit 1\nfi\n', '    :\nfi\n',
            MODULES[0] + "::TestBothAttemptsFailingNamesTheHelper"),
        "raw-comm": (ancestry,
            'Path(out["COMM"].strip()).name == "claude"', 'out["COMM"].strip() == "claude"',
            ancestry + "::test_resolves_to_an_ancestor_named_claude"),
        "wrong-ancestor": ("lib/claude-session-pid.sh",
            "claude) printf '%s\\n' \"$p\"; return 0 ;;",
            "claude) printf '%s\\n' \"$((p + 1))\"; return 0 ;;",
            ancestry + "::test_from_walks_the_given_ancestry"),
        "long-socket": (MODULES[2], 'prefix="p610-", dir="/tmp"',
            'prefix="p610-" + "x" * 120, dir="/tmp"',
            MODULES[2] + "::test_pulse_completes_with_no_events_bot"),
        "ambient-startup": (ancestry,
            'start_new_session=True, env=env)', 'start_new_session=True, env=None)',
            ancestry + "::test_fixture_ignores_ambient_startup_files"),
    }
    path, old, new, selection = specs[name]
    target = source / path
    before = target.read_bytes()
    text = before.decode()
    if name == "remove-failure-guard":
        # Change only the allocation guard, not another exit-1 branch.
        start = text.index("if ! _LC_TMPDIR=")
        end = text.index("\nfi", start) + len("\nfi\n")
        fragment = text[start:end]
        assert fragment.count(old) == 1
        text = text[:start] + fragment.replace(old, new) + text[end:]
    else:
        assert text.count(old) == 1, (name, path, text.count(old))
        text = text.replace(old, new)
    target.write_text(text)
    return target, before, selection


def fingerprints(source):
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*") if p.is_file() and not p.is_symlink()}


def verify(source, expected):
    assert all((source / p).is_file() and hashlib.sha256((source / p).read_bytes()).hexdigest() == h
               for p, h in expected.items()), "tracked archive bytes changed"


def export(repo, sha, dest):
    data = subprocess.check_output(["git", "-C", str(repo), "archive", sha], timeout=30)
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        # Archives come from pinned reviewed commits, but still reject escapes.
        for member in archive.getmembers():
            assert owned(dest / member.name, dest) and not member.isdev()
            if member.issym() or member.islnk():
                assert owned(dest / member.name / ".." / member.linkname, dest)
        archive.extractall(dest)
    return fingerprints(dest)


def snapshot():
    r = subprocess.run(["/bin/ps", "-Ao", "pid=,ppid=,pgid=,comm="],
                       capture_output=True, text=True, timeout=10, check=True)
    return [{"pid": int(a), "ppid": int(b), "pgid": int(c), "comm": d}
            for line in r.stdout.splitlines() if len(parts := line.split(None, 3)) == 4
            for a, b, c, d in [parts]]


def tmux_passthrough(real, ledger, proof_root, args):
    """Execute native tmux unchanged, recording only our exact server/pane IDs."""
    hosted_only()
    directory = Path(os.environ.get("TMUX_TMPDIR", "/invalid"))
    short = (directory.resolve().parent == Path("/tmp").resolve()
             and directory.name.startswith("p610-") and directory.is_dir()
             and directory.stat().st_uid == os.getuid())
    assert owned(directory, proof_root) or short, "fixture proof refused unowned tmux socket"
    assert "-L" in args, "fixture proof refused default tmux server"
    name = args[args.index("-L") + 1]
    assert name in ("pulse610", "pulse610-none"), name
    # Record the endpoint BEFORE creation. Native tmux may create a detached
    # server and then fail or time out before any PID query succeeds.
    append(Path(ledger).with_name("tmux-attempts.jsonl"),
           {"socket_dir": str(directory), "socket_name": name})
    result = subprocess.run([real, *args], timeout=70)
    if "new-session" in args and result.returncode == 0:
        for fmt in ("#{pid}", "#{pane_pid}"):
            found = subprocess.run([real, "-L", name, "display-message", "-p", fmt],
                                   capture_output=True, text=True, timeout=10, check=True)
            append(ledger, {"kind": fmt, "pid": int(found.stdout.strip()),
                            "socket_dir": str(directory), "socket_name": name})
    return result.returncode


def tmux_absence(query, socket):
    """Only an exact owned-socket absence diagnostic establishes no server."""
    diagnostics = {message for path in (socket, socket.resolve()) for message in (
        "no server running on " + str(path),
        "error connecting to " + str(path) + " (No such file or directory)")}
    return (query.returncode == 1 and not query.stdout
            and query.stderr.strip() in diagnostics)


def inspect_endpoints(real, attempts, env):
    """Independently query every attempted private endpoint, not just PID receipts."""
    found = []
    for directory, name in sorted({(a["socket_dir"], a["socket_name"]) for a in attempts}):
        socket = Path(directory) / ("tmux-" + str(os.getuid())) / name
        row = {"socket_dir": directory, "socket_name": name,
               "socket_exists": socket.exists(), "pids": [], "query_invalid": False}
        try:
            query = subprocess.run([real, "-L", name, "list-panes", "-a", "-F", "#{pid} #{pane_pid}"],
                env={**env, "TMUX_TMPDIR": directory}, capture_output=True, text=True, timeout=10)
            row.update(rc=query.returncode, stdout=query.stdout, stderr=query.stderr)
            # rc 1 alone includes permission, path and command failures. None
            # of those can prove absence, even when the socket file is gone.
            row["query_invalid"] = query.returncode != 0 and not tmux_absence(query, socket)
            if query.returncode == 0:
                parsed = [line.split() for line in query.stdout.splitlines()]
                if not parsed or any(len(pair) != 2 or not all(p.isdigit() for p in pair) for pair in parsed):
                    row["query_invalid"] = True
                else:
                    row["pids"] = sorted({int(pid) for pair in parsed for pid in pair})
        except subprocess.TimeoutExpired:
            row["query_invalid"] = True
            row["timeout"] = True
        found.append(row)
    return found


def child_pytest(source, state, xml, selection):
    """Observer only; no native process/utility answers are replaced."""
    hosted_only()
    import pytest
    source, state = Path(source), Path(state)
    sys.path.insert(0, str(source))
    import claudlobby
    assert Path(claudlobby.__file__).resolve().parent == source / "claudlobby"
    original = subprocess.Popen
    ledger = state / "processes.jsonl"
    defaults = {key: os.environ[key] for key in
                ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "TELEGRAM_STATE_DIR", "TMUX_TMPDIR",
                 "GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "CLAUDLOBBY_NATIVE_FIXTURE_PROOF")}

    class ObservedPopen(original):
        def __init__(self, args, *a, **kw):
            if kw.get("env") is not None:
                kw["env"] = {**defaults, **kw["env"]}
            super().__init__(args, *a, **kw)
            command = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
            self.proof_command = command
            self.proof_native = any(x.endswith("/claude") or x.endswith("/tree.js") for x in command)
            if self.proof_native or kw.get("start_new_session"):
                append(ledger, {"pid": self.pid, "new_group": bool(kw.get("start_new_session")),
                                "command": command, "kind": "native ancestry"})

        def communicate(self, *a, **kw):
            result = super().communicate(*a, **kw)
            if self.proof_native:
                append(state / "native-observations.jsonl", {"pid": self.pid,
                       "rc": self.returncode, "stdout": str(result[0]), "stderr": str(result[1])})
            # The old BSD oracle leaks a new empty directory before its bad
            # assertion. Reap exactly its native returned allocation, after
            # execution; do not change argv, return code, output or semantics.
            if self.proof_command == ["mktemp", "-d", "-t", "lib-common"] and self.returncode == 0:
                path = Path(result[0].strip())
                assert path.name.startswith("lib-common.") and path.stat().st_uid == os.getuid()
                path.rmdir()
            return result

    subprocess.Popen = ObservedPopen
    try:
        return pytest.main([*selection, "-ra", "--tb=short", "--junitxml=" + xml,
                            "--basetemp=" + str(state / "pytest"),
                            "-o", "cache_dir=" + str(state / "cache")])
    finally:
        subprocess.Popen = original


def environment(source, state, tools, proof_root):
    for name in ("home", "tmp", "xdg", "channel", "socket", "root"):
        (state / name).mkdir(parents=True)
    return {"PATH": str(tools), "HOME": str(state / "home"), "TMPDIR": str(state / "tmp"),
            "XDG_CONFIG_HOME": str(state / "xdg"), "LANG": "C.UTF-8", "LC_ALL": "C",
            "TELEGRAM_STATE_DIR": str(state / "channel"), "TMUX_TMPDIR": str(state / "socket"),
            "CLAUDLOBBY_ROOT": str(state / "root"), "PLANE_EMIT_DISABLED": "1",
            "CLAUDLOBBY_TOOL_PREFIXES": "",
            "PLANE_SOCKET": str(state / "socket" / "absent.sock"),
            "CLAUDLOBBY_HOST_SYSTEM_YAML": str(state / "absent-system.yaml"),
            "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "CLAUDLOBBY_NATIVE_FIXTURE_PROOF": "1"}


def make_tools(state, python, real_tmux, proof_root):
    tools = state / "bin"
    tools.mkdir()
    native = {}
    for name in UTILITIES:
        path = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
        assert path, "required native utility missing: " + name
        (tools / name).symlink_to(path)
        native[name] = path
    for name in ("node", "jq"):
        path = shutil.which(name)
        assert path, name
        (tools / name).symlink_to(path)
        native[name] = path
    for name in ("python", "python3"):
        (tools / name).symlink_to(python)
    (tools / "claudlobby").symlink_to(python.parent / "claudlobby")
    wrapper = tools / "tmux"
    wrapper.write_text("#!/bin/bash\nexec " + " ".join(shlex.quote(str(x)) for x in
        (python, Path(__file__).resolve(), "--tmux", real_tmux, state / "tmux.jsonl", proof_root)) + ' "$@"\n')
    wrapper.chmod(0o755)
    for name in FORBIDDEN:
        path = tools / name
        path.write_text("#!/bin/bash\nprintf '%s\\n' " + shlex.quote(name)
                        + " >> " + shlex.quote(str(state / "forbidden-calls"))
                        + "\necho 'fixture proof refused forbidden client' >&2\nexit 97\n")
        path.chmod(0o755)
    return tools, native


def install_network_guard(python, base):
    """Every venv Python child rejects IP traffic even after env construction.

    Dependency download finishes first. The pth hook then survives children
    dropping PYTHONPATH; Node only runs the fixed local ancestry script.
    """
    site = Path(subprocess.check_output([str(python), "-c",
        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    guard = site / "_native_fixture_network_guard.py"
    guard.write_text(
        "import sys\nfrom pathlib import Path\n"
        + "ROOT = Path(" + repr(str(base)) + ").resolve()\n"
        + "LOG = ROOT / 'evidence' / 'forbidden-network.jsonl'\n"
        + "def check(event, args):\n"
        + "    dns = ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo')\n"
        + "    if event not in ('socket.connect', 'socket.connect_ex', 'socket.sendto', 'socket.sendmsg', 'socket.bind') + dns:\n"
        + "        return\n"
        + "    address = None if event in dns else args[-1]\n"
        + "    if isinstance(address, str) and Path(address).resolve().is_relative_to(ROOT):\n"
        + "        return\n"
        + "    with LOG.open('a') as out: out.write(event + '\\n')\n"
        + "    raise PermissionError('fixture proof refused non-private network')\n"
        + "sys.addaudithook(check)\n"
    )
    (site / "_native_fixture_network_guard.pth").write_text("import _native_fixture_network_guard\n")


def run_arm(label, source, python, base, selection, real_tmux):
    state = base / "runs" / label
    state.mkdir(parents=True)
    tools, native = make_tools(state, python, real_tmux, base)
    env = environment(source, state, tools, base)
    evidence = base / "evidence" / label
    evidence.mkdir()
    xml, log = evidence / "pytest.xml", evidence / "pytest.log"
    write_json(evidence / "environment.json", {"env": env, "native_utilities": native,
               "python": str(python), "source": str(source), "selection": selection})
    timed_out = False
    with log.open("w") as out:
        proc = subprocess.Popen([str(python), str(Path(__file__).resolve()), "--pytest",
            str(source), str(state), str(xml), *selection], cwd=source, env=env,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            rc = proc.wait(timeout=10)
    records = []
    for name in ("processes.jsonl", "tmux.jsonl"):
        p = state / name
        if p.exists():
            records.extend(json.loads(line) for line in p.read_text().splitlines())
    attempts_file = state / "tmux-attempts.jsonl"
    attempts = [json.loads(line) for line in attempts_file.read_text().splitlines()] if attempts_file.exists() else []
    pids = {r["pid"] for r in records}
    groups = {proc.pid} | {r["pid"] for r in records if r.get("new_group")}
    deadline = time.monotonic() + 3
    while True:
        survivors = [r for r in snapshot() if r["pid"] in pids or r["pgid"] in groups]
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    endpoints = inspect_endpoints(real_tmux, attempts, env)
    # A cleanup defect stays a failure even if this emergency private cleanup
    # succeeds. Never target an ambient server or process selected by name.
    for endpoint in endpoints:
        if endpoint["pids"] or endpoint["socket_exists"] or endpoint["query_invalid"]:
            try:
                subprocess.run([real_tmux, "-L", endpoint["socket_name"], "kill-server"],
                    env={**env, "TMUX_TMPDIR": endpoint["socket_dir"]}, timeout=10, check=False)
            except subprocess.TimeoutExpired:
                endpoint["emergency_cleanup_timed_out"] = True
    for group in groups:
        if any(s["pgid"] == group for s in survivors):
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    sockets = sorted({r["socket_dir"] for r in attempts})
    socket_residue = [str(p) for directory in sockets for p in Path(directory).rglob("*")
                      if p.is_socket()]
    cases = rows(xml) if xml.exists() else []
    result = {"label": label, "rc": rc, "timed_out": timed_out, "cases": cases,
              "valid_completed": valid_completed(rc, cases, timed_out, log.read_text()),
              "cleanup": {"records": records, "survivors_before_emergency_cleanup": survivors,
                          "attempted_endpoints_before_emergency_cleanup": endpoints,
                          "socket_residue": socket_residue},
              "forbidden_calls": (state / "forbidden-calls").exists()}
    for name in ("processes.jsonl", "tmux.jsonl", "tmux-attempts.jsonl", "native-observations.jsonl", "forbidden-calls"):
        if (state / name).exists():
            shutil.copyfile(state / name, evidence / name)
    write_json(evidence / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("cases", "cleanup")}), flush=True)
    return result


def clean(result):
    return (result["valid_completed"] and not result["forbidden_calls"]
            and not result["cleanup"]["survivors_before_emergency_cleanup"]
            and not any(e["pids"] or e["socket_exists"] or e["query_invalid"]
                        for e in result["cleanup"]["attempted_endpoints_before_emergency_cleanup"])
            and not result["cleanup"]["socket_residue"])


def main(output):
    hosted_only()
    base = Path(output).resolve()
    assert not base.exists(), "proof output must be fresh"
    assert owned(base, os.environ["RUNNER_TEMP"])
    base.mkdir()
    (base / "evidence").mkdir()
    repo = Path(__file__).resolve().parents[2]
    real_tmux = shutil.which("tmux")
    assert real_tmux and shutil.which("node") and shutil.which("jq")
    write_json(base / "evidence" / "provenance.json", {"parent": PARENT, "candidate": CANDIDATE,
        "controller_sha": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
        "controller_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version, "platform": sys.platform, "tmux": real_tmux,
        "bash": subprocess.check_output(["/bin/bash", "--version"], text=True).splitlines()[0]})
    results, installations = [], {}
    for role, sha in (("parent", PARENT), ("candidate", CANDIDATE)):
        source = base / (role + "-source")
        hashes = export(repo, sha, source)
        venv = base / (role + "-venv")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=60)
        python = venv / "bin" / "python"
        with (base / "evidence" / (role + "-install.log")).open("w") as log:
            subprocess.run([str(python), "-m", "pip", "install", "-e", str(source) + "[dev]"],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
        install_network_guard(python, base)
        installations[role] = (source, python, hashes)
        for index, module in enumerate(MODULES):
            results.append(run_arm(role + "-" + str(index), source, python, base, [module], real_tmux))
            verify(source, hashes)
    source, python, hashes = installations["candidate"]
    for name in ("remove-fallback", "remove-failure-guard", "raw-comm", "wrong-ancestor",
                 "long-socket", "ambient-startup"):
        if name == "raw-comm" and sys.platform != "darwin":
            # Basenames really ARE native Linux's answer. This mutation is
            # required/killed on macOS, not relabeled as a Linux failure.
            continue
        target, before, selection = mutation(name, source)
        try:
            results.append(run_arm(name, source, python, base, [selection], real_tmux))
        finally:
            target.write_bytes(before)
            verify(source, hashes)
    failures = []
    if (base / "evidence" / "forbidden-network.jsonl").exists():
        failures.append("non-private network attempted")
    for r in results:
        passed = all(c["status"] in ("passed", "skipped") for c in r["cases"])
        if not clean(r):
            failures.append(r["label"] + ": invalid execution or cleanup")
        elif r["label"].startswith("candidate-") and (r["rc"] != 0 or not passed):
            failures.append(r["label"] + ": candidate not green")
        elif not r["label"].startswith(("parent-", "candidate-")) and (
                r["rc"] != 1 or not mutant_valid(r["label"], r["cases"])):
            failures.append(r["label"] + ": negative control did not fail its exact assertions")
        if r["label"].startswith(("parent-", "candidate-")):
            role, index = r["label"].split("-")
            if not inventory_valid(role, int(index), r["cases"]):
                failures.append(r["label"] + ": unexpected node inventory or control status")
    by_label = {r["label"]: r for r in results}
    native_records = by_label["candidate-1"]["cleanup"]["records"]
    if len([r for r in native_records if r["kind"] == "native ancestry"]) != 4:
        failures.append("candidate native ancestry launch inventory missing")
    pulse_records = by_label["candidate-2"]["cleanup"]["records"]
    if len([r for r in pulse_records if r["kind"] == "#{pid}"]) != 2:
        failures.append("candidate native tmux server inventory missing")
    # The direct BSD oracle is a meaningful parent RED independent of unsafe
    # copied-Bash startup. No timeout/denial can satisfy this requirement.
    if sys.platform == "darwin" and not any(
        r["label"] == "parent-0" and clean(r) and any(
            "test_the_new_template_succeeds_where_the_old_one_failed" in c["node"]
            and c["status"] == "failure" for c in r["cases"]) for r in results):
        failures.append("native BSD parent oracle RED absent")
    write_json(base / "evidence" / "summary.json", {"results": results, "failures": failures,
               "restored_tracked_counts": {k: len(v[2]) for k, v in installations.items()}})
    return bool(failures)


def self_check():
    """Pure controller controls; no native subprocess or fixture execution."""
    assert not valid_completed(1, [{"status": "failure"}], True, "")
    assert not valid_completed(2, [{"status": "error"}], False, "")
    assert not valid_completed(1, [{"status": "failure"}], False, "Operation not permitted")
    assert not valid_completed(0, [{"status": "skipped"}], False, "")
    assert valid_completed(1, [{"status": "failure"}], False, "assert native BSD returncode != 0")
    assert valid_completed(0, [{"status": "passed"}], False, "")
    assert valid_completed(0, [{"status": "skipped",
        "node": "test::test_ambient_claude_processes_do_not_leak_in",
        "detail": "no live claude processes on this host: control unavailable"}], False, "")
    socket = Path("/proof-owned/tmux-123/pulse610")
    for diagnostic in ("no server running on " + str(socket),
            "error connecting to " + str(socket) + " (No such file or directory)"):
        assert tmux_absence(subprocess.CompletedProcess([], 1, "", diagnostic), socket)
        assert not tmux_absence(subprocess.CompletedProcess([], 2, "", diagnostic), socket)
        assert not tmux_absence(subprocess.CompletedProcess([], 1, "unexpected", diagnostic), socket)
    for diagnostic in ("", "unknown query failure", "invalid socket path", "Operation not permitted",
            "operation unsupported", "no server running on /unowned/tmux-123/pulse610"):
        assert not tmux_absence(subprocess.CompletedProcess([], 1, "", diagnostic), socket)
    print("controller validity and exact endpoint absence controls passed; no native execution")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--tmux"]:
        raise SystemExit(tmux_passthrough(*sys.argv[2:5], sys.argv[5:]))
    if sys.argv[1:2] == ["--pytest"]:
        raise SystemExit(child_pytest(*sys.argv[2:5], sys.argv[5:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--self-check", action="store_true")
    options = parser.parse_args()
    if options.self_check:
        self_check()
    elif options.output:
        raise SystemExit(main(options.output))
    else:
        parser.error("choose --self-check or --output")
