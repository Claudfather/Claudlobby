"""Cold-start contract — guards the path a brand-new user actually walks.

Why this file exists: the documented bootstrap rotted for months while the whole
suite stayed green, because nothing ever executed it. CI installs with
`pip install -e '.[dev]'` on ubuntu-latest, where `pip` exists and setup-python
hands you a non-externally-managed environment — so PEP 668 never fires and the
two blockers a real user hits first are invisible by construction.

These tests encode the contract instead: what the docs are allowed to tell a
user to run. Selected CLI entrypoint behavior is covered by
`test_maintenance_jobs.py`; these checks cover onboarding and setup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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
        from tests.package_fixtures import source_package
        from claudlobby.paths import Paths
        from claudlobby.validator import validate

        paths = Paths(root=REPO_ROOT, seed=True, package=source_package())
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
        # Exclude explicitly rather than relying on the Homebrew prefix being
        # absent: on Linux these live in /usr/bin and the mirror would include
        # them, so the install branch would never run and the test would pass
        # without testing anything.
        mirror = sysbin_excluding("node", "tmux", "gh")
        proc = subprocess.run(
            [str(REPO_ROOT / "lib" / "setup-system"), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PLANE_EMIT_DISABLED": "1",
                "PATH": str(mirror),
                "HOME": os.environ.get("HOME", "/tmp"),
                # phase_systemd dereferences $USER under `set -u`; without it the
                # script exits non-zero on Linux and the skip below swallows the
                # whole check — the Linux-only bugs would go unguarded.
                "USER": os.environ.get("USER", "runner"),
            },
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

        claimed_ok = set(ok_line.split(":", 1)[1].split())
        # Non-vacuous: if the mirror failed to hide these, the assertions below
        # would pass by describing nothing.
        assert "[dry-run] would run" in proc.stdout, (
            "no install was even attempted — the narrowed PATH did not hide the "
            f"tools, so this check proves nothing.\n{proc.stdout[-600:]}"
        )
        # node and gh only. Hiding a binary from PATH does not make every check
        # fail: on Linux tmux/jq/curl resolve through `dpkg -l`, which still
        # reports the package installed, so asserting on them tests the mirror
        # rather than the script. node and gh are PATH-determined on both
        # platforms, so their verdicts genuinely depend on the fix.
        for tool in ("node", "gh"):
            assert tool not in claimed_ok, (
                f"dry-run listed {tool} under 'prereqs ok' on a host where it is "
                f"not resolvable and it only ever said it *would* install it.\n"
                f"{ok_line}\n{missing_line}"
            )
            assert tool in missing_line, (
                f"{tool} was absent and never installed, so it belongs in "
                f"'prereqs missing'.\n{ok_line}\n{missing_line}"
            )
