"""Native #1811 evidence, exclusively on opted-in disposable hosted runners.

The historical installers/renderers are unmodified. Labels, installed files,
HOME and harmless receipt programs are owned by this proof. A distinct Linux
user manager is mandatory: changing HOME cannot redirect a running manager.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time

PARENT = "5943907f0bd076cb44cd77f4e67d6d6bdfa87467"
CANDIDATE = "beb4e35dcdfa51b51a17b306f0421d274c701698"
FORBIDDEN = ("tmux", "claude", "codex", "curl", "wget", "gh", "git", "ssh",
             "scp", "telegram", "claudlobby", "npm", "npx", "osascript", "open")
UTILITIES = ("basename", "dirname", "mkdir", "id", "cp", "rm", "grep", "sed",
             "tr", "awk", "date", "mktemp", "uname", "bash", "cat", "sleep")
ROLES = {"parent": PARENT, "candidate": CANDIDATE}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def hosted_only():
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("CLAUDLOBBY_INSTALLER_NATIVE_PROOF") == "1",
            "refused: installer proof requires opted-in disposable GitHub-hosted runner")


def owned(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def token():
    run, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(run.isdigit() and attempt.isdigit(), "missing numeric hosted run identity")
    value = "i1811" + run + "a" + attempt
    require(len(value) < 31, "run identity too long")
    return value


def base_path(value):
    hosted_only()
    base = Path(value).resolve()
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(base != runner_temp and owned(base, runner_temp), "proof must be below RUNNER_TEMP")
    require(not re.search(r"[\s'\"&<>%{};\\]", str(base)), "unsupported renderer proof path")
    return base


def command(argv, env, log, *, timeout=20, check=True, grouped=True):
    """Bound one owned client process group; never signal manager/service PIDs."""
    hosted_only()
    with subprocess.Popen([str(a) for a in argv], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=grouped) as proc:
        expired = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired = True
            if not grouped:
                require(os.getpid() == os.getpgrp(), "refused non-owned timeout process group")
                with Path(log).open("a") as stream:
                    stream.write(json.dumps({"argv": [str(a) for a in argv], "timeout": True, "arm_group": os.getpgrp()}) + "\n")
                # The outer controller owns this arm group and performs the
                # persisted-label cleanup even if this entire arm is killed.
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate(timeout=10)
        result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    with Path(log).open("a") as stream:
        stream.write(json.dumps({"argv": [str(a) for a in argv], "rc": result.returncode,
                                "stdout": out, "stderr": err, "timeout": expired, "observed_at": time.time()}) + "\n")
    require(not expired, "native command timed out; evidence INVALID")
    if check:
        require(result.returncode == 0, "command failed: " + repr(argv) + "\n" + out + err)
    return result


def verify_source(base, role):
    manifest = load(base / "prepared.json")
    source = base / (role + "-source")
    require(manifest["revisions"][role] == ROLES[role], "source pin mismatch")
    require(all(digest(source / p) == h for p, h in manifest["hashes"][role].items()),
            "immutable source changed")


def network_guard_source(base):
    # This proof needs no Python network sockets, including UNIX sockets.
    # Native systemctl uses only the dedicated manager bus; launchctl and the
    # fixed receipt program are bounded native children, outside Python's hook.
    return ("import sys\nfrom pathlib import Path\n"
            + "LOG = Path(" + repr(str(base / "evidence/forbidden-network.jsonl")) + ")\n"
            + "def check(event, args):\n"
            + "    if not event.startswith('socket.'):\n        return\n"
            + "    with LOG.open('a') as out: out.write(event + '\\n')\n"
            + "    raise PermissionError('installer proof refused Python networking')\n"
            + "sys.addaudithook(check)\n"
            + "sys._installer_network_guard = True\n")


def install_network_guard(python, base, env):
    """Install after downloads; .pth also covers env-cleared Python children."""
    result = command([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                     env, base / "evidence/prepare.jsonl")
    site = Path(result.stdout.strip())
    require(owned(site, python.parent.parent), "venv site escaped private prefix")
    (site / "_installer_network_guard.py").write_text(network_guard_source(base))
    (site / "_installer_network_guard.pth").write_text("import _installer_network_guard\n")
    command([python, "-c", "import sys; assert sys._installer_network_guard is True"],
            env, base / "evidence/prepare.jsonl")


def install_python_entrypoint(path, python):
    # A symlink through private bin can lose venv prefix discovery and skip its
    # .pth network guard. Exec the exact venv spelling instead (do not resolve).
    path.write_text("#!/bin/bash\nexec " + shlex.quote(str(python)) + ' "$@"\n')
    path.chmod(0o755)


def prepare(value):
    base = base_path(value)
    require(not base.exists(), "proof directory must be fresh")
    base.mkdir(mode=0o755)
    evidence = base / "evidence"
    evidence.mkdir()
    (base / "home").mkdir(mode=0o700)
    # Downloads happen during preparation, before the closed installer phase.
    env = {"PATH": os.environ["PATH"], "HOME": str(base / "home"),
           "TMPDIR": str(base), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
    manifest = {"revisions": ROLES, "controller": digest(__file__), "token": token(), "hashes": {}}
    for role, sha in ROLES.items():
        source = base / (role + "-source")
        source.mkdir()
        data = subprocess.check_output(["git", "archive", sha], timeout=30)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for item in archive.getmembers():
                require(owned(source / item.name, source) and not item.isdev(), "unsafe archive member")
                if item.issym() or item.islnk():
                    require(owned(source / item.name / ".." / item.linkname, source), "unsafe archive link")
            archive.extractall(source)
        manifest["hashes"][role] = {str(p.relative_to(source)): digest(p)
            for p in source.rglob("*") if p.is_file() and not p.is_symlink()}
        venv = base / (role + "-venv")
        command([sys.executable, "-m", "venv", venv], env, evidence / "prepare.jsonl", timeout=60)
        command([venv / "bin/python", "-m", "pip", "install", "-e", str(source)],
                env, evidence / "prepare.jsonl", timeout=180)
        install_network_guard(venv / "bin/python", base, env)
    save(base / "prepared.json", manifest)
    save(evidence / "provenance.json", {**manifest, "platform": platform.platform(),
                                       "python": sys.version})
    for role in ROLES:
        verify_source(base, role)


def private_env(base, role):
    state, home = base / (role + "-state"), base / "home"
    return {"PATH": str(state / "bin"), "HOME": str(home), "USER": pwd.getpwuid(os.getuid()).pw_name,
            "LOGNAME": pwd.getpwuid(os.getuid()).pw_name, "LANG": "C", "LC_ALL": "C",
            "TMPDIR": str(state / "tmp"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}" if sys.platform == "linux" else str(state / "xdg"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus" if sys.platform == "linux" else "",
            "CLAUDLOBBY_ROOT": str(state / "root"), "PLANE_EMIT_DISABLED": "1",
            "CLAUDLOBBY_TOOL_PREFIXES": "", "TELEGRAM_STATE_DIR": str(state / "channels"),
            "TMUX_TMPDIR": str(state / "sockets"), "TMUX_BIN": str(state / "bin/tmux"),
            "PLANE_SOCKET": str(state / "sockets/absent.sock"), "PYTHONDONTWRITEBYTECODE": "1"}


def parse_systemd(result):
    require(result.returncode == 0 and not result.stderr, "systemd query failed; cannot establish absence")
    data = {}
    for line in result.stdout.splitlines():
        require("=" in line, "invalid systemd property")
        key, value = line.split("=", 1)
        require(key not in data, "duplicate systemd property")
        data[key] = value
    required = {"LoadState", "ActiveState", "FragmentPath"}
    require(required <= data.keys(), "incomplete systemd state")
    if data["LoadState"] == "not-found":
        require(data["ActiveState"] == "inactive" and not data["FragmentPath"], "contradictory absent state")
        return {"present": False, "native": data}
    require(data["LoadState"] == "loaded", "unreadable native unit state")
    return {"present": True, "native": data}


def account_absent():
    """Only a definitive passwd KeyError establishes account absence."""
    hosted_only()
    require(sys.platform == "linux", "Linux account check requested on another platform")
    try:
        pwd.getpwnam(token())
    except KeyError:
        return
    require(False, "disposable proof account is still present")


def parse_stopped_manager(result):
    state = parse_systemd(result)
    require(state["present"] and state["native"].get("ActiveState") == "inactive"
            and state["native"].get("SubState") == "dead",
            "disposable user manager is not verifiably stopped")


def manager_stopped(base):
    hosted_only()
    require(sys.platform == "linux", "Linux manager check requested on another platform")
    created = load(base / "linux-created.json")
    require(created["user"] == token() and created["home"] == str(base / "home"),
            "manager cleanup identity journal mismatch")
    row = pwd.getpwnam(created["user"])
    require(row.pw_uid == created["uid"] and row.pw_dir == created["home"],
            "manager cleanup account identity changed")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(base / "home"), "LANG": "C", "LC_ALL": "C"}
    result = command(["/usr/bin/sudo", "/usr/bin/systemctl", "show", "--no-pager",
                      "--property=LoadState,ActiveState,SubState,FragmentPath",
                      f"user@{row.pw_uid}.service"], env,
                     base / "evidence/linux-manager-postcondition.jsonl", check=False)
    parse_stopped_manager(result)


def launchd_fields(text, label):
    """Read only exact top-level fields from launchctl's nested print format.

    Its human-readable output is not a stable API. Unknown/ambiguous structure
    is INVALID, never permission to remove a label. Nested fields cannot stand
    in for the installed job's identity.
    """
    lines = text.splitlines()
    require(lines and lines[0] == f"gui/{os.getuid()}/{label} = {{", "unexpected launchd job header")
    fields, depth, arguments, in_arguments = {}, 1, [], False
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        require(depth > 0, "trailing launchd query output")
        if line == "}":
            if in_arguments:
                require(depth == 2, "nested launchd arguments")
                fields["arguments"] = tuple(arguments)
                in_arguments = False
            depth -= 1
            continue
        if in_arguments:
            require("{" not in line and "}" not in line, "unsupported launchd argument")
            arguments.append(line)
            continue
        if depth == 1:
            require(" = " in line, "invalid launchd field")
            key, value = line.split(" = ", 1)
            require(key not in fields, "duplicate launchd field")
            fields[key] = value
            if key == "arguments":
                require(value == "{", "invalid launchd arguments")
                in_arguments = True
        # Unrelated nested sections may contain fields and arrows; braces are
        # structural in this deliberately restricted receipt-only fixture.
        depth += line.count("{") - line.count("}")
        require(depth >= 1, "invalid launchd nesting")
    require(depth == 0 and not in_arguments, "incomplete launchd query")
    require({"program", "working directory", "arguments", "path"} <= fields.keys(),
            "incomplete launchd identity")
    return fields


def parse_launchd(result, label):
    if result.returncode == 0:
        require(not result.stderr, "launchd success mixed with error")
        launchd_fields(result.stdout, label)
        return {"present": True, "native": result.stdout}
    absent = f'Could not find service "{label}" in domain for user gui: {os.getuid()}'
    known = {absent, absent + "\n", "Bad request.\n" + absent, "Bad request.\n" + absent + "\n"}
    require(result.returncode == 113 and not result.stdout and result.stderr in known,
            "launchd query failed; cannot establish absence")
    return {"present": False, "native": result.stderr}


def systemd_exec_identity(value):
    # systemctl show prints one brace-delimited record per ExecStart command.
    # The proof's paths/arguments forbid spaces, braces, semicolons and escapes;
    # there is consequently one unambiguous literal argv serialization.
    match = re.fullmatch(r"\{ path=([^;{}]+) ; argv\[\]=([^;{}]+) ; "
                         r"ignore_errors=no ; start_time=\[[^;{}]*\] ; "
                         r"stop_time=\[[^;{}]*\] ; pid=[0-9]+ ; "
                         r"code=[^;{}]+ ; status=[^;{}]+ \}", value)
    require(match is not None, "unsupported native ExecStart format")
    return match.group(1), tuple(match.group(2).split(" "))


class Proof:
    def __init__(self, base, role, *, arm_group=False):
        hosted_only()
        require(role in ROLES, "unknown proof arm")
        self.base, self.role, self.arm_group = base, role, arm_group
        self.state, self.source = base / (role + "-state"), base / (role + "-source")
        self.evidence = base / "evidence" / role
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.env = private_env(base, role)
        self.log = self.evidence / "commands.jsonl"
        self.ext = ".service" if sys.platform == "linux" else ".plist"
        self.units = base / "home" / (".config/systemd/user" if sys.platform == "linux" else "Library/LaunchAgents")
        self.labels = {k: f"org.claudlobby.{token()}.{role}.{k}.worker"
                       for k in ("old", "current", "foreign", "unknown")}
        self.bot = self.state / "root/local/alpha/runtime/bots/worker"
        self.other = self.state / "root/local/beta/runtime/bots/worker"
        self.receipts = self.state / "receipts.log"
        self.receipt_script = self.state / "receipt-only.sh"
        self.journal = self.evidence / "ownership.json"

    def call(self, argv, **kw):
        return command(argv, self.env, self.log, grouped=not self.arm_group, **kw)

    def native(self, args, **kw):
        hosted_only()
        binary = "/usr/bin/systemctl" if sys.platform == "linux" else "/bin/launchctl"
        return self.call([binary, *( ["--user"] if sys.platform == "linux" else []), *args], **kw)

    def query(self, key):
        label = self.labels[key]
        if sys.platform == "linux":
            result = self.native(["show", "--no-pager", "--property=LoadState,ActiveState,SubState,Result,MainPID,FragmentPath,WorkingDirectory,ExecStart,InvocationID,ExecMainPID,ExecMainCode,ExecMainStatus", label + self.ext], check=False)
            return parse_systemd(result)
        return parse_launchd(self.native(["print", f"gui/{os.getuid()}/{label}"], check=False), label)

    def receipt_argv(self, key):
        return ("/usr/bin/env", "-i", "HOME=" + self.env["HOME"], "PATH=" + self.env["PATH"],
                "/bin/bash", "--noprofile", "--norc", str(self.receipt_script),
                self.labels[key], str(self.receipts))

    def identity(self, key, state):
        require(state["present"], "expected native label missing: " + key)
        owner = self.other if key == "foreign" else self.bot
        expected_path = str(self.units / (self.labels[key] + self.ext))
        if sys.platform == "linux":
            native = state["native"]
            require(native["FragmentPath"] == expected_path, "manager loaded foreign fragment")
            require(native.get("WorkingDirectory") == str(owner), "native owner differs")
            program, argv = systemd_exec_identity(native.get("ExecStart", ""))
        else:
            fields = launchd_fields(state["native"], self.labels[key])
            require(fields["path"] == expected_path, "manager loaded foreign plist")
            require(fields["working directory"] == str(owner), "native owner differs")
            program, argv = fields["program"], fields["arguments"]
        require(program == "/usr/bin/env" and argv == self.receipt_argv(key), "native program/argv differs")

    def healthy(self, state):
        if not state["present"]:
            return False
        if sys.platform == "linux":
            row = state["native"]
            return all(row.get(k) == v for k, v in {"ActiveState": "active", "SubState": "exited", "Result": "success", "MainPID": "0"}.items())
        # query already validated the job header; use exact top-level fields,
        # never a matching substring inside an unrelated nested section.
        label = state["native"].splitlines()[0].split("/")[-1].removesuffix(" = {")
        row = launchd_fields(state["native"], label)
        return row.get("state") == "not running" and row.get("last exit code") == "0"

    def await_state(self, key, present):
        deadline = time.monotonic() + 10
        while True:
            value = self.query(key)
            if value["present"] == present and (not present or self.healthy(value)):
                if present:
                    self.identity(key, value)
                return value
            require(time.monotonic() < deadline, "native state deadline: " + key)
            time.sleep(0.1)

    def receipt_count(self, key):
        return self.receipts.read_text().splitlines().count(self.labels[key]) if self.receipts.exists() else 0

    def await_receipt(self, key):
        deadline = time.monotonic() + 10
        while not self.receipt_count(key):
            require(time.monotonic() < deadline, "native receipt deadline: " + key)
            time.sleep(0.1)

    def setup(self):
        require(not self.state.exists(), "arm state must be fresh")
        for p in (self.bot, self.other, self.state / "bin", self.state / "tmp", self.state / "xdg",
                  self.state / "channels", self.state / "sockets", self.state / "root/state/plane", self.units):
            p.mkdir(parents=True, exist_ok=True)
        require(not list(self.units.glob("*" + self.ext)), "private installed-unit directory is not empty")
        for name in UTILITIES:
            target = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
            require(target is not None, "native utility missing: " + name)
            (self.state / "bin" / name).symlink_to(target)
        install_python_entrypoint(self.state / "bin/python3", Path(sys.executable))
        self.call([self.state / "bin/python3", "-c", "import sys; assert sys._installer_network_guard is True"])
        if sys.platform == "linux":
            (self.state / "bin/systemctl").symlink_to("/usr/bin/systemctl")
        for name in FORBIDDEN:
            p = self.state / "bin" / name
            p.write_text("#!/bin/bash\nprintf '%s\\n' " + shlex.quote(name) + " >> " + shlex.quote(str(self.evidence / "forbidden.log")) + "\nexit 97\n")
            p.chmod(0o755)
        if sys.platform == "linux":
            account = pwd.getpwuid(os.getuid())
            require(account.pw_name == token() and Path(account.pw_dir).resolve() == (self.base / "home").resolve(), "not the dedicated Linux proof account")
            runtime = Path(self.env["XDG_RUNTIME_DIR"])
            require(runtime.stat().st_uid == os.getuid() and (runtime / "bus").stat().st_uid == os.getuid(), "manager bus not owned by proof UID")
            paths = self.native(["show", "--property=UnitPath", "--value"]).stdout.split()
            require(str(self.units) in paths, "native manager does not search the proof home")
            save(self.evidence / "manager-binding.json", {"uid": os.getuid(), "home": account.pw_dir, "bus": str(runtime / "bus"), "unit_paths": paths})
        else:
            self.native(["print", f"gui/{os.getuid()}"])
        for key in self.labels:
            require(not self.query(key)["present"], "label already present before proof")

        self.receipt_script.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$1" >> "$2"\n')
        self.receipt_script.chmod(0o755)
        self.bot.joinpath("bot.conf").write_text("export BOT_SERVICE=" + shlex.quote(self.labels["current"]) + "\n")
        # Each arm imports and invokes the real renderer from its pinned source.
        sys.path.insert(0, str(self.source))
        import claudlobby.supervision as supervision
        require(owned(supervision.__file__, self.source), "renderer import escaped source pin")
        rendered = {}
        for key in ("old", "current", "foreign"):
            owner = self.other if key == "foreign" else self.bot
            spec = supervision.SupervisionSpec(
                label=self.labels[key], description="disposable installer ownership proof",
                bot_dir=owner, working_dir=owner, launcher=Path("/usr/bin/env"),
                launcher_args=self.receipt_argv(key)[1:],
                environment={"PLANE_EMIT_DISABLED": "1"}, launchd_environment_extra={},
                stop_command="true", stop_post_command="true",
                stdout_log=owner / "stdout.log", stderr_log=owner / "stderr.log")
            render = supervision.render_systemd_unit if sys.platform == "linux" else supervision.render_launchd_plist
            rendered[key] = render(spec)
        rendered["unknown"] = "malformed ownership sentinel\n"
        hashes = {}
        for key, text in rendered.items():
            path = (self.bot if key == "current" else self.units) / (self.labels[key] + self.ext)
            path.write_text(text)
            hashes[key] = digest(path)
            (self.evidence / (key + self.ext)).write_text(text)
        # Persist BEFORE any enrollment, including an enrollment that errors.
        save(self.journal, {"labels": self.labels, "hashes": hashes, "attempted": [], "preflight_absent": True, "receipt_program_sha": digest(self.receipt_script)})
        save(self.evidence / "environment.json", self.env)

    def record_attempt(self, keys):
        data = load(self.journal)
        data["attempted"] = sorted(set(data["attempted"]) | set(keys))
        save(self.journal, data)

    def enroll_seed(self, key):
        self.record_attempt([key])
        if sys.platform == "linux":
            self.native(["daemon-reload"])
            self.native(["enable", "--now", self.labels[key] + self.ext])
        else:
            self.native(["bootstrap", f"gui/{os.getuid()}", self.units / (self.labels[key] + self.ext)])
        self.await_state(key, True)
        self.await_receipt(key)

    def install(self):
        self.record_attempt(["old", "current", "foreign"])
        script = self.source / "lib" / ("install-bot-systemd.sh" if sys.platform == "linux" else "install-bot.sh")
        return self.call(["/bin/bash", "--noprofile", "--norc", script, self.bot], timeout=60)

    def observe(self, name):
        data = {"observed_at": time.time(), "states": {k: self.query(k) for k in ("old", "current", "foreign")},
                "hashes": {k: digest(p) if p.exists() else None for k in self.labels
                           for p in [self.units / (self.labels[k] + self.ext)]},
                "receipts": {k: self.receipt_count(k) for k in self.labels}}
        save(self.evidence / (name + ".json"), data)
        return data

    def exercise(self):
        self.setup()
        self.enroll_seed("old"); self.enroll_seed("foreign")
        before = self.observe("before")
        self.install()
        self.await_state("current", True); self.await_receipt("current")
        self.await_state("old", False)
        after = self.observe("after-first")
        require(after["hashes"]["old"] is None, "legitimate same-owner rename was not cleaned")
        require(after["hashes"]["current"] == digest(self.bot / (self.labels["current"] + self.ext)), "installed current bytes differ")
        if self.role == "parent":
            require(not after["states"]["foreign"]["present"] and after["hashes"]["foreign"] is None,
                    "parent did not reproduce destructive foreign de-enrollment")
        else:
            self.identity("foreign", after["states"]["foreign"])
            require(self.healthy(after["states"]["foreign"]), "foreign native service stopped or failed")
            require(after["hashes"]["foreign"] == before["hashes"]["foreign"]
                    and after["receipts"]["foreign"] == before["receipts"]["foreign"], "foreign unit changed")
            require(after["hashes"]["unknown"] == before["hashes"]["unknown"], "unknown owner was removed/rewritten")
        second = self.install()
        self.await_state("current", True)
        last = self.observe("after-second")
        require(last["hashes"] == after["hashes"], "reinstall changed installed file inventory")
        require(not last["states"]["old"]["present"], "reinstall resurrected old owner")
        expected_current = after["receipts"]["current"] + (sys.platform == "darwin")
        require(last["receipts"]["current"] == expected_current, "current reinstall receipt behavior changed")
        require(digest(self.receipt_script) == load(self.journal)["receipt_program_sha"], "receipt program changed")
        if self.role == "candidate":
            self.identity("foreign", last["states"]["foreign"])
            require(self.healthy(last["states"]["foreign"]), "reinstall stopped foreign native service")
            require(last["receipts"]["foreign"] == before["receipts"]["foreign"], "reinstall restarted foreign owner")
            require("preserving" in second.stderr and self.labels["unknown"] in second.stderr, "unknown-owner diagnostic absent")
        require(not (self.evidence / "forbidden.log").exists(), "unexpected outbound/model/tool attempt")
        for p in (self.state / "channels", self.state / "sockets", self.state / "root/state/plane"):
            require(not list(p.iterdir()), "unexpected runtime artifact: " + str(p))
        require(not (self.base / "evidence/forbidden-network.jsonl").exists(), "Python network attempted")
        verify_source(self.base, self.role)

    def cleanup(self):
        """Only exact labels proved absent before this arm may be removed."""
        if not self.journal.exists():
            return {"attempted": [], "errors": []}
        journal = load(self.journal)
        require(journal["labels"] == self.labels, "cleanup label journal mismatch")
        require(set(journal["attempted"]) <= {"old", "current", "foreign"}, "unowned cleanup label")
        require(not journal["attempted"] or journal["preflight_absent"], "cleanup lacks initial absence proof")
        errors, observed, blocked = [], {}, set()
        for key in journal["attempted"]:
            try:
                value = self.query(key); observed[key] = value
                if value["present"]:
                    self.identity(key, value)
                    if sys.platform == "linux":
                        self.native(["disable", "--now", self.labels[key] + self.ext])
                    else:
                        self.native(["bootout", f"gui/{os.getuid()}/{self.labels[key]}"])
            except Exception as exc:
                errors.append(key + ": " + str(exc))
                blocked.add(key)
        # Retain exact unit copies under evidence before removing our installed files.
        for key in self.labels:
            path = self.units / (self.labels[key] + self.ext)
            if path.exists() and key not in blocked:
                if digest(path) == journal["hashes"][key]:
                    path.unlink()
                else:
                    errors.append("cleanup refused changed file: " + key)
        if sys.platform == "linux" and journal["attempted"]:
            try:
                self.native(["daemon-reload"])
            except Exception as exc:
                errors.append(str(exc))
        for key in journal["attempted"]:
            try:
                self.await_state(key, False)
            except Exception as exc:
                errors.append("residual " + key + ": " + str(exc))
        result = {"attempted": journal["attempted"], "before_cleanup": observed, "errors": errors, "observed_at": time.time()}
        serial = len(list(self.evidence.glob("cleanup-*.json")))
        save(self.evidence / f"cleanup-{serial}.json", result)
        return result


def arm(base, role):
    require(os.getpid() == os.getpgrp(), "arm requires its own process group")
    proof = Proof(base, role, arm_group=True)
    errors = []
    try:
        verify_source(base, role)
        proof.exercise()
    except Exception as exc:
        errors.append(str(exc))
    finally:
        try:
            errors.extend(proof.cleanup()["errors"])
            verify_source(base, role)
        except Exception as exc:
            errors.append("cleanup/source verification: " + str(exc))
    if proof.receipts.exists():
        shutil.copyfile(proof.receipts, proof.evidence / "receipts.log")
    save(proof.evidence / "result.json", {"role": role, "revision": ROLES[role],
        "verdict": "INVALID" if errors else ("PARENT_RED" if role == "parent" else "CANDIDATE_PASS"), "errors": errors})
    return bool(errors)


def execute(base):
    manifest = load(base / "prepared.json")
    require(manifest["token"] == token() and manifest["controller"] == digest(__file__), "controller/preparation mismatch")
    result = 0
    env = {k: os.environ[k] for k in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "CLAUDLOBBY_INSTALLER_NATIVE_PROOF", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_TEMP")}
    env.update(HOME=str(base / "home"), PATH="/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1")
    for role in ROLES:
        try:
            completed = command([base / (role + "-venv/bin/python"), Path(__file__).resolve(), "--arm", role, "--base", base], env, base / "evidence/arms.jsonl", timeout=180, check=False)
            result |= bool(completed.returncode)
        except Exception as exc:
            save(base / "evidence" / (role + "-controller-error.json"), {"error": str(exc)})
            result = 1
        finally:
            # Independent cleanup also covers an arm killed before its finally.
            try:
                result |= bool(Proof(base, role).cleanup()["errors"])
            except Exception as exc:
                save(base / "evidence" / (role + "-cleanup-error.json"), {"error": str(exc)})
                result = 1
    save(base / "evidence/summary.json", {"invalid": bool(result), "pins": ROLES})
    return result


def cleanup_all(base):
    errors = []
    for role in ROLES:
        try:
            errors.extend(Proof(base, role).cleanup()["errors"])
        except Exception as exc:
            errors.append(role + ": " + str(exc))
    save(base / "evidence/final-cleanup.json", {"errors": errors})
    return bool(errors)


def self_check():
    """Pure parser/refusal controls; no real manager/client execution."""
    import unittest
    import tempfile
    from unittest.mock import patch

    class Controls(unittest.TestCase):
        def test_local_refuses_before_effect(self):
            with patch.dict(os.environ, {}, clear=True), patch.object(subprocess, "Popen", side_effect=AssertionError("native called")):
                with self.assertRaises(RuntimeError):
                    prepare("/nonexistent-installer-proof")

        def test_systemd_absence_requires_complete_success(self):
            good = "LoadState=not-found\nActiveState=inactive\nFragmentPath=\n"
            self.assertFalse(parse_systemd(subprocess.CompletedProcess([], 0, good, ""))["present"])
            for rc, text in ((1, good), (0, ""), (0, good.replace("inactive", "active")), (0, good.replace("not-found", "error"))):
                with self.subTest(rc=rc, text=text), self.assertRaises(RuntimeError):
                    parse_systemd(subprocess.CompletedProcess([], rc, text, "query failed"))

        def test_launchd_absence_requires_exact_result_shape(self):
            label = "org.example.owned.worker"
            good = f'Could not find service "{label}" in domain for user gui: {os.getuid()}'
            for err in (good, good + "\n", "Bad request.\n" + good, "Bad request.\n" + good + "\n"):
                self.assertFalse(parse_launchd(subprocess.CompletedProcess([], 113, "", err), label)["present"])
            bad = [(1, "", good), (0, "", good), (113, "unexpected body", good)]
            bad += [(113, "", err) for err in ("", "Permission denied", good.replace(label, "other"),
                    good + "\nUnknown query failure\n", good.replace("gui:", "user:"),
                    good + "\n\n", good + "Operation not permitted", good + "0")]
            for rc, out, err in bad:
                with self.subTest(rc=rc, out=out, err=err), self.assertRaises(RuntimeError):
                    parse_launchd(subprocess.CompletedProcess([], rc, out, err), label)

        def native_state(self, proof, key):
            owner = proof.other if key == "foreign" else proof.bot
            argv = proof.receipt_argv(key)
            if sys.platform == "linux":
                return {"present": True, "native": {
                    "FragmentPath": str(proof.units / (proof.labels[key] + proof.ext)),
                    "WorkingDirectory": str(owner),
                    "ExecStart": "{ path=/usr/bin/env ; argv[]=" + " ".join(argv)
                        + " ; ignore_errors=no ; start_time=[Sat 2026-09-26 12:00:00 UTC] ; stop_time=[Sat 2026-09-26 12:00:01 UTC] ; pid=123 ; code=exited ; status=0 }"}}
            return {"present": True, "native": (
                f"gui/{os.getuid()}/{proof.labels[key]} = {{\n"
                + f"\tpath = {proof.units / (proof.labels[key] + proof.ext)}\n"
                + "\tprogram = /usr/bin/env\n\targuments = {\n"
                + "".join("\t\t" + arg + "\n" for arg in argv) + "\t}\n"
                + f"\tworking directory = {owner}\n"
                + "\tstate = not running\n\tlast exit code = 0\n"
                + "\tenvironment = {\n\t\tIGNORE => nested\n\t}\n}\n")}

        def test_native_identity_requires_exact_owner_program_and_full_argv(self):
            for platform_name in ("linux", "darwin"):
                with self.subTest(platform=platform_name), tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(sys, "platform", platform_name):
                    proof = Proof(Path(directory), "candidate")
                    good = self.native_state(proof, "foreign")
                    proof.identity("foreign", good)
                    for old, new in ((str(proof.other), str(proof.other) + "-not-ours"),
                                     ("/usr/bin/env", "/usr/bin/env-not-ours"),
                                     (str(proof.receipt_script), str(proof.receipt_script) + "-not-ours"),
                                     (proof.labels["foreign"], proof.labels["foreign"] + "-not-ours"),
                                     (str(proof.receipts), str(proof.receipts) + "-not-ours")):
                        bad = json.loads(json.dumps(good).replace(old, new))
                        with self.subTest(old=old), self.assertRaises(RuntimeError):
                            proof.identity("foreign", bad)
                    if platform_name == "linux":
                        bad = json.loads(json.dumps(good))
                        bad["native"]["ExecStart"] += " " + bad["native"]["ExecStart"]
                        with self.assertRaises(RuntimeError): proof.identity("foreign", bad)
                    else:
                        text = good["native"]
                        parse_launchd(subprocess.CompletedProcess([], 0, text, ""), proof.labels["foreign"])
                        self.assertTrue(proof.healthy(good))
                        for changed in (text.replace("gui/", "user/", 1),
                                        text.replace("\tprogram =", "\tprogram = other\n\tprogram ="),
                                        text.replace("\tworking directory =", "\tworking directory = other\n\tworking directory ="),
                                        text + "unrecognized trailing text\n",
                                        text.replace("\targuments = {", "\targuments = not-a-list"),
                                        text.replace("\tworking directory = " + str(proof.other) + "\n", "")):
                            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                                proof.identity("foreign", {"present": True, "native": changed})
                        self.assertFalse(proof.healthy({"present": True, "native": text.replace("last exit code = 0", "last exit code = 01")}))

        def test_replaced_native_identity_blocks_cleanup_and_file_removal(self):
            for platform_name in ("linux", "darwin"):
                with self.subTest(platform=platform_name), tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(sys, "platform", platform_name):
                    proof = self.cleanup_fixture(Path(directory))
                    bad = json.loads(json.dumps(self.native_state(proof, "foreign")).replace(str(proof.other), str(proof.other) + "-not-ours"))
                    proof.query = lambda key: bad if key == "foreign" else {"present": False}
                    calls = []
                    proof.native = lambda argv: calls.append(argv)
                    proof.await_state = lambda key, present: {"present": False}
                    result = proof.cleanup()
                    self.assertTrue(result["errors"])
                    self.assertTrue((proof.units / (proof.labels["foreign"] + proof.ext)).exists())
                    self.assertFalse(any(proof.labels["foreign"] in str(call) for call in calls))
                    self.assertEqual(result["before_cleanup"]["foreign"], bad)

        def test_python_audit_denies_all_socket_events_before_effect(self):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base / "evidence").mkdir()
                scope = {}
                with patch.object(sys, "addaudithook") as install:
                    exec(network_guard_source(base), scope)
                    self.assertEqual(install.call_args.args, (scope["check"],))
                scope["check"]("subprocess.Popen", ())  # fixed native commands remain available
                for event in ("socket.__new__", "socket.connect", "socket.connect_ex", "socket.bind",
                              "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname",
                              "socket.gethostbyaddr", "socket.getnameinfo"):
                    with self.subTest(event=event), self.assertRaises(PermissionError):
                        scope["check"](event, ())
                self.assertEqual(len((base / "evidence/forbidden-network.jsonl").read_text().splitlines()), 10)

        def test_private_python_entrypoint_keeps_cold_venv_guard(self):
            # Offline interpreter startup only: no package download or service.
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base / "evidence").mkdir()
                env = {"HOME": str(base), "PATH": "/usr/bin:/bin", "TMPDIR": str(base), "PYTHONDONTWRITEBYTECODE": "1"}
                venv = base / "venv"
                subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                               env=env, check=True, capture_output=True, timeout=45)
                python = venv / "bin/python"
                def only_python(argv, child_env, log, **kwargs):
                    self.assertEqual(str(argv[0]), str(python))
                    return subprocess.run(list(map(str, argv)), env=child_env, text=True,
                                          capture_output=True, check=True, timeout=15)
                with patch(__name__ + ".command", side_effect=only_python):
                    install_network_guard(python, base, env)
                (base / "bin").mkdir()
                entry = base / "bin/python3"
                install_python_entrypoint(entry, python)
                probe = ("import sys; assert sys._installer_network_guard is True; import socket\n"
                         "try: socket.getaddrinfo('proof.invalid',80)\n"
                         "except PermissionError: print('blocked')\n"
                         "else: raise AssertionError('DNS escaped')\n")
                for path in (python, entry):
                    result = subprocess.run([str(path), "-c", probe], env=env, text=True,
                                            capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "blocked\n")

        def test_account_absence_distinguishes_present_unknown_and_missing(self):
            with patch(__name__ + ".hosted_only"), patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
                with patch.object(pwd, "getpwnam", side_effect=KeyError):
                    account_absent()
                with patch.object(pwd, "getpwnam", return_value=object()), self.assertRaises(RuntimeError):
                    account_absent()
                for error in (PermissionError("lookup denied"), OSError("lookup unavailable")):
                    with self.subTest(error=error), patch.object(pwd, "getpwnam", side_effect=error), self.assertRaises(OSError):
                        account_absent()

        def test_retired_manager_requires_explicit_successful_inactive_dead_state(self):
            good = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nFragmentPath=/usr/lib/systemd/system/user@.service\n"
            parse_stopped_manager(subprocess.CompletedProcess([], 0, good, ""))
            bad = [(1, good, ""), (0, good, "Permission denied"), (0, "", ""),
                   (0, good.replace("inactive", "active"), ""),
                   (0, good.replace("dead", "running"), ""),
                   (0, good.replace("loaded", "not-found"), ""),
                   (0, good + "ActiveState=inactive\n", "")]
            for rc, out, err in bad:
                with self.subTest(rc=rc, out=out, err=err), self.assertRaises(RuntimeError):
                    parse_stopped_manager(subprocess.CompletedProcess([], rc, out, err))

        def test_manager_postcondition_checks_created_identity_before_native_query(self):
            from types import SimpleNamespace
            with tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
                base = Path(directory)
                created = {"user": token(), "uid": 12345, "home": str(base / "home")}
                save(base / "linux-created.json", created)
                response = subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=inactive\nSubState=dead\nFragmentPath=/usr/lib/systemd/system/user@.service\n", "")
                with patch.object(pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=12345, pw_dir=created["home"])), patch(__name__ + ".command", return_value=response) as query:
                    manager_stopped(base)
                    self.assertEqual(query.call_args.args[0][-1], "user@12345.service")
                    self.assertEqual(query.call_count, 1)
                for row in (SimpleNamespace(pw_uid=12346, pw_dir=created["home"]),
                            SimpleNamespace(pw_uid=12345, pw_dir=created["home"] + "-not-ours")):
                    with patch.object(pwd, "getpwnam", return_value=row), patch(__name__ + ".command", side_effect=AssertionError("native query before owner check")), self.assertRaises(RuntimeError):
                        manager_stopped(base)

        def test_identity_and_role_are_fixed(self):
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
                self.assertEqual(token(), "i1811123a2")
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "bad;value", "GITHUB_RUN_ATTEMPT": "2"}):
                with self.assertRaises(RuntimeError): token()
            self.assertEqual(set(ROLES), {"parent", "candidate"})

        def cleanup_fixture(self, base):
            proof = Proof(base, "candidate")
            proof.units.mkdir(parents=True)
            hashes = {}
            for key in proof.labels:
                path = proof.units / (proof.labels[key] + proof.ext)
                path.write_text("owned fixture " + key)
                hashes[key] = digest(path)
            save(proof.journal, {"labels": proof.labels, "hashes": hashes,
                                "preflight_absent": True, "attempted": ["old", "foreign", "current"]})
            return proof

        def test_partial_enrollment_without_success_receipt_is_still_queried(self):
            with tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(sys, "platform", "linux"):
                proof = self.cleanup_fixture(Path(directory))
                queried, calls = [], []
                def query(key):
                    queried.append(key)
                    return {"present": key == "foreign", "native": "modeled"}
                proof.query = query
                proof.identity = lambda key, value: None
                proof.native = lambda argv: calls.append(argv)
                proof.await_state = lambda key, present: {"present": False}
                result = proof.cleanup()
                self.assertEqual(queried, ["old", "foreign", "current"])
                self.assertEqual(calls[0], ["disable", "--now", proof.labels["foreign"] + proof.ext])
                self.assertEqual(result["errors"], [])
                self.assertTrue(result["before_cleanup"]["foreign"]["present"])

        def test_unreadable_state_preserves_file_and_invalidates_cleanup(self):
            with tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}), patch.object(sys, "platform", "linux"):
                proof = self.cleanup_fixture(Path(directory))
                def query(key):
                    if key == "foreign":
                        raise RuntimeError("query denied")
                    return {"present": False}
                proof.query = query
                proof.native = lambda argv: None
                proof.await_state = lambda key, present: {"present": False}
                result = proof.cleanup()
                self.assertTrue(result["errors"])
                self.assertTrue((proof.units / (proof.labels["foreign"] + proof.ext)).exists())
                proof.cleanup()
                self.assertTrue((proof.evidence / "cleanup-0.json").exists())
                self.assertTrue((proof.evidence / "cleanup-1.json").exists())

        def test_cleanup_refuses_unclaimed_labels_before_native_calls(self):
            with tempfile.TemporaryDirectory() as directory, patch(__name__ + ".hosted_only"), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}):
                proof = self.cleanup_fixture(Path(directory))
                row = load(proof.journal); row["attempted"].append("unknown"); save(proof.journal, row)
                proof.native = lambda *a, **kw: self.fail("native call before ownership check")
                with self.assertRaises(RuntimeError): proof.cleanup()

        def test_private_environment_ignores_ambient_startup_and_model_state(self):
            with patch.dict(os.environ, {"BASH_ENV": "/foreign/startup", "CLAUDE_CONFIG_DIR": "/foreign/account", "PLANE_SOCKET": "/foreign/socket", "HOME": "/foreign/home"}):
                env = private_env(Path("/private-proof"), "candidate")
            self.assertNotIn("BASH_ENV", env)
            self.assertNotIn("CLAUDE_CONFIG_DIR", env)
            self.assertEqual(env["HOME"], "/private-proof/home")
            self.assertTrue(env["PLANE_SOCKET"].startswith("/private-proof/"))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Controls)
    return not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--arm", choices=tuple(ROLES))
    mode.add_argument("--cleanup", action="store_true")
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--account-absent", action="store_true")
    mode.add_argument("--manager-stopped", action="store_true")
    parser.add_argument("--base")
    args = parser.parse_args()
    if args.self_check:
        raise SystemExit(self_check())
    base = base_path(args.base)
    if not args.prepare:
        exec(network_guard_source(base), {})
    if args.account_absent:
        account_absent()
    elif args.manager_stopped:
        manager_stopped(base)
    elif args.prepare:
        prepare(base)
    elif args.run:
        raise SystemExit(execute(base))
    elif args.arm:
        raise SystemExit(arm(base, args.arm))
    else:
        raise SystemExit(cleanup_all(base))
