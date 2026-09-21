"""The MCP package check (#1058) — an instrument, never a remedy.

A fragment composing `npx -y <pkg>` where `<pkg>` is not in the registry gives
a fleet a DEAD server that reports nothing, because an erroring MCP fires
`PostToolUseFailure` while the vitals hook listens on `PostToolUse`.

**These tests deliberately never assert WHICH shipped fragments are unpinned.**
That assertion would fail the moment an operator applies the remedy, turning an
instrument into a blocker on the decision it exists to inform. What is pinned
here is the CLASSIFIER's behaviour on synthetic specs, plus the structural fact
that every shipped fragment is readable by the grammar — both stable under any
remedy anyone chooses.

The network path is exercised for real in the PR body's live run, not here:
these tests make no network call.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from claudlobby import mcp_packages as mp  # noqa: E402


def _grammar():
    spec = importlib.util.spec_from_file_location(
        "mcp_package_grammar", REPO_ROOT / "lib" / "mcp-package-grammar.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


g = _grammar()


class TestIsPinned:
    @pytest.mark.parametrize(
        "runtime,spec,pinned",
        [
            ("npx", "docker-mcp@1.0.0", True),
            ("npx", "@notionhq/notion-mcp-server@2.2.1", True),
            ("npx", "mcp-remote@0.1.38", True),
            ("npx", "pkg@latest", True),
            ("npx", "pkg@^2.0.0", True),
            ("npx", "printify-mcp", False),
            # The scoped name with NO version: the `@` that is part of the NAME
            # must never read as a version. This is the shape that broke a
            # hand-rolled probe -- it stripped to the empty string, the registry
            # root answered 200, and a dead fragment read healthy.
            ("npx", "@modelcontextprotocol/server-spotify", False),
            ("uvx", "google-analytics-mcp==2.8.1", True),
            ("uvx", "mcp-search-console==0.3.2", True),
            ("uvx", "pkg>=2", True),
            ("uvx", "workspace-mcp", False),
            # An extra is not a pin.
            ("uvx", "pkg[extra]", False),
        ],
    )
    def test_classification(self, runtime, spec, pinned):
        assert g.is_pinned(runtime, spec) is pinned

    def test_pypi_normalization_does_not_read_as_a_version(self):
        """The trap in the obvious shortcut.

        `bare_name(cmd, spec) != spec` looks like a fine pinning test and is
        wrong for PyPI: bare_name also applies PEP 503 normalization, so an
        UNPINNED name with underscores or capitals differs from its bare form.
        The npm arm passes that shortcut and the uvx arm does not, which is how
        a half-right predicate ships.
        """
        spec = "Google_Analytics_MCP"
        assert g.bare_name("uvx", spec) != spec  # the shortcut would say "pinned"
        assert g.is_pinned("uvx", spec) is False  # the real answer


class TestWarmArgv:
    def test_it_is_the_exact_command_the_fragment_would_run(self):
        argv = g.warm_argv("npx", ["-y", "printify-mcp"])
        assert argv == ["npx", "-y", "printify-mcp", "--help"]

    def test_server_flags_and_placeholders_are_not_carried_into_the_probe(self):
        """A fragment's own args can hold unexpanded ${VAR}; the probe is the
        package identity only, so they never reach a subprocess."""
        argv = g.warm_argv(
            "npx", ["-y", "mcp-remote@0.1.38", "https://x/${TOKEN}", "--port", "${P}"]
        )
        assert argv == ["npx", "-y", "mcp-remote@0.1.38", "--help"]

    def test_unreadable_shape_returns_none_rather_than_guessing(self):
        assert g.warm_argv("npx", []) is None


class TestProbeVerdicts:
    """Only an unambiguous registry not-found may condemn a package."""

    def _fake_run(self, monkeypatch, *, rc, out="", err=""):
        def fake(argv, **kw):
            return subprocess.CompletedProcess(argv, rc, out, err)

        monkeypatch.setattr(mp.subprocess, "run", fake)

    def test_clean_exit_is_a_pass_and_yields_no_finding(self, monkeypatch):
        self._fake_run(monkeypatch, rc=0, out="Usage: ...")
        assert mp._probe(["npx", "-y", "real-pkg", "--help"]) == ("", "")

    def test_e404_is_missing(self, monkeypatch):
        self._fake_run(monkeypatch, rc=1, err="npm error code E404\nnot in this registry")
        kind, _detail = mp._probe(["npx", "-y", "ghost-pkg", "--help"])
        assert kind == mp.MISSING

    def test_nonzero_without_a_not_found_signature_is_UNCHECKED_not_missing(
        self, monkeypatch
    ):
        """The false positive this rule exists to prevent.

        A real package whose --help is unsupported exits nonzero. Reading that
        as "missing" sends an operator to replace a fragment that was fine.
        """
        self._fake_run(monkeypatch, rc=1, err="Error: unknown option '--help'")
        kind, detail = mp._probe(["npx", "-y", "real-pkg", "--help"])
        assert kind == mp.UNCHECKED
        assert "not evidence it is missing" in detail

    def test_timeout_is_UNCHECKED(self, monkeypatch):
        """Measured for real: `uvx workspace-mcp --help` exceeded 120s on a
        live host and the package exists. A timeout is not a verdict."""

        def fake(argv, **kw):
            raise subprocess.TimeoutExpired(argv, mp.PROBE_TIMEOUT_S)

        monkeypatch.setattr(mp.subprocess, "run", fake)
        kind, detail = mp._probe(["npx", "-y", "slow-pkg", "--help"])
        assert kind == mp.UNCHECKED
        assert "did not answer" in detail

    def test_absent_toolchain_is_UNCHECKED(self, monkeypatch):
        def fake(argv, **kw):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(mp.subprocess, "run", fake)
        kind, detail = mp._probe(["npx", "-y", "pkg", "--help"])
        assert kind == mp.UNCHECKED
        assert "not installed" in detail

    def test_a_runtime_with_no_established_signature_is_UNCHECKED_without_running(
        self, monkeypatch
    ):
        """uvx has no observed not-found signature, so it is never condemned --
        and the probe does not even run, which is what keeps a slow uv fetch
        out of a compose."""

        def explode(argv, **kw):  # pragma: no cover — must not be reached
            raise AssertionError("the probe ran for a runtime with no signature")

        monkeypatch.setattr(mp.subprocess, "run", explode)
        kind, detail = mp._probe(["uvx", "workspace-mcp", "--help"])
        assert kind == mp.UNCHECKED
        assert "uvx" in detail


class TestFindingMessages:
    def test_unpinned_says_unverified_not_broken(self):
        msg = mp.Finding(mp.UNPINNED, "gws.json", "gws", "uvx", "workspace-mcp").message()
        assert "NO VERSION" in msg
        # Measured: one of the shipped unpinned declarations resolves fine, so
        # the wording must not assert breakage.
        assert "does not exist" not in msg and "DEAD" not in msg

    def test_missing_says_dead_and_silent(self):
        msg = mp.Finding(
            mp.MISSING, "printify.json", "printify", "npx", "printify-mcp", "npx said so"
        ).message()
        assert "DEAD" in msg and "silent" in msg

    def test_unchecked_never_reads_as_a_pass(self):
        msg = mp.Finding(
            mp.UNCHECKED, "gws.json", "gws", "uvx", "workspace-mcp", "no signature"
        ).message()
        assert "could NOT check" in msg


class TestAgainstTheShippedLibrary:
    """Structural only — never which fragments are unpinned (see module docstring)."""

    def test_every_shipped_fragment_is_readable_by_the_grammar(self):
        rows = g.declared_packages([str(REPO_ROOT / "library" / "mcp")])
        assert rows, "positive control: the shared library declares packages"
        for frag, server, runtime, spec, bare, pinned in rows:
            assert Path(frag).is_file()
            assert runtime in g.WARM_RUNTIMES
            assert spec and bare
            assert isinstance(pinned, bool)

    def test_probe_targets_still_narrows_the_same_walk(self):
        """`probe_targets` is parsed by check-npx-cache.sh; it must stay the
        deduped projection of the shared walk."""
        base = [str(REPO_ROOT / "library" / "mcp")]
        expected = sorted({(r[2], r[3], r[4]) for r in g.declared_packages(base)})
        assert g.probe_targets(base) == expected


class TestTheRungIsAWarningNeverAnError:
    def _fleet(self, tmp_path, fragments: dict[str, dict]):
        from claudlobby.config import BotConfig, FleetConfig, McpEntry

        mcp_dir = tmp_path / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        # The REAL shared grammar, not a stand-in: mcp_grammar refuses rather
        # than falling back, so a test root without lib/ exercises the refusal
        # path instead of the rung.
        (tmp_path / "lib").symlink_to(REPO_ROOT / "lib")
        for name, body in fragments.items():
            (mcp_dir / f"{name}.json").write_text(json.dumps(body))
        bot = BotConfig(
            bot_id="alpha",
            name="alpha",
            expertise=["software-engineering"],
            mcp=[McpEntry(name=n) for n in fragments],
        )
        return FleetConfig(
            name="probe", service_prefix="com.example.probe", bots={"alpha": bot}
        )

    def test_an_unpinned_declaration_warns_and_never_errors(self, tmp_path, monkeypatch):
        from claudlobby import validator
        from claudlobby.paths import Paths

        fleet = self._fleet(
            tmp_path,
            {
                "ghost": {"ghost": {"command": "npx", "args": ["-y", "ghost-pkg"]}},
                "solid": {"solid": {"command": "npx", "args": ["-y", "solid-pkg@1.0.0"]}},
            },
        )
        paths = Paths(root=tmp_path)
        report = validator.ValidationReport()
        # Disarmed: the offline signal must stand on its own, with the
        # resolution probe never invoked. Guarding `_probe` rather than
        # `subprocess.run` on purpose -- `mp.subprocess` IS the global module,
        # so patching it also catches the env cascade's own shell-out and the
        # test would fail for a reason unrelated to its subject.
        monkeypatch.setattr(
            mp, "_probe", lambda argv: pytest.fail(f"probed while disarmed: {argv}")
        )
        validator._validate_mcp_packages(fleet, paths, report)

        assert report.errors == [], "the rung must never block a compose"
        unpinned = [w for w in report.warnings if "NO VERSION" in w]
        assert len(unpinned) == 1 and "ghost.json" in unpinned[0]

    def test_armed_it_probes_and_a_missing_package_is_still_only_a_warning(
        self, tmp_path, monkeypatch
    ):
        from claudlobby import validator
        from claudlobby.paths import Paths

        fleet = self._fleet(
            tmp_path, {"ghost": {"ghost": {"command": "npx", "args": ["-y", "ghost-pkg"]}}}
        )
        (tmp_path / ".env").write_text(f"{mp.PROBE_FLAG}=1\n")
        paths = Paths(root=tmp_path)
        report = validator.ValidationReport()

        seen = []

        def fake_probe(argv):
            seen.append(argv)
            return mp.MISSING, "npx reported it is not in the registry"

        monkeypatch.setattr(mp, "_probe", fake_probe)
        validator._validate_mcp_packages(fleet, paths, report)

        assert seen == [["npx", "-y", "ghost-pkg", "--help"]], (
            "the probe must run the EXACT argv the fragment would run"
        )
        assert report.errors == [], "a dead package must NOT block a compose"
        assert any("DEAD server" in w for w in report.warnings)
