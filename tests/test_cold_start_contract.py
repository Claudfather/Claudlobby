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

import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
GETTING_STARTED = REPO_ROOT / "documentation" / "getting-started.md"
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


class TestDocumentedInstallPath:
    """The install commands the docs hand a user must work on a stock host."""

    @pytest.mark.parametrize("doc", [README, GETTING_STARTED], ids=lambda p: p.name)
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

    @pytest.mark.parametrize("doc", [README, GETTING_STARTED], ids=lambda p: p.name)
    def test_install_is_accompanied_by_a_venv(self, doc: Path):
        """Any doc that installs the package must first create a virtualenv.

        Homebrew python (macOS) and Debian/Raspberry Pi system python are both
        externally-managed under PEP 668 and refuse an install into the
        interpreter. Those are the two hosts this project targets first, so an
        install instruction without a venv is a blocker, not a style nit.
        """
        lines = _shell_lines(doc)
        installs_package = any(re.search(r"pip\s+install\s+-e\s+\.", ln) for ln in lines)
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
        assert "import claudlobby." in src, (
            "expected a submodule import probe (e.g. `import claudlobby.composer`) "
            "in claudlobby_cli"
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

    @pytest.mark.skipif(
        not (REPO_ROOT / ".venv" / "bin" / "python").exists(),
        reason="no repo-local .venv on this host",
    )
    def test_cli_resolves_with_the_venv_off_path(self, tmp_path: Path):
        """End-to-end: strip PATH the way launchd does and resolve the CLI."""
        script = tmp_path / "probe.sh"
        script.write_text(
            textwrap.dedent(
                f"""\
                export CLAUDLOBBY_ROOT="{REPO_ROOT}"
                . "$CLAUDLOBBY_ROOT/lib/lib-common.sh"
                claudlobby_cli --version
                """
            )
        )
        # A deliberately minimal PATH: no venv, no pipx shims.
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(tmp_path),
            },
        )
        assert result.returncode == 0, (
            f"claudlobby_cli failed with the venv off PATH:\n{result.stderr}"
        )
        assert "claudlobby" in result.stdout.lower(), result.stdout


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

    def test_validator_treats_placeholders_as_errors_not_warnings(self):
        """Warnings are documented as an acceptable outcome, so this must error.

        getting-started tells the user a warnings-only run is a success. A
        placeholder warning would therefore read as 'fine' and ship a bot that
        posts to chat id REPLACE_ME.
        """
        src = (REPO_ROOT / "claudlobby" / "validator.py").read_text()
        block = re.search(
            r"def _validate_placeholders.*?(?=\ndef |\Z)", src, re.DOTALL
        )
        assert block, "_validate_placeholders is gone — the F6 guard was removed"
        body = block.group(0)
        assert "report.errors.append" in body
        assert "report.warnings.append" not in body, (
            "placeholders must be hard errors, not warnings"
        )


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
