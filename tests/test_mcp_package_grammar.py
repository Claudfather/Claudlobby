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

        (fleet_dir / "library" / "mcp" / "uvxdemo.json").write_text(
            json.dumps({"uvxdemo": {"command": "uvx", "args": ["workspace-mcp", "--tools", "gmail"]}})
        )
        fy = fleet_dir / "fleet.yaml"
        before = fy.read_text()
        after = before.replace(
            "    lead:\n      expertise: [orchestration]\n",
            "    lead:\n      expertise: [orchestration]\n      mcp: [uvxdemo]\n",
        )
        assert after != before, "fixture no longer equips the bot"
        fy.write_text(after)

        # Force the swap's precondition: a resolvable global binary.
        monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/node")
        fleet = load_test_fleet(fleet_dir)
        out = compose_mcp_json(fleet.bots["lead"], make_paths(fleet_dir))
        server = out["mcpServers"]["uvxdemo"]
        assert server["command"] == "uvx", "the npx->node swap reached a uvx server"
        assert server["args"] == ["workspace-mcp", "--tools", "gmail"]


class TestTheProbeSeesUvPackages:
    """#1577's actual defect: the gate could not see a uv package, so on a fleet
    whose npx packages were cached it passed and the warm never ran."""

    def _root(self, tmp_path: Path, servers: dict) -> Path:
        root = tmp_path / "clroot"
        (root / "library" / "mcp").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        for name, server in servers.items():
            (root / "library" / "mcp" / f"{name}.json").write_text(json.dumps({name: server}))
        # The probe execs the real grammar; copy it rather than stub it.
        (root / "lib" / "mcp-package-grammar.py").write_text(
            (REPO_ROOT / "lib" / "mcp-package-grammar.py").read_text()
        )
        return root

    def _run(self, root: Path, npx_cache: Path, uv_cache: Path, uv_tools: Path, bindir: Path):
        """`uv cache dir` / `uv tool dir` are shelled, so uv is stubbed on PATH:
        the probe must be testable on a host with no uv at all."""
        bindir.mkdir(parents=True, exist_ok=True)
        stub = bindir / "uv"
        stub.write_text(
            "#!/bin/bash\n"
            f'[ "$1 $2" = "cache dir" ] && echo "{uv_cache}" && exit 0\n'
            f'[ "$1 $2" = "tool dir" ] && echo "{uv_tools}" && exit 0\n'
            "exit 1\n"
        )
        stub.chmod(0o755)
        env = constructed_env(CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=npx_cache)
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        # The real check-npx-cache.sh, driven against the scoped root.
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
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        self._cache_npx(npx_cache, "demo-mcp")
        r = self._run(root, npx_cache, uv_cache, uv_tools, tmp_path / "bin")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "uvx:cold-pkg==1.0" in r.stdout
        assert "demo-mcp" not in r.stdout.split("MISSING")[1]

    def test_a_cached_uv_package_passes(self, tmp_path: Path):
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["warm-pkg", "--tools", "x"]}}
        )
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        self._cache_uv(uv_cache, "warm-pkg")
        r = self._run(root, npx_cache, uv_cache, uv_tools, tmp_path / "bin")
        assert r.returncode == 0, r.stdout + r.stderr

    def test_the_wheel_dir_version_is_globbed_not_pinned(self, tmp_path: Path):
        """uv version-stamps these directories (wheels-v1 and wheels-v6 coexist
        on a real host); a pinned name silently stops matching after upgrade."""
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["warm-pkg"]}}
        )
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        (uv_cache / "wheels-v99" / "pypi" / "warm-pkg").mkdir(parents=True)
        r = self._run(root, npx_cache, uv_cache, uv_tools, tmp_path / "bin")
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
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        (uv_tools / "tooled-pkg").mkdir()
        r = self._run(root, npx_cache, uv_cache, uv_tools, tmp_path / "bin")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "uv tool install" in r.stdout

    def test_the_pypi_name_is_normalized_before_lookup(self, tmp_path: Path):
        root = self._root(
            tmp_path, {"u": {"command": "uvx", "args": ["--from", "Warm_Pkg==1.0", "w"]}}
        )
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        self._cache_uv(uv_cache, "warm-pkg")
        r = self._run(root, npx_cache, uv_cache, uv_tools, tmp_path / "bin")
        assert r.returncode == 0, r.stdout + r.stderr

    def test_a_host_with_no_uv_at_all_reports_missing_rather_than_crashing(
        self, tmp_path: Path
    ):
        """No uv stub on PATH: the probe must still answer, and must not claim
        a uv package is fine."""
        root = self._root(tmp_path, {"u": {"command": "uvx", "args": ["some-pkg"]}})
        npx_cache, uv_cache, uv_tools = tmp_path / "npx", tmp_path / "uv", tmp_path / "tools"
        npx_cache.mkdir(); uv_cache.mkdir(); uv_tools.mkdir()
        bindir = tmp_path / "emptybin"; bindir.mkdir()
        env = constructed_env(CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=npx_cache)
        env["PATH"] = f"{bindir}:/usr/bin:/bin"
        r = subprocess.run(["bash", str(CHECKER)], capture_output=True, text=True, env=env)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "uvx:some-pkg" in r.stdout
