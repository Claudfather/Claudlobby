"""#1604: launch a pinned npx MCP server as `node <entry>`, with no npm wrapper.

`npx -y <pkg>@<ver>` leaves `npm exec` resident as the PARENT of the server it
starts, plus a `sh -c` shim between them. Measured on the Pi, 2026-09-29: 41
wrappers holding 45 MB private and 1,388 MB of swap, a third of the swap file,
while doing nothing. The copy `warm-cache` installs under
`$CLAUDLOBBY_ROOT/state/mcp/npm/<name>@<version>/` lets the composer launch
the same pinned package directly.

Three rules carry the weight, and each has a test below:

* OFF is byte-identical. The `mcp_direct_launch` key is on unless a bot or a
  fleet's defaults turn it off, and a bot that opts out composes exactly the
  npx launch it always had.
* ARMED but unusable falls back to today's npx launch and SAYS so. A copy that
  is missing, a spec that is not an exact pin, or an entry point that is not a
  plain node script keeps npx: that form cannot break a server, it only forgoes
  the saving, and the warning keeps the forgone saving visible.
* The install is ATOMIC. `npm install` runs into a temporary sibling that is
  renamed into place, so a half-finished install is never what a compose finds.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from claudlobby.commands.core import cmd_warm_cache
from tests.conftest import (
    SubprocessRecorder,
    equip_bot_with_mcp,
    load_test_fleet,
    make_paths,
    warm_cache_args,
)

SPEC = "@scope/demo-mcp@1.2.3"
BARE = "@scope/demo-mcp"
VERSION = "1.2.3"
NPX = {"command": "npx", "args": ["-y", SPEC, "--flag"], "env": {"TOKEN": "${TOKEN}"}}


@pytest.fixture(autouse=True)
def _equip_grammar(fleet_dir):
    """compose and warm-cache read the pin and bin grammar from the install's
    `claudlobby/_runtime_scripts/`, and that door refuses rather than falling back, so the fixture root
    carries the real file (the test_warm_cache_uvx.py fixture, same reason)."""
    from tests.conftest import equip_grammar

    equip_grammar(fleet_dir)


def _arm(fleet_dir: Path, *, where: str = "lead", value: str = "true") -> None:
    """Set `mcp_direct_launch` on one bot, or on `defaults` with where='defaults'."""
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    if where == "defaults":
        anchor = "  defaults:\n    model: opus\n"
        after = before.replace(anchor, anchor + f"    mcp_direct_launch: {value}\n")
    else:
        anchor = f"    {where}:\n"
        after = before.replace(
            anchor, anchor + f"      mcp_direct_launch: {value}\n", 1
        )
    assert after != before, "fixture anchor moved: the key was not written"
    fy.write_text(after)


def _package_dir(root: Path, bare: str = BARE, version: str = VERSION) -> Path:
    return root / "state" / "mcp" / "npm" / f"{bare}@{version}" / "node_modules" / bare


def _write_package(
    pkg_dir: Path,
    *,
    name: str = BARE,
    bin_field=None,
    entry: str = "dist/index.js",
    first_line: str = "#!/usr/bin/env node",
) -> Path:
    """Lay out an installed package the way npm leaves one, and return its entry."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    if bin_field is None:
        bin_field = {"demo-mcp": entry}
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": name, "version": VERSION, "bin": bin_field})
    )
    path = pkg_dir / entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(first_line + "\nconsole.log('server')\n")
    return path


def _equip_with_global_binary(fleet_dir: Path) -> None:
    """The demo fragment with a fragment-level `_global_binary`, equipped on lead."""
    equip_bot_with_mcp(fleet_dir, {"demo": NPX})
    (fleet_dir / "library" / "mcp" / "demo.json").write_text(
        json.dumps({"demo": NPX, "_global_binary": "demo-mcp"})
    )


def _compose(fleet_dir: Path, bot: str = "lead") -> dict:
    from claudlobby.composer import compose_mcp_json

    fleet = load_test_fleet(fleet_dir)
    return compose_mcp_json(fleet.bots[bot], make_paths(fleet_dir))["mcpServers"]


# --- the manifest key ------------------------------------------------------


class TestTheKeyIsPerBot:
    def test_a_bot_that_does_not_set_it_is_on(self, fleet_dir: Path):
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].mcp_direct_launch is True

    def test_one_bot_can_opt_out(self, fleet_dir: Path):
        _arm(fleet_dir, where="lead", value="false")
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].mcp_direct_launch is False
        assert fleet.bots["worker-1"].mcp_direct_launch is True

    def test_defaults_widen_it_and_a_bot_can_stay_out(self, fleet_dir: Path):
        _arm(fleet_dir, where="defaults")
        _arm(fleet_dir, where="worker-1", value="false")
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].mcp_direct_launch is True
        assert fleet.bots["worker-1"].mcp_direct_launch is False

    def test_a_string_is_refused_rather_than_read_as_true(self, fleet_dir: Path):
        _arm(fleet_dir, where="lead", value='"yes"')
        with pytest.raises(ValueError, match="mcp_direct_launch"):
            load_test_fleet(fleet_dir)


# --- composition ----------------------------------------------------------


class TestAnArmedBotLaunchesTheInstalledCopy:
    def test_node_runs_the_entry_point_and_keeps_the_server_args(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        entry = _write_package(_package_dir(fleet_dir))
        server = _compose(fleet_dir)["demo"]
        assert server["command"] == "node"
        assert server["args"] == [str(entry), "--flag"]
        assert server["env"] == {"TOKEN": "${TOKEN}"}, "env must ride through unchanged"

    def test_a_dot_slash_entry_is_normalised(self, fleet_dir: Path):
        # @ajackus/shopify-mcp-server ships `"bin": {"...": "./build/index.js"}`.
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        pkg = _package_dir(fleet_dir)
        _write_package(
            pkg, bin_field={"demo-mcp": "./build/index.js"}, entry="build/index.js"
        )
        server = _compose(fleet_dir)["demo"]
        assert server["args"][0] == str(pkg / "build" / "index.js")

    def test_a_string_bin_is_the_package_entry(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        entry = _write_package(_package_dir(fleet_dir), bin_field="dist/index.js")
        assert _compose(fleet_dir)["demo"]["args"][0] == str(entry)

    def test_with_several_bins_the_one_named_after_the_package_wins(
        self, fleet_dir: Path
    ):
        # npx's own rule (libnpmexec getBinFromManifest), and mcp-remote's shape:
        # `mcp-remote` and `mcp-remote-client`, where npx runs `mcp-remote`.
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        pkg = _package_dir(fleet_dir)
        _write_package(pkg, entry="dist/client.js")
        entry = _write_package(
            pkg,
            entry="dist/proxy.js",
            bin_field={
                "demo-mcp-client": "dist/client.js",
                "demo-mcp": "dist/proxy.js",
            },
        )
        assert _compose(fleet_dir)["demo"]["args"][0] == str(entry)

    def test_the_installed_copy_beats_a_global_binary(
        self, fleet_dir: Path, monkeypatch
    ):
        # A PATH-global binary is whatever version someone installed; the
        # state/mcp copy is the fragment's exact pin, so it wins when both exist.
        import shutil as _shutil

        _equip_with_global_binary(fleet_dir)
        _arm(fleet_dir)
        entry = _write_package(_package_dir(fleet_dir))
        monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/local/bin/demo-mcp")
        server = _compose(fleet_dir)["demo"]
        assert server["args"] == [str(entry), "--flag"]


class TestOffIsByteIdentical:
    def test_an_unarmed_bot_ignores_an_installed_copy(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir, where="lead", value="false")
        before = _compose(fleet_dir)
        _write_package(_package_dir(fleet_dir))
        after = _compose(fleet_dir)
        assert after == before
        assert after["demo"] == {
            "command": "npx",
            "args": ["-y", SPEC, "--flag"],
            "env": {"TOKEN": "${TOKEN}"},
        }


class TestAnArmedBotFallsBackToNpxAndSaysSo:
    def _fallback(self, fleet_dir: Path, caplog) -> tuple[dict, str]:
        with caplog.at_level(logging.WARNING, logger="claudlobby.composer"):
            server = _compose(fleet_dir)["demo"]
        return server, caplog.text

    def test_a_missing_copy_keeps_npx_and_names_the_fix(self, fleet_dir: Path, caplog):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert server["args"] == ["-y", SPEC, "--flag"]
        assert "lead" in text and "demo" in text and SPEC in text
        assert "not installed" in text and "host cache warm" in text
        assert "config plan" in text

    def test_a_version_range_keeps_npx(self, fleet_dir: Path, caplog):
        # A bare name falls out on its own (no version to install); a RANGE has
        # a version part, so only the exact-pin rule keeps it off the install.
        ranged = {"command": "npx", "args": ["-y", f"{BARE}@^{VERSION}"]}
        equip_bot_with_mcp(fleet_dir, {"demo": ranged})
        _arm(fleet_dir)
        _write_package(_package_dir(fleet_dir, version=f"^{VERSION}"))
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert "not an exact version pin" in text

    def test_a_bin_outside_the_package_keeps_npx(self, fleet_dir: Path, caplog):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        pkg = _package_dir(fleet_dir)
        _write_package(pkg, bin_field={"demo-mcp": "../../escape.js"}, entry="index.js")
        (pkg.parent.parent / "escape.js").write_text("#!/usr/bin/env node\n")
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert "outside the package" in text

    def test_a_global_binary_swap_is_not_reported_as_a_fallback(self, fleet_dir: Path, monkeypatch, caplog):
        # No installed copy, but a PATH-global binary resolves: the older swap
        # runs, so the server carries no wrapper and is not "still on npx".
        import shutil as _shutil

        _equip_with_global_binary(fleet_dir)
        _arm(fleet_dir)
        monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/local/bin/demo-mcp")
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "node"
        assert server["args"] == ["/usr/local/bin/demo-mcp", "--flag"]
        assert "still launch through npx" not in text

    def test_an_unpinned_spec_keeps_npx(self, fleet_dir: Path, caplog):
        equip_bot_with_mcp(
            fleet_dir, {"demo": {"command": "npx", "args": ["-y", BARE]}}
        )
        _arm(fleet_dir)
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert "not an exact version pin" in text
        assert "host cache warm" not in text, "cache warming cannot fix an unpinned spec"

    def test_an_entry_with_a_flagged_shebang_keeps_npx(self, fleet_dir: Path, caplog):
        # `node <path>` would drop the flags a `#!/usr/bin/env -S node --x` asks for.
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        _write_package(
            _package_dir(fleet_dir),
            first_line="#!/usr/bin/env -S node --experimental-vm-modules",
        )
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert "plain node script" in text

    def test_a_copy_whose_entry_is_missing_keeps_npx(self, fleet_dir: Path, caplog):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        entry = _write_package(_package_dir(fleet_dir))
        entry.unlink()
        server, _text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"

    def test_several_bins_with_none_named_after_the_package_keeps_npx(
        self, fleet_dir: Path, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        pkg = _package_dir(fleet_dir)
        _write_package(pkg, entry="a.js")
        _write_package(pkg, entry="b.js", bin_field={"one": "a.js", "two": "b.js"})
        server, text = self._fallback(fleet_dir, caplog)
        assert server["command"] == "npx"
        assert "which bin" in text

    def test_a_uvx_server_is_untouched(self, fleet_dir: Path, caplog):
        uvx = {"command": "uvx", "args": ["--from", "pkg==1.0.0", "entry"]}
        equip_bot_with_mcp(fleet_dir, {"demo": uvx})
        _arm(fleet_dir)
        server, text = self._fallback(fleet_dir, caplog)
        assert server == uvx
        assert "npx" not in text, (
            "a uvx server is not a fallback and must not be reported as one"
        )


# --- warm-cache installs the copy ------------------------------------------


class _FakeNpm(SubprocessRecorder):
    """`npm install --prefix <dir> ... <spec>` lays the package out as npm would;
    `fail=True` leaves a partial tree and exits 1."""

    def __init__(self, *, fail: bool = False):
        super().__init__()
        self.fail = fail

    def __call__(self, argv, *a, **kw):
        result = super().__call__(argv, *a, **kw)
        if argv[:2] == ["npm", "install"]:
            prefix = Path(argv[argv.index("--prefix") + 1])
            pkg = prefix / "node_modules" / BARE
            if self.fail:
                pkg.mkdir(parents=True, exist_ok=True)
                (pkg / "package.json").write_text("{")  # torn mid-write
                return subprocess.CompletedProcess(
                    argv, 1, "", "npm ERR! network ETIMEDOUT"
                )
            _write_package(pkg)
        return result


class TestWarmCacheInstallsForArmedBots:
    def _run(
        self, fleet_dir: Path, monkeypatch, npm: _FakeNpm, *, dry_run=False, summary=None
    ) -> int:
        monkeypatch.setattr(subprocess, "run", npm)
        return cmd_warm_cache(warm_cache_args(fleet_dir, dry_run=dry_run), summary=summary)

    def _installs(self, npm: _FakeNpm) -> list[list[str]]:
        return [c for c in npm.argv_for("npm") if c[1:2] == ["install"]]

    def test_the_pinned_package_lands_in_state_mcp(self, fleet_dir: Path, monkeypatch):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        npm = _FakeNpm()
        assert self._run(fleet_dir, monkeypatch, npm) == 0
        [call] = self._installs(npm)
        assert call[-1] == SPEC
        assert {"--prefer-offline", "--no-audit", "--no-fund"} <= set(call)
        prefix = Path(call[call.index("--prefix") + 1])
        final = fleet_dir / "state" / "mcp" / "npm" / SPEC
        assert prefix != final and prefix.parent == final.parent, (
            "npm must install into a sibling of the final dir, never the final dir"
        )
        assert (_package_dir(fleet_dir) / "package.json").is_file()
        assert not prefix.exists(), "the temporary install dir was left behind"

    def test_the_composer_then_resolves_what_was_installed(
        self, fleet_dir: Path, monkeypatch
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        assert self._run(fleet_dir, monkeypatch, _FakeNpm()) == 0
        assert _compose(fleet_dir)["demo"]["command"] == "node"

    def test_nothing_is_installed_when_no_bot_is_armed(
        self, fleet_dir: Path, monkeypatch
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir, where="defaults", value="false")
        npm = _FakeNpm()
        assert self._run(fleet_dir, monkeypatch, npm) == 0
        assert self._installs(npm) == []
        assert not (fleet_dir / "state" / "mcp").exists()
        assert npm.argv_for("npx"), "positive control: the npx warm still ran"

    def test_a_present_copy_is_not_reinstalled(self, fleet_dir: Path, monkeypatch):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        _write_package(_package_dir(fleet_dir))
        npm = _FakeNpm()
        assert self._run(fleet_dir, monkeypatch, npm) == 0
        assert self._installs(npm) == []

    def test_a_copy_whose_entry_was_deleted_is_installed_again(self, fleet_dir: Path, monkeypatch):
        # The remedy start-bot and doctor name for a missing copy is one warm,
        # so a warm must restore a copy whose entry script is gone, not report
        # it unusable. Installs are atomic, so only damage leaves one.
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        entry = _write_package(_package_dir(fleet_dir))
        entry.unlink()
        npm = _FakeNpm()
        assert self._run(fleet_dir, monkeypatch, npm) == 0
        assert len(self._installs(npm)) == 1
        assert entry.is_file()
        scope = fleet_dir / "state" / "mcp" / "npm" / "@scope"
        assert [p.name for p in scope.iterdir() if p.name.startswith(".")] == [], (
            "the damaged copy or the temporary install was left behind")

    def test_a_copy_the_package_itself_cannot_launch_is_left_alone(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _write_package(_package_dir(fleet_dir), first_line="#!/bin/sh")
        npm = _FakeNpm()
        with caplog.at_level(logging.WARNING):
            assert self._run(fleet_dir, monkeypatch, npm) == 0
        assert self._installs(npm) == []
        assert "cannot launch directly" in caplog.text

    def test_a_failed_install_leaves_no_copy_and_fails_the_warm(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        summary = {}
        with caplog.at_level(logging.WARNING):
            assert self._run(fleet_dir, monkeypatch, _FakeNpm(fail=True), summary=summary) == 1
        assert SPEC in summary["failed"]
        assert not (fleet_dir / "state" / "mcp" / "npm" / SPEC).exists()
        leftovers = list((fleet_dir / "state" / "mcp" / "npm" / "@scope").iterdir())
        assert leftovers == [], f"a torn install was left on disk: {leftovers}"
        assert SPEC in caplog.text and "ETIMEDOUT" in caplog.text
        assert _compose(fleet_dir)["demo"]["command"] == "npx"

    def test_a_dry_run_names_the_install_and_runs_nothing(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        npm = _FakeNpm()
        with caplog.at_level(logging.INFO):
            assert self._run(fleet_dir, monkeypatch, npm, dry_run=True) == 0
        assert self._installs(npm) == []
        assert "would install" in caplog.text and SPEC in caplog.text


# --- the switch ------------------------------------------------------------


class TestTheSwitchIsNamedWhereTheOperatorLooks:
    def test_it_is_registered_as_a_per_bot_opt_out(self):
        from claudlobby import switches as sw

        row = sw.by_key("mcp-direct-launch")
        assert row.polarity == sw.OPT_OUT
        assert row.carrier == sw.COMPOSE_BOT
        assert row.config == "mcp_direct_launch"
        assert not row.why_opt_in

    def test_its_lines_say_restart_and_that_the_plan_installs_not_next_tool_call(self):
        from claudlobby import switches as sw

        row = sw.by_key("mcp-direct-launch")
        assert row.arm.startswith("on by default")
        assert "config plan installs the copies" in row.arm and "host activate" in row.arm
        assert "restart" in row.arm and "restart" in row.disarm
        assert "mcp_direct_launch: false" in row.disarm
        assert "next tool call" not in row.arm and "next tool call" not in row.disarm

    def test_the_isolation_switch_retains_its_next_tool_call_contract(self):
        # The shared carrier can name activation for both switches while the
        # isolation deny still binds at a different time from .mcp.json.
        from claudlobby import switches as sw

        row = sw.by_key("shared-config-isolation")
        assert "config plan" in row.arm and "host activate" in row.arm
        assert "next tool call" in row.arm and "next tool call" in row.disarm
        assert "at the bot's next restart" not in row.arm

    def test_resolve_names_the_bots_that_have_it_on(self, fleet_dir: Path):
        from claudlobby import switches as sw

        _arm(fleet_dir, where="worker-1", value="false")
        fleet = load_test_fleet(fleet_dir)
        rows = {
            r.switch.key: r
            for r in sw.resolve(make_paths(fleet_dir), fleet, cascade={})
        }
        row = rows["mcp-direct-launch"]
        assert row.on is True
        assert "1 of 2 bot(s): lead" in row.source


# --- doctor names what config staging would compose -------------------------

class TestDoctorNamesTheFallbacks:
    """The rung reads the composer's own plan, so doctor and config staging cannot
    disagree about which servers still carry a wrapper."""

    def _rung(self, fleet_dir: Path):
        from claudlobby.doctor import DoctorReport, check_mcp_launch

        report = DoctorReport()
        check_mcp_launch(load_test_fleet(fleet_dir), make_paths(fleet_dir), report)
        return [c for c in report.checks if c.name == "mcp-launch"]

    def test_an_armed_bot_with_a_missing_copy_warns_with_the_fix(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        [check] = self._rung(fleet_dir)
        assert check.status == "warn"
        assert "lead/demo" in check.detail and "not installed" in check.detail
        assert "host cache warm" in check.detail and "config plan" in check.detail

    def test_an_armed_bot_whose_servers_all_launch_directly_passes(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        _write_package(_package_dir(fleet_dir))
        [check] = self._rung(fleet_dir)
        assert check.status == "pass"
        assert "1 armed bot(s)" in check.detail

    def test_no_armed_bot_adds_no_line(self, fleet_dir: Path):
        # A fleet whose bots all opt out: the switches rung already names that
        # with its lines, and a second line would say the same thing.
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir, where="defaults", value="false")
        assert self._rung(fleet_dir) == []

    def test_it_is_part_of_the_doctor_run(self):
        import inspect

        from claudlobby import doctor

        assert "check_mcp_launch(fleet, paths, report)" in inspect.getsource(doctor.run_doctor)


# --- doctor reads the file the bot will launch (ravi, #1991 review) ---------

class TestDoctorReadsTheComposedFile:
    """The plan is what `generate` WOULD compose now; a bot launches what its
    `.mcp.json` says. A copy removed after compose leaves a composed `node`
    entry whose script is gone: that server will not start, and the plan
    alone reads it as a harmless npx fallback."""

    def _compose(self, fleet_dir: Path, bot: str = "lead") -> Path:
        from claudlobby.composer import compose_bot

        fleet = load_test_fleet(fleet_dir)
        return compose_bot(fleet.bots[bot], fleet, make_paths(fleet_dir), log=lambda m: None)

    def _checks(self, fleet_dir: Path) -> dict:
        from claudlobby.doctor import DoctorReport, check_mcp_launch

        report = DoctorReport()
        check_mcp_launch(load_test_fleet(fleet_dir), make_paths(fleet_dir), report)
        return {c.name: c for c in report.checks}

    def test_a_copy_removed_after_generate_fails_naming_bot_server_and_path(self, fleet_dir: Path):
        import shutil

        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        entry = _write_package(_package_dir(fleet_dir))
        bot_dir = self._compose(fleet_dir)
        composed = json.loads((bot_dir / ".mcp.json").read_text())["mcpServers"]["demo"]
        assert composed["args"][0] == str(entry), "precondition: the file launches the copy"
        shutil.rmtree(fleet_dir / "state" / "mcp" / "npm" / SPEC)
        checks = self._checks(fleet_dir)
        dead = checks["mcp-launch-composed"]
        assert dead.status == "fail"
        assert "lead/demo" in dead.detail and str(entry) in dead.detail
        assert "will not start" in dead.detail and "host cache warm" in dead.detail
        from claudlobby import mcp_direct

        assert mcp_direct.remedy(load_test_fleet(fleet_dir).name) in dead.detail
        # ...and the plan rung keeps its own reading: what generate would compose now.
        assert checks["mcp-launch"].status == "warn"

    def test_a_composed_copy_that_exists_passes(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        _write_package(_package_dir(fleet_dir))
        self._compose(fleet_dir)
        checks = self._checks(fleet_dir)
        assert checks["mcp-launch-composed"].status == "pass"
        assert checks["mcp-launch"].status == "pass"

    def test_a_disarmed_bot_whose_file_still_launches_a_copy_is_checked(self, fleet_dir: Path):
        # Disarmed in the manifest but not regenerated: the file still names the
        # copy, and the file is what the bot runs.
        import shutil

        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir)
        _write_package(_package_dir(fleet_dir))
        self._compose(fleet_dir)
        fy = fleet_dir / "fleet.yaml"
        fy.write_text(fy.read_text().replace("      mcp_direct_launch: true\n",
                                             "      mcp_direct_launch: false\n"))
        shutil.rmtree(fleet_dir / "state" / "mcp" / "npm" / SPEC)
        checks = self._checks(fleet_dir)
        assert checks["mcp-launch-composed"].status == "fail"
        assert "mcp-launch" not in checks, "no armed bot: the plan rung stays silent"

    def test_an_npx_only_fleet_adds_no_composed_line(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        self._compose(fleet_dir)
        assert "mcp-launch-composed" not in self._checks(fleet_dir)

    def test_a_node_entry_outside_state_mcp_is_not_judged(self, fleet_dir: Path, monkeypatch):
        # The older global-binary swap also composes `node <path>`, pointing at
        # a PATH binary the fleet does not own. That is not a copy this check
        # vouches for, so a missing one is not reported as a dead copy.
        import shutil as _shutil

        _equip_with_global_binary(fleet_dir)
        monkeypatch.setattr(_shutil, "which", lambda _n: "/nonexistent/bin/demo-mcp")
        bot_dir = self._compose(fleet_dir)
        composed = json.loads((bot_dir / ".mcp.json").read_text())["mcpServers"]["demo"]
        assert composed["command"] == "node", "precondition: the global-binary swap ran"
        assert "mcp-launch-composed" not in self._checks(fleet_dir)


# --- option A: the direct launch is the default, and staging installs it ------

class TestItIsOnUnlessABotOptsOut:
    """#1604, option A. The npm wrapper is the cost every bot pays by default,
    so the default is the direct launch; a bot or a fleet's defaults can still
    opt out, and anything that cannot launch directly keeps npx as before."""

    def test_a_bot_that_sets_nothing_launches_the_installed_copy(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        entry = _write_package(_package_dir(fleet_dir))
        server = _compose(fleet_dir)["demo"]
        assert (server["command"], server["args"]) == ("node", [str(entry), "--flag"])

    def test_a_bot_that_opts_out_keeps_npx(self, fleet_dir: Path):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _write_package(_package_dir(fleet_dir))
        _arm(fleet_dir, where="lead", value="false")
        assert _compose(fleet_dir)["demo"]["command"] == "npx"

    def test_defaults_can_opt_a_fleet_out_and_a_bot_back_in(self, fleet_dir: Path):
        _arm(fleet_dir, where="defaults", value="false")
        _arm(fleet_dir, where="worker-1", value="true")
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].mcp_direct_launch is False
        assert fleet.bots["worker-1"].mcp_direct_launch is True

    def test_the_switch_is_an_opt_out(self):
        from claudlobby import switches as sw

        row = sw.by_key("mcp-direct-launch")
        assert row.polarity == sw.OPT_OUT and row.default_on
        assert not row.why_opt_in
        assert "mcp_direct_launch: false" in row.disarm


class TestPlanningInstallsTheCopiesFirst:
    """Installs belong in staging (#1604, A): `config plan` installs each copy
    an armed bot launches BEFORE it composes, so the first plan on a fresh host
    composes the direct launch instead of the npx fallback, with no separate
    warm. A failed install never stops the plan: that server keeps npx."""

    def _plan(self, fleet_dir: Path, monkeypatch, npm: "_FakeNpm") -> dict:
        import types

        import claudlobby.config_staging as staging
        import claudlobby.context as context
        from claudlobby.commands import releases

        class _Staged(Exception):
            pass

        seen: dict = {}

        def stage(fleet_paths, release, *, log=lambda message: None):
            # What composition will find when it runs.
            seen["copy_installed"] = (_package_dir(fleet_dir) / "package.json").is_file()
            raise _Staged

        monkeypatch.setattr(releases, "_release",
                            lambda root, release_id: types.SimpleNamespace(cli_path=Path("/x")))
        monkeypatch.setattr(context, "declared_paths",
                            lambda root, package, *, external=(): [make_paths(fleet_dir)])
        monkeypatch.setattr(staging, "stage_configuration", stage)
        monkeypatch.setattr(subprocess, "run", npm)
        with pytest.raises(_Staged):
            releases._config_plan(types.SimpleNamespace(release="r-" + "0" * 64, fleet_path=()),
                                  fleet_dir)
        return seen

    def _installs(self, npm: "_FakeNpm") -> list[list[str]]:
        return [c for c in npm.argv_for("npm") if c[1:2] == ["install"]]

    def test_the_copy_is_installed_before_composition(self, fleet_dir: Path, monkeypatch):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        npm = _FakeNpm()
        assert self._plan(fleet_dir, monkeypatch, npm) == {"copy_installed": True}
        [call] = self._installs(npm)
        assert call[-1] == SPEC

    def test_a_failed_install_still_plans_and_says_which(self, fleet_dir: Path, monkeypatch, capsys):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        npm = _FakeNpm(fail=True)
        assert self._plan(fleet_dir, monkeypatch, npm) == {"copy_installed": False}
        err = capsys.readouterr().err
        assert SPEC in err and "failed" in err
        assert "ETIMEDOUT" not in err, "npm's own text stays out of the plan's output"

    def test_a_bot_that_opts_out_installs_nothing(self, fleet_dir: Path, monkeypatch):
        equip_bot_with_mcp(fleet_dir, {"demo": NPX})
        _arm(fleet_dir, where="defaults", value="false")
        npm = _FakeNpm()
        assert self._plan(fleet_dir, monkeypatch, npm) == {"copy_installed": False}
        assert self._installs(npm) == []


# --- one predicate for a composed copy that is gone ---------------------------

START_BOT = Path(__file__).resolve().parents[1] / "claudlobby" / "_runtime_scripts" / "start-bot.sh"
LIB_COMMON = START_BOT.with_name("lib-common.sh")


def _composed(entry: Path) -> dict:
    return {"mcpServers": {
        "demo": {"command": "node", "args": [str(entry), "--flag"]},
        "global": {"command": "node", "args": ["/usr/local/bin/demo-mcp"]},
        "remote": {"command": "npx", "args": ["-y", SPEC]},
    }}


class TestTheOnePredicateForAMissingCopy:
    """`mcp_direct.missing_copies` is what doctor and start-bot.sh both read,
    so the two cannot disagree about which composed server will not start."""

    def test_a_composed_copy_is_judged_by_its_path_segments(self, tmp_path: Path):
        from claudlobby import mcp_direct

        entry = (tmp_path / "another-spelling" / "state" / "mcp" / "npm" / SPEC
                 / "node_modules" / BARE / "dist" / "index.js")
        assert mcp_direct.composed_copies(_composed(entry)) == [("demo", entry)]

    def test_only_a_copy_whose_entry_is_gone_is_missing(self, fleet_dir: Path):
        from claudlobby import mcp_direct

        entry = _write_package(_package_dir(fleet_dir))
        assert mcp_direct.missing_copies(_composed(entry)) == []
        entry.unlink()
        assert mcp_direct.missing_copies(_composed(entry)) == [("demo", entry)]

    def _notice(self, tmp_path: Path, mcp_json: Path) -> subprocess.CompletedProcess:
        import sys

        return subprocess.run(
            [sys.executable, "-I", "-B", "-m", "claudlobby.mcp_direct", "notice",
             str(mcp_json), "lead", "demo-fleet"],
            capture_output=True, text=True, timeout=60)

    def test_the_notice_names_the_bot_server_path_and_remedy(self, fleet_dir: Path, tmp_path: Path):
        entry = _package_dir(fleet_dir) / "dist" / "index.js"  # composed, never installed
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(json.dumps(_composed(entry)))
        r = self._notice(tmp_path, mcp_json)
        assert r.returncode == 0, r.stderr
        [line] = r.stdout.splitlines()
        assert line.startswith("lead: 1 MCP server(s) will not start")
        assert f"demo ({entry})" in line
        assert "--fleet demo-fleet host cache warm" in line

    def test_a_present_copy_or_an_unreadable_file_says_nothing(self, fleet_dir: Path, tmp_path: Path):
        entry = _write_package(_package_dir(fleet_dir))
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(json.dumps(_composed(entry)))
        assert self._notice(tmp_path, mcp_json).stdout == ""
        mcp_json.write_text("{ torn")
        r = self._notice(tmp_path, mcp_json)
        assert (r.returncode, r.stdout) == (2, "")


class TestStartBotNamesAMissingCopy:
    """The real block cut from start-bot.sh, under errexit and an ERR trap: it
    raises one notice for a missing copy, says nothing otherwise, and never
    stops the boot, whatever the predicate does."""

    def _run(self, tmp_path: Path, mcp: dict, python: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
        source = START_BOT.read_text()
        start = "# --- Direct-launch MCP copies (#1604)"
        end = "# --- end direct-launch MCP copies"
        assert source.count(start) == 1 and source.count(end) == 1
        block = start + source.split(start, 1)[1].split(end, 1)[0]
        bot = tmp_path / "bots" / "lead"
        (bot / "logs").mkdir(parents=True)
        (bot / ".mcp.json").write_text(json.dumps(mcp))
        notices, errors = tmp_path / "notices", tmp_path / "errors"
        script = (
            '. "$1"\n'
            "set -Eeuo pipefail\n"
            'trap \'printf "ERR\\n" >> "$4"\' ERR\n'
            'NOTICES="$3"\n'
            'emit_fleet_notice() { printf "%s\\n" "$@" >> "$NOTICES"; }\n'
            'BOT_DIR="$2"; FLEET_NAME=demo-fleet; _NATIVE_ADMISSION_PYTHON="$5"\n'
            + block)
        r = subprocess.run(["/bin/bash", "-c", script, "_", str(LIB_COMMON), str(bot),
                            str(notices), str(errors), python],
                           capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin"})
        return r, notices, errors

    def test_a_missing_copy_raises_one_notice_and_the_boot_goes_on(self, fleet_dir: Path, tmp_path: Path):
        import sys

        entry = _package_dir(fleet_dir) / "dist" / "index.js"
        r, notices, errors = self._run(tmp_path, _composed(entry), sys.executable)
        assert r.returncode == 0, r.stderr
        assert not errors.exists(), errors.read_text() if errors.exists() else ""
        bots_dir, event, message = notices.read_text().splitlines()
        assert (bots_dir, event) == (str(tmp_path / "bots"), "mcp_copy_missing")
        assert message.startswith("lead: 1 MCP server(s) will not start") and str(entry) in message
        assert "MCP lead: 1 MCP server(s)" in (tmp_path / "bots" / "lead" / "logs" / "startup.log").read_text()

    def test_a_present_copy_raises_nothing(self, fleet_dir: Path, tmp_path: Path):
        import sys

        entry = _write_package(_package_dir(fleet_dir))
        r, notices, errors = self._run(tmp_path, _composed(entry), sys.executable)
        assert r.returncode == 0, r.stderr
        assert not notices.exists() and not errors.exists()

    def test_a_predicate_that_fails_never_stops_the_boot(self, fleet_dir: Path, tmp_path: Path):
        failing = tmp_path / "python"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(0o755)
        entry = _package_dir(fleet_dir) / "dist" / "index.js"
        r, notices, errors = self._run(tmp_path, _composed(entry), str(failing))
        assert r.returncode == 0, r.stderr
        assert not notices.exists() and not errors.exists()
