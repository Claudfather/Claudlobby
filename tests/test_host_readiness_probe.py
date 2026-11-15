"""Preinstall inventory is bounded data; prerequisites are never executed."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.conftest import constructed_env

PROBE = Path(__file__).resolve().parents[1] / "claudlobby/preinstall/host-readiness-probe.sh"
TOOLS = ("python3", "git", "tailscale", "claude", "tmux", "jq", "systemctl", "launchctl")


@pytest.fixture
def inventory(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "executed-prerequisite"
    for name in ("dd", "tr"):
        (bindir / name).symlink_to(shutil.which(name))

    def run(*, system="Linux", architecture=b"aarch64\n", uid=b"1001\n",
            user=b"example-user\n", present=TOOLS, args=(), failed=None, reader=None):
        if reader:
            name, mode = reader
            path = bindir / name
            path.unlink()
            if mode != "missing":
                executable = shutil.which(name)
                command = "'" + executable + "' \"$@\"\n" if mode == "failed-after-output" else ""
                path.write_text("#!/bin/sh\n" + command + "exit 1\n")
                path.chmod(0o755)
        # uname/id accept exactly the operation's fixed fact arguments.
        for name, variants in (("uname", {"-s": system.encode() + b"\n", "-m": architecture}),
                               ("id", {"-u": uid, "-un": user})):
            lines = ["#!/bin/sh", 'case "$*" in']
            for argument, value in variants.items():
                data = tmp_path / (name + argument + "-output")
                data.write_bytes(value)
                lines.append(argument + ") /bin/cat '" + str(data) + "'; exit "
                             + ("1" if failed == name + argument else "0") + ";;")
            lines.extend(["*) exit 99;;", "esac", ""])
            path = bindir / name
            path.write_text("\n".join(lines))
            path.chmod(0o755)
        for name in TOOLS:
            path = bindir / name
            if path.exists():
                path.unlink()
            if name in present:
                path.write_text("#!/bin/sh\nprintf invoked > '" + str(marker) + "'\nexit 99\n")
                path.chmod(0o755)
        result = subprocess.run(["/bin/sh", str(PROBE), *args],
            env=constructed_env(PATH=bindir, USER="forged", LOGNAME="forged"),
            input=b"ignored input", capture_output=True, timeout=5)
        assert result.stderr == b""
        assert len(result.stdout) <= 8192
        assert result.stdout.endswith(b"\n") and result.stdout.count(b"\n") == 1
        assert not marker.exists(), "inventory executed a prerequisite or native-manager CLI"
        return result, json.loads(result.stdout)

    return run


@pytest.mark.parametrize("system,expected", [("Linux", "systemd_user"), ("Darwin", "launchd_user")])
def test_presence_never_claims_functional_readiness(inventory, system, expected):
    result, data = inventory(system=system)
    assert result.returncode == 0
    assert data["schema_version"] == 1
    assert data["operation"] == "host-readiness-v1"
    assert data["os"] == {"Linux": "linux", "Darwin": "macos"}[system]
    assert data["architecture"] == "aarch64"
    assert data["account"] == {"uid": 1001, "name": "example-user"}
    for name in TOOLS[:6]:
        assert data["tools"][name] == {"present": True, "version": None, "readiness": "unknown"}
    assert data["native_manager"] == {"expected": expected,
        "systemctl_present": True, "launchctl_present": True,
        "readiness": "unknown", "lingering": "unknown"}
    assert data["checks"] == {"python_minimum_version": "unknown", "python_venv": "unknown",
        "python_ssl": "unknown", "provider_authentication": "unknown",
        "tailscale_access": "unknown"}


def test_absent_tools_do_not_hide_unknown_functional_checks(inventory):
    result, data = inventory(present=())
    assert result.returncode == 0
    assert all(value == {"present": False, "version": None, "readiness": "unknown"}
               for value in data["tools"].values())
    assert data["native_manager"]["systemctl_present"] is False
    assert data["native_manager"]["launchctl_present"] is False
    assert data["native_manager"]["readiness"] == "unknown"


@pytest.mark.parametrize("change", [
    {"uid": b"01\n"}, {"uid": b"-1\n"}, {"uid": b"1001,\"root\":true\n"},
    {"user": b'bad"user\n'}, {"user": b"name\nother\n"}, {"user": b"x" * 1024},
    {"user": b"a\x00lice\n"}, {"user": b"\xc3\xa9\n"}, {"user": b"\n"}, {"architecture": b"arm64\nextra\n"},
    {"architecture": b"x" * 1024}, {"system": 'Linux"'},
    {"failed": "id-u"}, {"failed": "id-un"}, {"failed": "uname-s"}, {"failed": "uname-m"},
], ids=["leading-zero-uid", "negative-uid", "uid-injection", "quote-user", "multiline-user",
        "oversize-user", "nul-user", "non-ascii-user", "empty-user", "multiline-arch", "oversize-arch",
        "invalid-os", "uid-failure", "user-failure", "os-failure", "arch-failure"])
def test_malformed_or_failed_facts_refuse_without_echoing_output(inventory, change):
    result, data = inventory(**change)
    assert result.returncode == 1
    assert data == {"schema_version": 1, "operation": "host-readiness-v1", "error": "inventory_unavailable"}


def test_arguments_cannot_select_an_operation_or_command(inventory):
    result, data = inventory(args=("--command", "claude"))
    assert result.returncode == 2
    assert data["error"] == "arguments_not_supported"


def test_inventory_runs_in_standalone_posix_shell_with_real_local_facts(tmp_path):
    result = subprocess.run(["/bin/sh", str(PROBE)], cwd=tmp_path,
        env=constructed_env(), input=b"", capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    data = json.loads(result.stdout)
    assert data["account"]["uid"] == os.getuid()
    assert len(result.stdout) <= 8192 and result.stdout.count(b"\n") == 1


@pytest.mark.parametrize("user", [b"alice\n\n0" + b"\n" * 1024,
                                 b"alice\n\nS0" + b"\n" * 1024],
                         ids=["legacy-status-spoof", "framed-status-spoof"])
def test_truncated_newline_suffix_cannot_forge_a_complete_fact(inventory, user):
    result, data = inventory(user=user)
    assert result.returncode == 1
    assert data["error"] == "inventory_unavailable"


@pytest.mark.parametrize("name", ["dd", "tr"])
@pytest.mark.parametrize("mode", ["missing", "failed-empty", "failed-after-output"])
def test_reader_failure_refuses_even_after_complete_output(inventory, name, mode):
    result, data = inventory(reader=(name, mode))
    assert result.returncode == 1
    assert data["error"] == "inventory_unavailable"


@pytest.mark.parametrize("length,expected_status", [(64, 0), (65, 1)])
def test_account_name_length_limit(inventory, length, expected_status):
    result, data = inventory(user=b"u" * length + b"\n")
    assert result.returncode == expected_status
    if expected_status == 0:
        assert data["account"]["name"] == "u" * 64
    else:
        assert data["error"] == "inventory_unavailable"
