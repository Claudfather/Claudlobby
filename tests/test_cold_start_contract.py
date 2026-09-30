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

import re
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

    def test_cold_host_docs_use_setup_then_activation(self):
        # `lib/setup-fleet` is retired with `lib/setup-system`: `fleet setup`
        # validates before it activates, so no doc may chain the old wrapper
        # after a validate (#1681's quickstart shape).
        for doc in (README, GETTING_STARTED, SETUP_SKILL):
            text = doc.read_text()
            assert "host setup" in text and "fleet setup" in text, doc
            assert "lib/setup-system" not in text and "host-timers" not in text, doc
            assert "lib/setup-fleet" not in text, doc

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
        installs_package = any(re.search(r"(?:-m\s+)?pip\s+install\b", ln) for ln in lines)
        if not installs_package:
            # The README delegates assembly to the canonical walkthrough.
            assert doc == README and "documentation/getting-started.md" in doc.read_text()
            lines = _shell_lines(GETTING_STARTED)
            assert any(re.search(r"-m\s+pip\s+install\b", ln) for ln in lines)

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
        for source in (README, GETTING_STARTED, SETUP_SKILL):
            assert "fleet.yaml.seed" in source.read_text(), source
        # The executable copy uses the installed package path, including when
        # the operator is still in the source checkout after building it.
        walkthrough = GETTING_STARTED.read_text()
        assert 'cp "$SEEDS/fleet.yaml.seed" "$WORK/fleet.yaml"' in walkthrough
        assert 'SEEDS=$("$WORK/bootstrap/bin/python" -I -c' in walkthrough



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


# --- Example IDs in the onboarding docs are obviously fake (#2002, finding F3) --

_ID_RE = re.compile(r"(?<![\w.:/-])(-?\d{7,})(?![\w:])")


def _obviously_fake(token: str) -> bool:
    """An ascending run (`1234567890`, after an optional `-` or `-100`) or a
    single repeated digit (`8888888`), the placeholder styles CLAUDE.md's PII
    rule uses. Anything else could be someone's real chat or user ID."""
    digits = token.lstrip("-")
    if token.startswith("-100") and len(digits) > 10:
        digits = digits[3:]
    return "1234567890123456789".startswith(digits) or len(set(digits)) == 1


@pytest.mark.parametrize("doc", [README, GETTING_STARTED, SETUP_SKILL], ids=lambda p: p.name)
def test_example_ids_in_onboarding_docs_are_obviously_fake(doc: Path):
    real_looking = [t for t in _ID_RE.findall(doc.read_text()) if not _obviously_fake(t)]
    assert not real_looking, (
        f"{doc.relative_to(REPO_ROOT)} carries ID-shaped numbers that are not obviously "
        f"fake placeholders: {real_looking}"
    )
