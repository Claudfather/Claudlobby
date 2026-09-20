"""#1577: one grammar for how an MCP server's args name its package.

The grammar was forked three ways — `warm-cache`, the composer's npx->node
binary swap, and `check-npx-cache.sh`'s embedded heredoc — and exactly one copy
knew `uvx` existed. The consequence was not a style problem: the probe that
GATES the warm could not see a uv package, so on a fleet whose npx packages
were cached the daily reload passed, called `debounce_clear`, and the uvx
servers stayed cold indefinitely. A correct warm behind a gate that never opens.

These tests pin the grammar, the uv probe's own derivation (uv does NOT lay its
cache out like npx, so the states are derived rather than transliterated), and
the boundary this PR must not cross: the binary swap stays npx-only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.conftest import constructed_env, load_lib_module

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "lib" / "check-npx-cache.sh"
SHIPPED_MCP = REPO_ROOT / "library" / "mcp"

g = load_lib_module("mcp-package-grammar")


def _equip(
    fleet_dir: Path, fragments: dict[str, dict], contracts: dict | None = None
) -> None:
    """Write fragments into the fleet library and equip the lead bot.

    `contracts` are the `_`-prefixed fragment-level keys that sit beside the
    server in a real fragment file (`_global_binary`, `_env_contract`).
    Composition BRANCHES on them, so a fixture that omits one silently takes
    the short-circuit path — and a test whose subject sits past that branch
    then passes without ever reaching the code it names.

    Asserts the edit landed: a silent no-op leaves the fleet with no MCP
    servers, so the command under test returns early and every assertion
    downstream reads the empty path instead of the code.
    """
    for name, server in fragments.items():
        (fleet_dir / "library" / "mcp" / f"{name}.json").write_text(
            json.dumps({name: server, **(contracts or {})})
        )
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    after = before.replace(
        "    lead:\n      expertise: [orchestration]\n",
        f"    lead:\n      expertise: [orchestration]\n      mcp: [{', '.join(fragments)}]\n",
    )
    assert after != before, "MINIMAL_FLEET_YAML indentation changed — fixture no longer equips"
    fy.write_text(after)


class TestTheGrammarReadsBothRuntimes:
    def test_npx_yields_the_package_after_dash_y(self):
        assert g.warm_prefix("npx", ["-y", "pkg@1.0", "--flag"]) == ("pkg@1.0", ["-y", "pkg@1.0"])

    def test_uvx_bare_takes_the_first_token(self):
        assert g.warm_prefix("uvx", ["workspace-mcp", "--tools", "gmail"]) == (
            "workspace-mcp",
            ["workspace-mcp"],
        )

    def test_uvx_from_keeps_the_spec_AND_the_entry_point(self):
        """`uvx --from <spec> --help` prints uv's own help and fetches nothing,
        so dropping the entry point is a warm that silently does nothing."""
        assert g.warm_prefix("uvx", ["--from", "ga-mcp==2.8.1", "ga4-mcp-server"]) == (
            "ga-mcp==2.8.1",
            ["--from", "ga-mcp==2.8.1", "ga4-mcp-server"],
        )

    def test_a_command_with_no_cache_is_not_warmable(self):
        assert g.warm_prefix("/bin/sh", ["-c", "exec foo"]) is None
        assert "/bin/sh" not in g.WARM_RUNTIMES

    def test_an_unreadable_uvx_shape_is_refused_not_guessed(self):
        """`--python 3.12` would yield `--python` or `3.12` under either
        obvious rule; warming the wrong token still exits 0."""
        assert g.warm_prefix("uvx", ["--python", "3.12", "foo"]) is None
        assert g.warm_prefix("uvx", ["--from", "some-pkg==1.0"]) is None


class TestOneParseServesBothConsumers:
    """The composer wants the args it must KEEP; warm-cache wants the package it
    DISCARDS. They are the same boundary, and the fork is what let them drift."""

    def test_split_returns_the_package_and_the_rest(self):
        pkg, rest = g.split_npx_args(["-y", "slack-mcp@1.2.3", "--transport", "stdio"])
        assert pkg == "slack-mcp@1.2.3"
        assert rest == ["--transport", "stdio"]

    def test_the_two_halves_are_disjoint_and_complete(self):
        args = ["-y", "pkg@1.0", "--a", "b"]
        pkg, rest = g.split_npx_args(args)
        assert [a for a in args if a != "-y" and a != pkg] == rest

    def test_warm_prefix_and_split_agree_on_which_token_is_the_package(self):
        args = ["-y", "mcp-remote@0.1.38", "https://example.invalid/mcp", "--port", "1"]
        assert g.warm_prefix("npx", args)[0] == g.split_npx_args(args)[0]


class TestTheProbeNameIsNotTheWarmName:
    """A cache is keyed by NAME; a warm should fetch exactly what the server
    runs. Collapsing the two would either warm an unpinned package or probe for
    a version-suffixed key that no cache holds."""

    def test_npm_version_suffix_is_stripped_for_the_probe_only(self):
        assert g.package_name("npx", ["-y", "docker-mcp@1.0.0"]) == "docker-mcp"
        assert g.warm_prefix("npx", ["-y", "docker-mcp@1.0.0"])[0] == "docker-mcp@1.0.0"

    def test_a_scoped_name_keeps_its_leading_at(self):
        """`@org/name` must not lose its scope to the version-stripping rule."""
        assert (
            g.package_name("npx", ["-y", "@modelcontextprotocol/server-github@2025.4.8"])
            == "@modelcontextprotocol/server-github"
        )

    def test_an_unversioned_package_is_left_alone(self):
        assert g.package_name("npx", ["-y", "printify-mcp"]) == "printify-mcp"

    def test_pypi_spec_is_stripped_and_pep503_normalized(self):
        """uv keys its wheel cache on the normalized name."""
        assert g.package_name("uvx", ["--from", "Google_Analytics_MCP==2.8.1", "ga4"]) == (
            "google-analytics-mcp"
        )
        assert g.package_name("uvx", ["workspace-mcp", "--tools", "gmail"]) == "workspace-mcp"


class TestTheShippedFragmentsAreAllReadable:
    """Reads the real library, not a fixture — a fixture is written by the same
    person at the same sitting and cannot surprise you about the real shape."""

    def test_every_shipped_warmable_fragment_yields_a_target(self):
        seen = {"npx": 0, "uvx": 0}
        for frag in sorted(SHIPPED_MCP.glob("*.json")):
            for name, server in g.servers_in(json.loads(frag.read_text())):
                rt = server.get("command")
                if rt not in g.WARM_RUNTIMES:
                    continue
                seen[rt] += 1
                assert g.warm_prefix(rt, server.get("args", [])) is not None, (
                    f"{frag.name}:{name} — unreadable arg shape"
                )
                assert g.package_name(rt, server.get("args", [])) is not None
        # A negative that asserts nothing is not evidence.
        assert seen["uvx"] >= 3 and seen["npx"] >= 10, seen


class TestTheBinarySwapStaysNpxOnly:
    """The boundary. Consolidating the shared GRAMMAR is this PR's job; making
    the npx->node swap uvx-aware is not, and would be wrong — the swap exists
    because a global `node` can stand in for `npx`, and uvx has no equivalent
    shape."""

    def test_a_uvx_server_is_never_rewritten_to_node(self, fleet_dir: Path, monkeypatch):
        import shutil as _shutil

        from claudlobby.composer import compose_mcp_json
        from tests.conftest import load_test_fleet, make_paths

        # The swap's precondition is TWO facts, not one: a fragment that
        # DECLARES a global binary, and that binary resolving on the host.
        # Without the declaration `resolved_binary` is None and compose
        # short-circuits before the `== "npx"` check — the comparison under
        # test here — so the monkeypatch below is inert and this test passes
        # whatever the gate says.
        _equip(
            fleet_dir,
            {"uvxdemo": {"command": "uvx", "args": ["workspace-mcp", "--tools", "gmail"]}},
            contracts={"_global_binary": "node"},
        )

        monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/node")
        fleet = load_test_fleet(fleet_dir)
        out = compose_mcp_json(fleet.bots["lead"], make_paths(fleet_dir))
        server = out["mcpServers"]["uvxdemo"]
        assert server["command"] == "uvx", "the npx->node swap reached a uvx server"
        assert server["args"] == ["workspace-mcp", "--tools", "gmail"]

    def test_an_npx_server_under_the_same_fixture_is_rewritten(
        self, fleet_dir: Path, monkeypatch
    ):
        """The control the boundary test cannot stand without.

        `command == "uvx"` is satisfied both by a swap that ran and held, and
        by a swap that never ran at all — so alone it cannot tell an intact
        boundary from a disarmed fixture. This drives the identical fixture
        with the one command the swap IS keyed on and requires the rewrite,
        so a fixture that stops arming the swap fails here instead of going
        quietly green next door.
        """
        import shutil as _shutil

        from claudlobby.composer import compose_mcp_json
        from tests.conftest import load_test_fleet, make_paths

        _equip(
            fleet_dir,
            {"npxdemo": {"command": "npx", "args": ["-y", "demo@1.0", "--flag"]}},
            contracts={"_global_binary": "node"},
        )

        monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/node")
        fleet = load_test_fleet(fleet_dir)
        out = compose_mcp_json(fleet.bots["lead"], make_paths(fleet_dir))
        server = out["mcpServers"]["npxdemo"]
        assert server["command"] == "node", "the swap did not fire — the fixture is disarmed"
        assert server["args"] == ["/usr/bin/node", "--flag"]


class TestTheProbeSeesUvPackages:
    """#1577's actual defect: the gate could not see a uv package, so on a fleet
    whose npx packages were cached it passed and the warm never ran."""

    def _root(self, tmp_path: Path, servers: dict) -> Path:
        """The probe resolves the grammar from its OWN dirname, never from
        $CLAUDLOBBY_ROOT, so this root carries fragments and nothing else."""
        root = tmp_path / "clroot"
        (root / "library" / "mcp").mkdir(parents=True)
        for name, server in servers.items():
            (root / "library" / "mcp" / f"{name}.json").write_text(json.dumps({name: server}))
        return root

    def _run(self, tmp_path: Path, root: Path, *, tools: tuple[str, ...] = ()):
        """Drive the real check-npx-cache.sh against a scoped cache.

        `uv cache dir` and `uv tool list` are shelled, so uv is stubbed: the
        probe must be testable on a host with no uv at all, and the stub
        answers the same two questions the real door does.
        """
        npx_cache = tmp_path / "npx"
        uv_cache = tmp_path / "uv"
        bindir = tmp_path / "bin"
        for d in (npx_cache, uv_cache, bindir):
            d.mkdir(exist_ok=True)
        stub = bindir / "uv"
        stub.write_text(
            "#!/bin/bash\n"
            f'[ "$1 $2" = "cache dir" ] && echo "{uv_cache}" && exit 0\n'
            f'[ "$1 $2" = "tool list" ] && printf "%s" "{chr(10).join(tools)}" && exit 0\n'
            "exit 1\n"
        )
        stub.chmod(0o755)
        env = constructed_env(CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=npx_cache)
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        return subprocess.run(
            ["bash", str(CHECKER)], capture_output=True, text=True, env=env
        )

    @staticmethod
    def _cache_npx(npx_cache: Path, bare: str):
        d = npx_cache / "abc123" / "node_modules" / bare
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}")

    @staticmethod
    def _cache_uv(uv_cache: Path, bare: str):
        (uv_cache / "wheels-v6" / "pypi" / bare).mkdir(parents=True)

    def test_a_cold_uv_package_fails_the_gate_while_every_npx_one_is_cached(
        self, tmp_path: Path
    ):
        """THE regression. Pre-fix this exited 0 — all npx cached, uvx unseen —
        so reload-fleet cleared the debounce and never warmed."""
        root = self._root(
            tmp_path,
            {
                "n": {"command": "npx", "args": ["-y", "demo-mcp@1.0.0"]},
                "u": {"command": "uvx", "args": ["--from", "cold-pkg==1.0", "cold"]},
            },
        )
        self._cache_npx(tmp_path / "npx", "demo-mcp")
        r = self._run(tmp_path, root)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "uvx:cold-pkg==1.0" in r.stdout
        assert "demo-mcp" not in r.stdout.split("MISSING")[1]

    def test_a_cached_uv_package_passes(self, tmp_path: Path):
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["warm-pkg", "--tools", "x"]}}
        )
        self._cache_uv(tmp_path / "uv", "warm-pkg")
        r = self._run(tmp_path, root)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_the_wheel_dir_version_is_globbed_not_pinned(self, tmp_path: Path):
        """uv version-stamps these directories (wheels-v1 and wheels-v6 coexist
        on a real host); a pinned name silently stops matching after upgrade."""
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["warm-pkg"]}}
        )
        (tmp_path / "uv" / "wheels-v99" / "pypi" / "warm-pkg").mkdir(parents=True)
        r = self._run(tmp_path, root)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_uv_tool_install_is_resolvable_and_named(self, tmp_path: Path):
        """Measured: `uv tool install` also populates the wheel cache, so uv has
        no permanently-unsatisfiable state of npm's kind. The tool dir is still
        checked, because a tool installed from a path or VCS — or one whose
        cache entry was pruned — would otherwise read MISSING forever while
        `uvx` runs it happily. That is #852's shape by another route."""
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["tooled-pkg"]}}
        )
        r = self._run(tmp_path, root, tools=("tooled-pkg",))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "uv tool install" in r.stdout

    def test_the_pypi_name_is_normalized_before_lookup(self, tmp_path: Path):
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["--from", "Warm_Pkg==1.0", "w"]}}
        )
        self._cache_uv(tmp_path / "uv", "warm-pkg")
        r = self._run(tmp_path, root)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_host_with_no_uv_at_all_reports_missing_rather_than_crashing(
        self, tmp_path: Path
    ):
        """No uv stub on PATH: the probe must still answer, and must not claim
        a uv package is fine."""
        root = self._root(tmp_path, {"u": {"command": "uvx", "args": ["some-pkg"]}})
        npx_cache = tmp_path / "npx"; npx_cache.mkdir()
        bindir = tmp_path / "emptybin"; bindir.mkdir()
        env = constructed_env(CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=npx_cache)
        env["PATH"] = f"{bindir}:/usr/bin:/bin"
        r = subprocess.run(["bash", str(CHECKER)], capture_output=True, text=True, env=env)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "uvx:some-pkg" in r.stdout


class TestTheRefusalCanActuallyFire:
    """`mcp_grammar` refuses rather than falling back, and `fleet_dir` now
    always carries the grammar — so without this the refusal, which is the
    whole reason that module exists, had no test that could reach it."""

    def test_a_missing_grammar_raises_rather_than_falling_back(self, fleet_dir: Path):
        from claudlobby.mcp_grammar import GrammarUnavailable, grammar
        from tests.conftest import make_paths

        (fleet_dir / "lib" / "mcp-package-grammar.py").unlink()
        paths = make_paths(fleet_dir)
        try:
            grammar(paths)
        except GrammarUnavailable as e:
            assert "mcp-package-grammar.py" in str(e)
        else:
            raise AssertionError("a missing grammar was silently tolerated")

    def test_warm_cache_refuses_rather_than_warming_the_wrong_thing(
        self, fleet_dir: Path, caplog
    ):
        """A fallback grammar would be consulted exactly when the two copies
        had diverged, so warm-cache exits nonzero instead of guessing."""
        import logging

        import argparse

        from claudlobby.commands.core import cmd_warm_cache

        _equip(fleet_dir, {"n": {"command": "npx", "args": ["-y", "demo@1.0"]}})
        (fleet_dir / "lib" / "mcp-package-grammar.py").unlink()
        args = argparse.Namespace(root=str(fleet_dir), fleet=None, seed=False, dry_run=False)
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(args) != 0
        assert "cache warm complete" not in caplog.text
