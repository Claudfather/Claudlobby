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

One guard is the exception, and it points the other way: no shipped fragment
may launch an UNPINNED npx package (#1890). Pinning satisfies it, so it never
blocks the remedy; it blocks the regression, a new fragment that runs whatever
the registry serves at boot.

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
            # --- npm: EXACT versions pin ---------------------------------
            ("npx", "docker-mcp@1.0.0", True),
            ("npx", "@notionhq/notion-mcp-server@2.2.1", True),
            ("npx", "mcp-remote@0.1.38", True),
            ("npx", "pkg@1.0.0-beta.1", True),          # prerelease
            ("npx", "pkg@1.0.0+build.5", True),         # build metadata
            # --- npm: everything that RESOLVES AT BOOT does not ----------
            # An earlier cut read all of these as pinned, because it reused
            # the suffix pattern `bare_name` strips with. `@latest` is npm's
            # own idiom for "give me current" and is behaviourally identical
            # to the bare name two rows below.
            ("npx", "pkg@latest", False),
            ("npx", "pkg@next", False),
            ("npx", "pkg@^2.0.0", False),
            ("npx", "pkg@~1.2.3", False),
            ("npx", "pkg@>=2", False),
            ("npx", "pkg@*", False),
            # A PARTIAL version is a range, not a pin. Measured rather than
            # reasoned: `npm view cowsay@1 version` answers 1.6.0, not 1.0.0.
            ("npx", "pkg@1.2", False),
            ("npx", "pkg@1", False),
            ("npx", "printify-mcp", False),
            # The scoped name with NO version: the `@` that is part of the NAME
            # must never read as a version. This is the shape that broke a
            # hand-rolled probe -- it stripped to the empty string, the registry
            # root answered 200, and a dead fragment read healthy.
            ("npx", "@modelcontextprotocol/server-spotify", False),
            # --- PyPI: only == and === pin -------------------------------
            ("uvx", "google-analytics-mcp==2.8.1", True),
            ("uvx", "mcp-search-console==0.3.2", True),
            ("uvx", "pkg===1.0", True),
            ("uvx", "pkg[extra]==1.0", True),           # an extra beside a pin
            ("uvx", "pkg>=2", False),
            ("uvx", "pkg<=3", False),
            ("uvx", "pkg~=1.0", False),
            # `!=` is an EXCLUSION -- it names the one version NOT to run, so
            # it pins least of all, and the operator list read it as a pin.
            ("uvx", "pkg!=1.0", False),
            # PEP 440 prefix matching: `==1.2.*` is any 1.2.x, not a version.
            ("uvx", "pkg==1.2.*", False),
            ("uvx", "workspace-mcp", False),
            # An extra is not a pin.
            ("uvx", "pkg[extra]", False),
        ],
    )
    def test_classification(self, runtime, spec, pinned):
        assert g.is_pinned(runtime, spec) is pinned

    def test_a_floating_tag_classifies_exactly_like_a_bare_name(self):
        """The property behind the parametrize block, stated once.

        `pkg@latest` and `pkg` resolve to the SAME thing at boot, so they must
        classify the same. An earlier cut had them disagree, which is what made
        the defect invisible: the bare case was covered and correct, and the
        tag case sat one row away asserting the opposite.
        """
        for tag in ("latest", "next", "*"):
            assert g.is_pinned("npx", f"pkg@{tag}") == g.is_pinned("npx", "pkg")

    def test_bare_name_still_strips_a_floating_tag(self):
        """`is_pinned` got stricter; `bare_name` must NOT have.

        A cache is keyed by NAME, so stripping `@latest` is correct there --
        the two functions ask different questions and the fix must not have
        leaked the stricter answer into the looser one.
        """
        assert g.bare_name("npx", "pkg@latest") == "pkg"
        assert g.bare_name("npx", "pkg@^2.0.0") == "pkg"
        assert g.bare_name("uvx", "pkg>=2") == "pkg"

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


class TestTheFlagHasOneName:
    def test_the_module_reads_the_flag_the_registry_declares(self):
        """A switch row that names a different variable from the one the code
        reads describes a control nobody has: `doctor --switches` would print an
        arming line that changes nothing. Cheap to pin, and this exact copy-drift
        is the estate's named recurring defect."""
        from claudlobby import switches as sw

        assert sw.by_key("mcp-package-probe").env == mp.PROBE_FLAG

    def test_the_namespace_it_claims_is_ours(self):
        """Claiming a namespace makes the validator's dead-flag sweep warn about
        every unregistered <NS>_..._ENABLED in a fleet .env, so the prefix must
        be one no fleet would spell for its own tooling."""
        from claudlobby import switches as sw

        assert mp.PROBE_FLAG.split("_", 1)[0] == "CLAUDLOBBY"
        assert "MCP" not in sw.namespaces(), (
            "MCP is an industry term — claiming it would warn on a fleet's own vars"
        )


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
    """Structural, plus the one guard the module docstring names."""

    def test_no_shipped_fragment_launches_an_unpinned_npx_package(self):
        # #1890: an unversioned `npx -y <pkg>` runs whatever the registry serves
        # at boot on every bot equipping the fragment, and an unscoped name
        # nobody owns can be claimed by anyone (printify-mcp was one). #1058's
        # own predicate, over the whole shipped library.
        rows = g.declared_packages([str(REPO_ROOT / "library" / "mcp")])
        unpinned = {f.fragment for f in mp.pinning_findings(rows) if f.runtime == "npx"}
        # One allowance: spotify.json names a package npm does not have
        # (@modelcontextprotocol/server-spotify, E404), so there is no version
        # to pin, and the scope is npm-owned, so the name cannot be claimed.
        # A SUBSET, so removing or replacing it passes.
        assert unpinned <= {"spotify.json"}, sorted(unpinned)

    def test_every_shipped_fragment_is_readable_by_the_grammar(self):
        rows = g.declared_packages([str(REPO_ROOT / "library" / "mcp")])
        assert rows, "positive control: the shared library declares packages"
        for frag, server, runtime, spec, bare, pinned, argv in rows:
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

    def test_a_fleet_declaring_no_mcp_says_NOTHING_even_with_no_lib(self, tmp_path):
        """The rung must not announce that a check of nothing did not run.

        Regression: the first build reached for the shared grammar before
        asking whether the fleet declared anything, so every fleet with no MCP
        — which is every minimal fixture in this repo — got a "could not load
        the grammar" warning. It broke four unrelated validator tests, and it
        broke them in a way worth recording: those tests assert on SUBSTRINGS
        of the warning list, the warning interpolated an absolute tmp path, and
        the tmp path is named after the test. So `"observability" in w` matched
        the PATH rather than any text this module wrote.
        """
        from claudlobby.config import BotConfig, FleetConfig
        from claudlobby import validator
        from claudlobby.paths import Paths

        # No lib/, no library/ — the grammar cannot load from this root at all.
        bot = BotConfig(bot_id="alpha", name="alpha", expertise=["x"], mcp=[])
        fleet = FleetConfig(
            name="probe", service_prefix="com.example.probe", bots={"alpha": bot}
        )
        report = validator.ValidationReport()
        validator._validate_mcp_packages(fleet, Paths(root=tmp_path), report)

        assert report.warnings == []
        assert report.errors == []

    def test_the_grammar_warning_carries_no_absolute_path(self, tmp_path):
        """Declaring MCP with no reachable grammar DOES warn — and the warning
        must not interpolate a filesystem path (see the regression above)."""
        from claudlobby.config import BotConfig, FleetConfig, McpEntry
        from claudlobby import validator
        from claudlobby.paths import Paths

        mcp_dir = tmp_path / "library" / "mcp"
        mcp_dir.mkdir(parents=True)
        (mcp_dir / "ghost.json").write_text(
            json.dumps({"ghost": {"command": "npx", "args": ["-y", "ghost-pkg"]}})
        )
        bot = BotConfig(
            bot_id="alpha", name="alpha", expertise=["x"], mcp=[McpEntry(name="ghost")]
        )
        fleet = FleetConfig(
            name="probe", service_prefix="com.example.probe", bots={"alpha": bot}
        )
        report = validator.ValidationReport()
        validator._validate_mcp_packages(fleet, Paths(root=tmp_path), report)

        assert len(report.warnings) == 1
        assert "UNKNOWN" in report.warnings[0]
        assert str(tmp_path) not in report.warnings[0], (
            "a path in the message collides with other tests' substring assertions"
        )

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
