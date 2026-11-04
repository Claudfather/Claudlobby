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
    """Docs distinguish preparation, credentials and explicit native startup."""

    def test_onboarding_uses_one_git_preparation_and_original_update_route(self):
        for doc in (README, GETTING_STARTED, SETUP_SKILL):
            text = doc.read_text()
            assert "host setup" in text and "fleet setup" in text, doc
            assert "--plan-only" in text and "host update continue" in text, doc
            assert not any(flag in text for flag in
                           ("--guided", "--wheelhouse", "--dependency-lock", "claudlobby-install.pyz")), doc
        commands = "\n".join(command for _, command in _commands(GETTING_STARTED.read_text()))
        assert "-m claudlobby.bootstrap" in commands
        for flag in ("--installation", "--root", "--remote", "--ref", "--interpreter"):
            assert flag in commands
        assert "--install-directory" in commands and "host update status" in commands

    @pytest.mark.parametrize("doc", [README, GETTING_STARTED, CONTRIBUTOR_GUIDE], ids=lambda p: p.name)
    def test_no_bare_or_system_pip_install(self, doc):
        lines = _shell_lines(doc)
        assert not [line for line in lines if re.match(r"^(sudo\s+)?pip3?\s+install\b", line)]
        if doc in (README, GETTING_STARTED):
            assert not [line for line in lines if "pip install" in line]

    def test_python_probe_creates_and_runs_an_independent_environment(self):
        commands = "\n".join(command for _, command in _commands(GETTING_STARTED.read_text()))
        assert "-m venv" in commands and '"$PROBE/venv/bin/python"' in commands
        assert "sys.prefix != sys.base_prefix" in commands
        assert "ssl" in commands and "resolve(strict=True)" in commands
        assert "sys.version_info >= (3, 10)" in commands
        assert not [line for line in _shell_lines(GETTING_STARTED) if re.search(r"\bexit\b", line)]

    def test_credentials_and_startup_are_separate_gates(self):
        text = GETTING_STARTED.read_text().lower()
        assert text.index("credential gate") < text.index("## 2.")
        assert text.index("agent-start gate") < text.index("## 3.")
        for source in (README, GETTING_STARTED, SETUP_SKILL):
            text = source.read_text().lower()
            assert "telegram" in text and "operator" in text
            assert "first-team-setup.md" in text
        first_team = (REPO_ROOT / "documentation/first-team-setup.md").read_text()
        assert "--template revenue-web-app" in first_team and "--plan-only" in first_team
        assert "host update continue" in first_team
        assert "not completed cold-host acceptance" in first_team
        assert "--guided" not in first_team



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


# --- Staging and activation never take --fleet or --seed (#2567) ---------------

# `config plan`, `config diff PLAN_ID` and `host activate` cover the whole host, and
# each refuses --fleet and --seed ("host preparation rejects --fleet and --seed",
# `_host_root` in claudlobby/commands/releases.py), so a doc that passes either stages
# nothing. After `config plan`, argparse reads a bare `--fleet X` as `--fleet-path X`.
# The form that works is the sealed CLI with an explicit --root on every command
# (library/protocols/canary-rollout.md).
_HOST_WIDE = re.compile(
    r"\bconfig\s+plan\b|\bhost\s+activate\b|\bconfig\s+diff\s+(?:--json\s+)*(?!-)[^\s;&|]"
)
_FLEET_OR_SEED = re.compile(r"(?<![\w-])--(?:fleet|seed)(?![\w-])")
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_COMMAND_SEP = re.compile(r";|&&|\|\|?")
_COMMENT_RE = re.compile(r"(?:^|\s)#.*")


def _commands(text: str) -> list[tuple[int, str]]:
    """(line, command) for each command a doc shows: a code-fence line, with its
    backslash continuations joined and its comment cut, or an inline code span.
    Each is split at `;`, `&&`, `||` and `|`, so a flag counts only in its own command."""
    found: list[tuple[int, str]] = []
    fenced, pending, start = False, "", 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced, pending = not fenced, ""
            continue
        if not fenced:
            found += [(number, part) for span in _SPAN_RE.findall(line)
                      for part in _COMMAND_SEP.split(span)]
            continue
        start = start if pending else number
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        code, pending = _COMMENT_RE.sub("", pending + line), ""
        found += [(start, part) for part in _COMMAND_SEP.split(code)]
    return found


def _command_docs() -> list[Path]:
    docs = [REPO_ROOT / name for name in ("README.md", "CLAUDE.md", "AGENTS.md")]
    for top in ("documentation", "library", "templates", ".claude"):
        docs += sorted(p for p in (REPO_ROOT / top).rglob("*")
                       if p.suffix in (".md", ".j2") and p.is_file())
    return docs


def test_no_doc_passes_fleet_or_seed_to_staging_or_activation():
    """A stranger following our own procedure must stage something (#2567).
    CHANGELOG.md is not read: it records what was, not what to type."""
    offenders = [
        f"{doc.relative_to(REPO_ROOT)}:{line}: {command.strip()}"
        for doc in _command_docs()
        for line, command in _commands(doc.read_text())
        if _HOST_WIDE.search(command) and _FLEET_OR_SEED.search(command)
    ]
    assert not offenders, (
        "these pass --fleet or --seed to a host-wide command, which refuses it:\n  "
        + "\n  ".join(offenders)
        + "\nUse the sealed CLI with --root on every command and no --fleet "
        "(library/protocols/canary-rollout.md)."
    )


# --- The switch tables, and `bot remove` (#2575) -------------------------------------

# fleet-yaml-schema.md and system-yaml-schema.md carry the switch tables as prose that
# claudlobby/switches.py generates, outside the code spans the rule above reads, so this
# one reads the registry. A command is the text from one `claudlobby` to the next: the
# --fleet of `host cache warm` must not stand in front of the verbs that follow it. The
# package is imported inside the test, so a broken import fails this test alone.
_PROGRAM = re.compile(r"(?=\bclaudlobby\s)")


def test_no_switch_line_passes_fleet_or_seed_to_staging_or_activation():
    from claudlobby import switches

    offenders = [
        f"{row.key} ({name}): {command.strip()}"
        for row in switches.SWITCHES
        for name, line in (("arm", row.arm), ("disarm", row.disarm))
        for command in _PROGRAM.split(line)
        if _HOST_WIDE.search(command) and _FLEET_OR_SEED.search(command)
    ]
    assert not offenders, (
        "these switch lines pass --fleet or --seed to a host-wide command, which refuses it:\n  "
        + "\n  ".join(offenders)
        + "\nGive each command its own `claudlobby --root <data-root>` and its own flags."
    )


# `bot remove` refuses without an explicit --root and --fleet ("bot remove requires
# explicit --root and --fleet", claudlobby/commands/bot_remove.py). A span that starts
# with the verb names it; one with anything before the verb shows a command to run.
_BOT_REMOVE = re.compile(r"\S.*\bbot\s+remove\b")
_ROOT = re.compile(r"(?<![\w-])--root(?![\w-])")
_FLEET = re.compile(r"(?<![\w-])--fleet(?![\w-])")


def test_no_doc_shows_bot_remove_without_root_and_fleet():
    offenders = [
        f"{doc.relative_to(REPO_ROOT)}:{line}: {command.strip()}"
        for doc in _command_docs()
        for line, command in _commands(doc.read_text())
        if _BOT_REMOVE.search(command) and not (_ROOT.search(command) and _FLEET.search(command))
    ]
    assert not offenders, (
        "these show `bot remove` without both --root and --fleet, which it requires:\n  "
        + "\n  ".join(offenders)
    )
