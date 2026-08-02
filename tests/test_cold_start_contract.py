"""Cold-start contract — guards the path a brand-new user actually walks.

Why this file exists: the documented bootstrap rotted for months while the whole
suite stayed green, because nothing ever executed it. CI installs with
`pip install -e '.[dev]'` on ubuntu-latest, where `pip` exists and setup-python
hands you a non-externally-managed environment — so PEP 668 never fires and the
two blockers a real user hits first are invisible by construction.

These tests encode the contract instead: what the docs are allowed to tell a
user to run, and what the CLI resolver is allowed to assume. They are fast and
host-independent, so they run everywhere the rest of the suite does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
GETTING_STARTED = REPO_ROOT / "documentation" / "getting-started.md"
# CLAUDE.md is an onboarding doc too — it is what a contributor (or an agent)
# reads first, and it carried the same bare-`pip` instruction long after the
# user-facing docs were fixed. Anything that tells a human what to type counts.
CONTRIBUTOR_GUIDE = REPO_ROOT / "CLAUDE.md"
SETUP_SKILL = REPO_ROOT / ".claude" / "skills" / "setup" / "SKILL.md"
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"

_FENCE_RE = re.compile(r"```(?:bash|sh|console)\n(.*?)```", re.DOTALL)


def _shell_lines(doc: Path) -> list[str]:
    """Every non-comment shell line inside ```bash fences in ``doc``."""
    out: list[str] = []
    for block in _FENCE_RE.findall(doc.read_text()):
        for raw in block.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def _synthetic_root(tmp_path: Path) -> Path:
    """A CLAUDLOBBY_ROOT where **both** resolver rungs would succeed.

    That is the point: the fixture package imports under any interpreter, so the
    system-python rung is viable, and a stub `.venv/bin/python` makes the venv
    rung viable too. With only one rung viable a test cannot tell which one ran,
    which is how a rung-order mutation survived the previous test.

    The stub venv is a shell wrapper that announces itself and then execs the
    real interpreter, so its use is observable in stdout. No actual virtualenv
    is created — that kept the old test skipped on CI and on fresh clones.
    """
    root = tmp_path / "clroot"
    pkg = root / "claudlobby"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "composer.py").write_text("")  # satisfies the usability probe
    (pkg / "__main__.py").write_text("import sys; print('MODULE', *sys.argv[1:])")

    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "python"
    stub.write_text(f'#!/bin/bash\nprintf "VENVPY\\n"\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return root


class TestDocumentedInstallPath:
    """The install commands the docs hand a user must work on a stock host."""

    @pytest.mark.parametrize(
        "doc", [README, GETTING_STARTED, CONTRIBUTOR_GUIDE], ids=lambda p: p.name
    )
    def test_no_bare_pip_invocation(self, doc: Path):
        """`pip ...` as a command is not portable — Homebrew ships pip3 only.

        A stock macOS has no `pip` on PATH at all, so a doc line starting with
        `pip install` dies with 'command not found' before PEP 668 even gets a
        say. `python3 -m pip` works anywhere `python3` does.
        """
        offenders = [
            line
            for line in _shell_lines(doc)
            if re.match(r"^(sudo\s+)?pip3?\s+install\b", line)
        ]
        assert not offenders, (
            f"{doc.name} tells users to run bare pip:\n  "
            + "\n  ".join(offenders)
            + "\nUse 'python3 -m pip install' (or a venv interpreter) instead."
        )

    @pytest.mark.parametrize(
        "doc", [README, GETTING_STARTED, CONTRIBUTOR_GUIDE], ids=lambda p: p.name
    )
    def test_install_is_accompanied_by_a_venv(self, doc: Path):
        """Any doc that installs the package must first create a virtualenv.

        Homebrew python (macOS) and Debian/Raspberry Pi system python are both
        externally-managed under PEP 668 and refuse an install into the
        interpreter. Those are the two hosts this project targets first, so an
        install instruction without a venv is a blocker, not a style nit.
        """
        lines = _shell_lines(doc)
        # Match the extras form too — `-e '.[dev]'` is how the contributor guide
        # installs, and an earlier bare `-e \.` pattern silently *skipped* that
        # file rather than checking it.
        installs_package = any(
            re.search(r"pip\s+install\s+-e\s+['\"]?\.", ln) for ln in lines
        )
        if not installs_package:
            pytest.skip(f"{doc.name} does not install the package")

        assert any("venv" in ln for ln in lines), (
            f"{doc.name} installs the package but never creates a venv. "
            "PEP 668 makes that install fail on both supported host families."
        )

    def test_every_entry_point_agrees_on_the_first_run_template(self):
        """The three onboarding entry points must name the same template.

        They disagreed: README and getting-started both said fleet.yaml.example
        (a ~600-line multi-bot manifest) while the /setup skill said
        fleet.yaml.seed (one bot). A newcomer following the README therefore got
        the hardest possible starting point, and the guided and manual paths
        diverged at step one.

        The skill is included deliberately — the docs agreed with *each other*
        the whole time, so a docs-only comparison would have stayed green.
        """
        pattern = re.compile(r"[Cc]opy\s+`?(fleet\.yaml\.\w+)|cp\s+(fleet\.yaml\.\w+)")

        def templates(source: Path) -> set[str]:
            found = pattern.findall(source.read_text())
            return {m for pair in found for m in pair if m}

        sources = {
            "README.md": README,
            "getting-started.md": GETTING_STARTED,
            "setup SKILL.md": SETUP_SKILL,
        }
        named = {label: templates(p) for label, p in sources.items() if p.is_file()}
        named = {label: t for label, t in named.items() if t}
        if len(named) < 2:
            pytest.skip("fewer than two entry points name a first-run template")

        union: set[str] = set()
        for t in named.values():
            union |= t
        assert len(union) == 1, (
            "onboarding entry points disagree on the first-run template: "
            + "; ".join(f"{label} → {sorted(t)}" for label, t in named.items())
            + " — pick one and make the others reference it."
        )


class TestCliResolutionProbe:
    """`claudlobby_cli` must not mistake an importable package for a usable one."""

    def test_bare_package_import_is_a_false_positive(self, tmp_path: Path):
        """Demonstrates the trap this guard exists for.

        `claudlobby/` is a plain package directory at the repo root, so
        `cd <root> && python3 -c 'import claudlobby'` succeeds from cwd alone —
        with zero dependencies installed. Any resolver probing that way commits
        to a broken interpreter and dies later on a raw ModuleNotFoundError.
        """
        pkg = tmp_path / "claudlobby"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        probe = subprocess.run(
            [sys.executable, "-c", "import claudlobby"],
            cwd=tmp_path,
            capture_output=True,
        )
        assert probe.returncode == 0, (
            "expected the bare import to succeed from cwd — if this ever fails, "
            "the false-positive premise changed and the guard below can relax"
        )

    def test_lib_common_probes_a_submodule_not_the_bare_package(self):
        """The resolver must import something that actually pulls the deps."""
        src = LIB_COMMON.read_text()

        bare = re.findall(r"python3?\s+-c\s+'import claudlobby'", src)
        assert not bare, (
            "lib-common.sh probes `import claudlobby`, which succeeds from cwd "
            "with no dependencies installed. Probe a submodule that imports the "
            "third-party deps (e.g. claudlobby.composer)."
        )
        # Renaming composer.py must fail here rather than silently breaking CLI
        # resolution on every supervised bot — that CI cost is what makes the
        # probe's coupling to a module name acceptable.
        assert "claudlobby.composer" in src, (
            "expected a submodule probe (claudlobby.composer) in claudlobby_cli — "
            "if composer.py was renamed, update the probe with it"
        )

    def test_resolver_prefers_the_repo_local_venv(self):
        """A venv console script is not on PATH under launchd/systemd.

        That is the common supervised case, not an exotic one — so the resolver
        has to reach $CLAUDLOBBY_ROOT/.venv itself rather than assuming an
        activated shell.
        """
        src = LIB_COMMON.read_text()
        assert ".venv/bin/python" in src, (
            "claudlobby_cli does not prefer $CLAUDLOBBY_ROOT/.venv — supervised "
            "runs will fall through to a system python without the deps"
        )

    def test_prefers_the_repo_local_venv_over_system_python(self, tmp_path: Path):
        """Behavioural rung-order check — asserts WHICH interpreter served the call.

        Replaces a version that was `skipif` on a repo-local `.venv` existing.
        That guard never fired on CI or on a fresh clone (neither creates one),
        so the only behavioural test of CLI resolution was dormant exactly where
        it mattered. Worse, when it *did* run it still passed a mutation that
        swapped the rung order — it asserted only that the CLI resolved, never
        that the venv served it, which is the entire property F3 rests on
        (PR #947 review).

        Here both rungs are deliberately viable: the fixture package imports
        under any interpreter, and a stub `.venv/bin/python` announces itself.
        So the assertion can distinguish them.
        """
        root = _synthetic_root(tmp_path)
        result = subprocess.run(
            ["bash", "-c", f'. "{LIB_COMMON}"\nclaudlobby_cli generate'],
            env={
                "CLAUDLOBBY_ROOT": str(root),
                "PATH": "/usr/bin:/bin",  # no console script, no pipx shims
                "HOME": str(tmp_path),
                # The reviewer hit a false PASS from an editable claudlobby in
                # user site-packages, which resolves regardless of cwd. Scrub it.
                "PYTHONNOUSERSITE": "1",
            },
            cwd="/",  # deliberately not the repo root
            capture_output=True,
            text=True,
        )
        assert "MODULE generate" in result.stdout, result.stdout + result.stderr
        assert "VENVPY" in result.stdout, (
            "claudlobby_cli resolved via system python3 rather than "
            "$CLAUDLOBBY_ROOT/.venv. The venv rung must win: a venv console "
            "script is not on PATH under launchd/systemd, so preferring the "
            "system interpreter silently drops the dependencies.\n"
            + result.stdout
            + result.stderr
        )

    def test_falls_through_to_system_python_when_no_venv(self, tmp_path: Path):
        """The venv preference must not become a venv *requirement*.

        Rung 3 exists for a checkout whose deps are installed system-wide (the
        Pi/apt case). Removing the stub venv must fall through, not fail.
        """
        root = _synthetic_root(tmp_path)
        shutil.rmtree(root / ".venv")
        result = subprocess.run(
            ["bash", "-c", f'. "{LIB_COMMON}"\nclaudlobby_cli generate'],
            env={
                "CLAUDLOBBY_ROOT": str(root),
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "PYTHONNOUSERSITE": "1",
            },
            cwd="/",
            capture_output=True,
            text=True,
        )
        assert "MODULE generate" in result.stdout, result.stdout + result.stderr
        assert "VENVPY" not in result.stdout


class TestSeedPlaceholderContract:
    """Keeps the validator's placeholder check meaningful."""

    def test_shipped_seed_still_carries_placeholders(self):
        """If the seed stops using REPLACE_ME, the validator guard must follow.

        The two are a pair: fleet.yaml.seed ships deliberately-unset values, and
        validate hard-errors on them. Changing the token in one place without
        the other silently disarms the check.
        """
        seed = (REPO_ROOT / "fleet.yaml.seed").read_text()
        assert "REPLACE_ME" in seed, (
            "fleet.yaml.seed no longer contains REPLACE_ME — update "
            "_PLACEHOLDER_TOKENS in claudlobby/validator.py to match, or this "
            "check is dead weight"
        )

    def test_shipped_seed_actually_fails_validation(self):
        """Behavioural, against the real seed and the real library.

        This replaces a source-pattern version that inspected the body of
        `_validate_placeholders` for `report.errors.append`. That version stayed
        green when the guard was **unwired** — commenting out the call site left
        the function, and both assertions, untouched (PR #947 review). Only
        calling `validate()` catches that, so this calls it.

        Errors, never warnings: getting-started documents a warnings-only run as
        success, so a placeholder warning would read as 'fine' and ship a bot
        pointed at chat id REPLACE_ME.
        """
        from claudlobby.config import load_fleet
        from claudlobby.paths import Paths
        from claudlobby.validator import validate

        paths = Paths(root=REPO_ROOT, seed=True)
        fleet, _meta = load_fleet(paths.fleet_yaml)
        report = validate(fleet, paths)

        assert report.has_errors, (
            "the shipped fleet.yaml.seed must fail validation on its own "
            "REPLACE_ME values — is _validate_placeholders still wired into "
            "validate()?"
        )
        joined = "\n".join(report.errors)
        for field in ("telegram_group_chat_id", "human_telegram_id", "telegram.handle"):
            assert field in joined, f"no placeholder error for {field}:\n{joined}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
class TestSetupSystemHonesty:
    """`setup-system --dry-run` must not report post-conditions it never took."""

    def test_dry_run_does_not_assert_an_install_it_skipped(self):
        """The success line must live inside the real-mode branch.

        It used to sit after the if/else, so --dry-run printed
        '✓ claudlobby package installed' and 'prereqs missing: (none)' on a host
        with no claudlobby at all. The /setup skill is told to parse that output
        and skip ahead, so the false green propagated into the guided flow.
        """
        src = (REPO_ROOT / "lib" / "setup-system").read_text()
        assert "_claudlobby_importable" in src, (
            "setup-system no longer verifies the install actually worked"
        )
        # The dry-run path must record the state that currently holds.
        assert re.search(r"DRY_RUN.*=.*1", src)
        assert "not installed yet" in src, (
            "dry-run must report claudlobby as missing when it is missing, "
            "rather than asserting the install it only described"
        )

    def test_dry_run_never_ticks_something_it_only_described(
        self, setup_system_dry_run
    ):
        """Behavioural, against real `--dry-run` output on this host.

        The invariant: a line announcing an action was *skipped* must never be
        immediately followed by a tick claiming that action succeeded. That is
        the whole bug class — the success line living outside the if/else — and
        it shipped twice: phase 4 (claudlobby, fixed first) and phase 6 (the
        claudna plugin, found only by re-running setup end to end afterwards).

        A source-grep version of this would have passed on phase 6 while it was
        still broken, since each phase spells the mistake differently. Parsing
        real output catches any phase, including ones not written yet.

        Uses the session-scoped fixture rather than spawning its own run: the
        script probes real tools and costs ~0.6s, and tests/test_setup_system.py
        already invoked it. Sharing also keeps one contract for what a non-zero
        exit means, instead of this module skipping where that one asserts.
        """
        proc = setup_system_dry_run
        assert proc.returncode == 0, f"setup-system --dry-run failed: {proc.stderr}"

        lines = [ln.strip() for ln in proc.stdout.splitlines()]
        # Pairwise over consecutive lines; ✓ is the ok() marker, ○ (miss) and a
        # further would-run line are both fine.
        offenders = [
            f"{line}\n    -> {nxt}"
            for line, nxt in zip(lines, lines[1:])
            if "[dry-run] would run" in line and "✓" in nxt
        ]

        assert not offenders, (
            "dry-run reported success for an action it skipped:\n"
            + "\n".join(offenders)
            + "\n\nThe /setup skill is told to parse this output and skip ahead, "
            "so a tick here sends the guided flow past a missing dependency."
        )

    def test_dry_run_summary_does_not_claim_a_genuinely_absent_tool(
        self, sysbin_excluding
    ):
        """The same lie, in the variant the test above structurally cannot see.

        `phase_node` recorded PREREQ_OK unconditionally but printed no ✓, so its
        false claim reached only the summary array — invisible to a scan for a
        tick following a would-run line. Two phases carried the bug past the
        first fix for exactly that reason.

        So this asserts the summary itself. PATH is narrowed to a mirror of the
        system bin dirs, which excludes the Homebrew prefix where node lives, so
        node is genuinely unresolvable and the install branch really executes.
        """
        mirror = sysbin_excluding()  # no exclusions needed; the mirror omits brew/node
        proc = subprocess.run(
            [str(REPO_ROOT / "lib" / "setup-system"), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": str(mirror), "HOME": os.environ.get("HOME", "/tmp")},
        )
        if proc.returncode != 0:
            pytest.skip(f"setup-system could not run on the narrowed PATH: {proc.stderr[-300:]}")

        ok_line = next(
            (ln for ln in proc.stdout.splitlines() if "prereqs ok:" in ln), ""
        )
        missing_line = next(
            (ln for ln in proc.stdout.splitlines() if "prereqs missing:" in ln), ""
        )
        assert ok_line, f"no summary in output:\n{proc.stdout[-800:]}"

        assert "node" not in ok_line.split(":", 1)[1].split(), (
            "dry-run listed node under 'prereqs ok' on a host where node is not "
            f"resolvable and it only ever said it *would* install it.\n"
            f"{ok_line}\n{missing_line}"
        )
        assert "node" in missing_line, (
            f"node was absent and never installed, so it belongs in "
            f"'prereqs missing'.\n{ok_line}\n{missing_line}"
        )
