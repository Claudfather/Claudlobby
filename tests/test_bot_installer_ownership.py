"""Installer cleanup may remove only a unit owned by the same bot directory.

Both real installers run against rendered units in scratch roots. The child
PATH is closed, every supervisor command records only, and the Darwin script
copy replaces its absolute supervisor path before it can run.
"""

from __future__ import annotations

import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.supervision import (
    SupervisionSpec,
    render_launchd_plist,
    render_systemd_unit,
)

REPO = Path(__file__).resolve().parents[1]


class Installer:
    def __init__(self, tmp_path: Path, flavor: str):
        self.flavor = flavor
        self.root = tmp_path / "root"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.lib = self.root / "lib"
        self.bot = self.root / "local" / "alpha" / "runtime" / "bots" / "worker"
        self.label = "com.alpha.worker"
        self.log = tmp_path / "supervisor.log"
        self.unexpected = tmp_path / "unexpected.log"
        self.installed = self.home / (
            "Library/LaunchAgents" if flavor == "plist" else ".config/systemd/user"
        )
        for path in (self.lib, self.bin, self.bot, self.installed,
                     tmp_path / "tmp", tmp_path / "xdg", tmp_path / "sockets",
                     tmp_path / "channels", self.root / "state" / "plane"):
            path.mkdir(parents=True, exist_ok=True)
        for name in ("lib-common.sh", "supervisor.sh", "bot-unit-owner.py"):
            source = REPO / "lib" / name
            if source.exists():  # The red arm has no ownership reader yet.
                shutil.copyfile(source, self.lib / name)
        self.script = self.lib / (
            "install-bot.sh" if flavor == "plist" else "install-bot-systemd.sh"
        )
        source = (REPO / "lib" / self.script.name).read_text()
        if flavor == "plist":
            assert source.count("/bin/launchctl") == 3
            source = source.replace("/bin/launchctl", shlex.quote(str(self.bin / "launchctl")))
        assert not re.search(r"(?:^|\s)/bin/(?:launchctl|systemctl)\b", source)
        self.script.write_text(source)

        # No inherited PATH segment can accidentally resolve a real supervisor.
        for name in ("basename", "dirname", "mkdir", "id", "cp", "rm", "grep",
                     "sed", "tr", "awk", "date", "mktemp"):
            target = shutil.which(name)
            assert target is not None
            (self.bin / name).symlink_to(target)
        (self.bin / "python3").symlink_to(sys.executable)
        self.stub("uname", "printf '%s\\n' " + ("Darwin" if flavor == "plist" else "Linux"))
        for name in ("systemctl", "launchctl"):
            self.stub(name, "{ printf '%s' '" + name
                      + "'; printf ' <%s>' \"$@\"; printf '\\n'; } >> \"$SUPERVISOR_LOG\"")
        for name in ("tmux", "curl", "gh", "claudlobby", "telegram"):
            self.stub(name, "printf '%s\\n' '" + name + "' >> \"$UNEXPECTED_LOG\"; exit 97")
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.home), "USER": "fixture",
            "LANG": "C.UTF-8", "CLAUDLOBBY_ROOT": str(self.root),
            "PLANE_EMIT_DISABLED": "1", "SUPERVISOR_LOG": str(self.log),
            "UNEXPECTED_LOG": str(self.unexpected),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg"),
            "TMPDIR": str(tmp_path / "tmp"), "TMUX_TMPDIR": str(tmp_path / "sockets"),
            "TMUX_BIN": str(self.bin / "tmux"),
            "TELEGRAM_STATE_DIR": str(tmp_path / "channels"),
        }
        for name in ("systemctl", "launchctl"):
            assert shutil.which(name, path=self.env["PATH"]) == str(self.bin / name)
            assert not (self.bin / name).is_symlink()
        self.bot.joinpath("bot.conf").write_text(f"export BOT_SERVICE='{self.label}'\n")
        self.bot.joinpath(f"{self.label}.{flavor}").write_text(self.unit(self.label, self.bot))

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def unit(self, label: str, owner: Path) -> str:
        spec = SupervisionSpec(
            label=label, description="fixture", bot_dir=owner,
            launcher=self.lib / "start-bot.sh", launcher_args=(str(owner),),
            working_dir=owner, environment={"CLAUDLOBBY_ROOT": str(self.root)},
            launchd_environment_extra={"HOME": str(self.home)},
            stop_command="true", stop_post_command="true",
            stdout_log=owner / "stdout", stderr_log=owner / "stderr",
        )
        return render_launchd_plist(spec) if self.flavor == "plist" else render_systemd_unit(spec)

    def add(self, label: str, owner: Path | None = None, *, text: str | None = None) -> Path:
        path = self.installed / f"{label}.{self.flavor}"
        path.write_text(text if text is not None else self.unit(label, owner or self.bot))
        return path

    def run(self) -> subprocess.CompletedProcess:
        result = subprocess.run(["/bin/bash", str(self.script), str(self.bot)],
                                env=self.env, cwd=self.root, capture_output=True,
                                text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not self.unexpected.exists(), self.unexpected.read_text() if self.unexpected.exists() else ""
        assert not list((self.root / "state" / "plane").iterdir())
        return result

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture(params=["plist", "service"])
def installer(tmp_path, request):
    return Installer(tmp_path, request.param)


def test_sibling_same_name_is_untouched(installer):
    other = installer.root / "local" / "beta" / "runtime" / "bots" / "worker"
    sibling = installer.add("com.beta.worker", other)
    before = sibling.read_bytes()
    installer.run()
    assert sibling.exists(), "same-name sibling was removed"
    assert sibling.read_bytes() == before
    assert "com.beta.worker" not in installer.calls()


def test_same_directory_prefix_rename_is_removed(installer):
    stale = installer.add("com.old.worker")
    installer.run()
    assert not stale.exists()
    assert "com.old.worker" in installer.calls()


@pytest.mark.parametrize("bad", ["missing", "malformed", "duplicate", "relative", "empty"])
def test_unknown_owner_is_preserved(installer, bad):
    if installer.flavor == "plist":
        content = {} if bad == "missing" else {"WorkingDirectory": {
            "relative": "relative/bot", "empty": "",
        }.get(bad, str(installer.bot))}
        text = plistlib.dumps(content).decode()
        if bad == "malformed":
            text = "not a plist"
        elif bad == "duplicate":
            text = text.replace("</dict>", f"<key>WorkingDirectory</key><string>{installer.bot}</string></dict>")
    else:
        value = {"relative": "relative/bot", "empty": ""}.get(bad, str(installer.bot))
        text = "[Service]\n"
        if bad != "missing":
            text += f"WorkingDirectory={value}\n"
        if bad == "duplicate":
            text += f"WorkingDirectory={value}\n"
        elif bad == "malformed":
            text = "not a unit"
    stale = installer.add("com.unknown.worker", text=text)
    before = stale.read_bytes()
    result = installer.run()
    assert stale.exists(), f"{bad} ownership authorized removal"
    assert stale.read_bytes() == before
    assert "com.unknown.worker" not in installer.calls()
    assert "preserving" in result.stderr and stale.name in result.stderr


def test_foreign_root_is_preserved(installer, tmp_path):
    stale = installer.add("com.foreign.worker", tmp_path / "foreign" / "bots" / "worker")
    before = stale.read_bytes()
    installer.run()
    assert stale.exists()
    assert stale.read_bytes() == before
    assert "com.foreign.worker" not in installer.calls()


def test_current_unit_is_not_a_cleanup_candidate(installer):
    current = installer.add(installer.label)
    before = current.read_bytes()
    first = installer.run()
    second = installer.run()
    assert current.read_bytes() == before
    assert "stale" not in first.stdout + second.stdout
    assert "disable" not in installer.calls()
    assert installer.calls().count("bootstrap" if installer.flavor == "plist" else "enable") == 2


def test_ownership_paths_with_spaces(tmp_path):
    for flavor in ("plist", "service"):
        harness = Installer(tmp_path / f"with spaces {flavor}", flavor)
        stale = harness.add("com.old.worker")
        harness.run()
        assert not stale.exists()
        assert "com.old.worker" in harness.calls()


@pytest.mark.parametrize("failure", ["missing_parser", "missing_python", "crashed_parser"])
def test_parser_failure_never_authorizes_removal(installer, failure):
    stale = installer.add("com.old.worker")
    before = stale.read_bytes()
    parser = installer.lib / "bot-unit-owner.py"
    if failure == "missing_parser":
        parser.unlink(missing_ok=True)
    elif failure == "missing_python":
        (installer.bin / "python3").unlink()
    else:
        parser.write_text("raise RuntimeError('synthetic reader failure')\n")
    result = installer.run()
    assert stale.exists()
    assert stale.read_bytes() == before
    assert "com.old.worker" not in installer.calls()
    assert "preserving" in result.stderr


def test_canonical_directory_ownership(installer, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(installer.bot, target_is_directory=True)
    stale = installer.add("com.alias.worker", alias)
    installer.run()
    assert not stale.exists()


@pytest.mark.parametrize("shape", ["quoted", "specifier", "continuation", "wrong_section"])
def test_unsupported_systemd_ownership_is_preserved(tmp_path, shape):
    harness = Installer(tmp_path, "service")
    body = {
        "quoted": f'[Service]\nWorkingDirectory="{harness.bot}"\n',
        "specifier": f"[Service]\nWorkingDirectory={harness.bot}/%n\n",
        "continuation": f"[Service]\nEnvironment=VALUE=first \\\nWorkingDirectory={harness.bot}\n",
        "wrong_section": f"[Unit]\nWorkingDirectory={harness.bot}\n[Service]\n",
    }[shape]
    stale = harness.add("com.unsupported.worker", text=body)
    harness.run()
    assert stale.read_text() == body
    assert "com.unsupported.worker" not in harness.calls()


@pytest.mark.parametrize("owner_kind, expected_rc", [
    ("own", 0), ("foreign", 1), ("missing", 3), ("unreadable", 3),
])
def test_reader_return_contract(installer, tmp_path, owner_kind, expected_rc):
    owner = installer.bot if owner_kind == "own" else tmp_path / "other"
    unit = installer.add("com.reader.worker", owner)
    if owner_kind in ("missing", "unreadable"):
        unit.unlink()
        if owner_kind == "unreadable":
            unit.mkdir()  # A read error independent of whether the test uid is root.
    result = subprocess.run(
        [str(installer.bin / "python3"), str(installer.lib / "bot-unit-owner.py"),
         str(unit), str(installer.bot)],
        env=installer.env, cwd=installer.root, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == expected_rc
    assert result.stdout == result.stderr == ""
