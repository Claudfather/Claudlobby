"""#1497: warm-cache covered npx and nothing else, so uvx servers lost cold.

`cmd_warm_cache` gated on `command == "npx"`, and every layer beneath that gate
was npx-shaped too: the extractor walked args for `-y`, the collection was a
`set[str]` that can hold a name but not a pinned spec plus a separate console
script, and the invocation was rebuilt from that name as `npx -y <pkg> --help`.

The measurement that motivates it (clog, idle host, no contention): a cold
`uvx workspace-mcp --help` took 33.0s against a 30s MCP connect budget, warm
3.4s and 2.9s. So a uvx server loses *deterministically* on a cold cache —
boot-storm contention is not needed, which is what refines the framing in #1497.

The three shipped uvx fragments have two arg shapes and neither is npx's:

    gws                     ["workspace-mcp", "--tools", "gmail", "calendar"]
    google-analytics        ["--from", "google-analytics-mcp==2.8.1", "ga4-mcp-server"]
    google-search-console   ["--from", "mcp-search-console==0.3.2", "mcp-search-console"]

The `--from` shape is the load-bearing one: the package to download and the
entry point to run are *different tokens*, so a warm rebuilt from the package
alone (`uvx --from <spec> --help`) prints uv's own help and fetches nothing —
a no-op that reports success.
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
    load_lib_module,
    warm_cache_args,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_MCP = REPO_ROOT / "library" / "mcp"

GWS = {"command": "uvx", "args": ["workspace-mcp", "--tools", "gmail", "calendar"]}
GA4 = {"command": "uvx", "args": ["--from", "google-analytics-mcp==2.8.1", "ga4-mcp-server"]}
NPXPKG = "demo-mcp@1.2.3"
NPX = {"command": "npx", "args": ["-y", NPXPKG]}


class TestUvxFragmentsAreWarmedAtAll:
    """The gate. `command == "npx"` meant uvx servers were never seen."""

    def test_a_bare_uvx_fragment_is_warmed(self, fleet_dir: Path, monkeypatch, caplog):
        """gws' shape: the package is the first arg, its own flags follow."""
        equip_bot_with_mcp(fleet_dir, {"gws": GWS})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) == 0
        assert rec.argv_for("uvx") == [["uvx", "workspace-mcp", "--help"]]

    def test_a_uvx_package_is_never_warmed_through_npx(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Asking npx for a Python package downloads nothing and warms nothing."""
        equip_bot_with_mcp(fleet_dir, {"gws": GWS})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert rec.argv_for("npx") == [], "a uvx package was handed to npx"


class TestTheFromShapeSurvivesTheRoundTrip:
    """The data-structure half. A `set[str]` cannot hold spec-plus-entrypoint."""

    def test_from_shape_reconstructs_both_the_spec_and_the_entry_point(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """`uvx --from <spec> --help` prints uv's help and fetches nothing, so
        dropping the entry point is a warm that silently does nothing."""
        equip_bot_with_mcp(fleet_dir, {"ga": GA4})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) == 0
        assert rec.argv_for("uvx") == [
            ["uvx", "--from", "google-analytics-mcp==2.8.1", "ga4-mcp-server", "--help"]
        ]

    def test_the_pinned_version_is_not_stripped(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Warming an unpinned package populates a cache entry the server will
        not use — the fragment pins the version it actually runs."""
        equip_bot_with_mcp(fleet_dir, {"ga": GA4})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert "google-analytics-mcp==2.8.1" in rec.argv_for("uvx")[0]


class TestTheNpxPathIsUnchanged:
    """Guard. Every npx behaviour predates this and must survive it."""

    def test_npx_invocation_is_byte_identical(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) == 0
        assert rec.calls == [["npx", "-y", NPXPKG, "--help"]]

    def test_a_failed_npx_warm_still_exits_nonzero(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX})
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(returncode=1))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) != 0

    def test_dry_run_spawns_nothing_but_names_both_runtimes(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX, "gws": GWS})
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir, dry_run=True)) == 0
        assert rec.calls == []
        assert NPXPKG in caplog.text and "workspace-mcp" in caplog.text


class TestAMissingToolchainIsScopedToItsOwnEcosystem:
    """`return 1` on FileNotFoundError aborted the whole command, so one absent
    toolchain stopped the other ecosystem from warming at all."""

    def test_an_absent_toolchain_does_not_abort_the_other_ecosystem(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """The missing runtime must be the one that runs FIRST, or the assertion
        cannot see the abort at all.

        Runtimes are warmed in sorted order, so with npx present and uvx missing
        the npx work is already done by the time a `return 1` would fire — the
        obvious spelling of this test passes against the very defect it names.
        Measured: that mutant survived the first version of this test, so npx is
        the one made absent here.
        """
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX, "gws": GWS})
        rec = SubprocessRecorder(missing="npx")
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            rc = cmd_warm_cache(warm_cache_args(fleet_dir))
        assert rec.argv_for("uvx") == [["uvx", "workspace-mcp", "--help"]], (
            "an absent npx stopped uvx from warming"
        )
        assert rc != 0, "an unwarmed ecosystem must not read as success"

    def test_the_symmetric_case_holds_too(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Guard on the other direction. It cannot detect an abort (npx runs
        first and is already done), so it pins the symmetry and nothing more."""
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX, "gws": GWS})
        rec = SubprocessRecorder(missing="uvx")
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            rc = cmd_warm_cache(warm_cache_args(fleet_dir))
        assert rec.argv_for("npx") == [["npx", "-y", NPXPKG, "--help"]]
        assert rc != 0

    def test_missing_uvx_names_uv_not_node(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """The npx-shaped error told an operator to install Node.js for a
        missing Python toolchain."""
        equip_bot_with_mcp(fleet_dir, {"gws": GWS})
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(missing="uvx"))
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert "uv" in caplog.text
        assert "Node.js" not in caplog.text

    def test_missing_npx_still_names_node(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Guard: the npx hint was correct and stays."""
        equip_bot_with_mcp(fleet_dir, {"npxdemo": NPX})
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(missing="npx"))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) != 0
        assert "Node.js" in caplog.text

    def test_a_missing_runtime_is_probed_once_not_once_per_package(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Retrying an absent binary per package turns one diagnostic into N."""
        equip_bot_with_mcp(fleet_dir, {"gws": GWS, "ga": GA4})
        rec = SubprocessRecorder(missing="uvx")
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert len(rec.argv_for("uvx")) == 1


class TestAnUnreadableShapeIsReportedNotGuessed:
    """A uvx arg vector this cannot parse must say so. Guessing produces a warm
    that succeeds against the wrong token and reports the server as covered —
    the false green #1497 is about, recreated inside its own fix."""

    def test_a_leading_flag_is_not_mistaken_for_the_package(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """`uvx --python 3.12 foo` has no `--from`; taking args[0] or the first
        non-flag token yields `--python` or `3.12`, neither a package."""
        equip_bot_with_mcp(
            fleet_dir,
            {"odd": {"command": "uvx", "args": ["--python", "3.12", "foo-mcp"]}},
        )
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert rec.calls == [], "an unreadable shape was warmed against a guess"
        assert "odd" in caplog.text, "an unwarmed server was dropped silently"

    def test_from_without_an_entry_point_is_not_warmed(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """`uvx --from <spec> --help` is uv's own help — it fetches nothing, so
        emitting it would report a warm that never happened."""
        equip_bot_with_mcp(
            fleet_dir, {"odd": {"command": "uvx", "args": ["--from", "some-pkg==1.0"]}}
        )
        rec = SubprocessRecorder()
        monkeypatch.setattr(subprocess, "run", rec)
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert rec.calls == []
        assert "odd" in caplog.text


class TestTheShippedFragmentsAreAllReadable:
    """Reads the real library, not a fixture. A fixture is written by the same
    person at the same sitting and cannot surprise you about the real shape."""

    def test_every_shipped_uvx_fragment_yields_a_warm_command(self):
        # `_warm_prefix` moved out of core.py into the shared grammar when
        # #1577 consolidated it (bash needs to exec it too). Same function,
        # new home — the call below is unchanged.
        _warm_prefix = load_lib_module("mcp-package-grammar").warm_prefix

        seen = 0
        for frag in sorted(SHIPPED_MCP.glob("*.json")):
            for name, server in json.loads(frag.read_text()).items():
                if name.startswith("_") or not isinstance(server, dict):
                    continue
                if server.get("command") != "uvx":
                    continue
                seen += 1
                target = _warm_prefix("uvx", server.get("args", []))
                assert target is not None, f"{frag.name}:{name} — unreadable arg shape"
        # A negative that asserts nothing is not evidence: if the sweep found no
        # uvx fragments, the loop above passed without testing anything.
        assert seen >= 3, f"expected at least the 3 shipped uvx fragments, found {seen}"


@pytest.fixture(autouse=True)
def _equip_grammar(fleet_dir):
    """This module drives composition/warm-cache, which load the shared
    grammar through `mcp_grammar` -- and that door REFUSES rather than
    falling back, so the real file has to be under the fixture's lib/."""
    from tests.conftest import equip_grammar

    equip_grammar(fleet_dir)
