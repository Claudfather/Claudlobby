"""Tests for library/skills/gitguardian, the GitGuardian incident helper (#2476).

Wrapped in Python so CI runs it. The surfaces:

  - ``gitguardian/test.sh``: the helper's own suite, run with the GITGUARDIAN_*
    env scrubbed. A stand-in curl replays synthetic answers and a throwaway
    repository holds the bytes triage reads, so it reaches no network.
  - **Read-only by construction.** Every request goes through one curl call, a
    GET on one of two routes, and no route that writes, or that returns a
    secret's value, appears in the helper at all. The token a bot holds may
    carry every scope, so this list, not the token, keeps the helper read-only.
  - **triage.py's hash is GitGuardian's**: it reproduces a published
    HasMySecretLeaked sample, so a match it confirms is the secret.
  - The execute bit, the env contract, the frontmatter and the grants, as
    ``tests/test_printify_skill.py`` pins them for printify.
  - **Composition**: ``integrations: [gitguardian]`` alone brings the skill and
    the protocol into a bot composed from the shipped library, and deleting the
    line takes them away again.

Why a wrapper, not ``tests/test_sh_suites.py``: the suite lives beside the
helper and reads ``fixtures/`` beside itself, and the scrub keeps a developer's
real token out of it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from claudlobby.composer import compose_bot
from claudlobby.config import load_fleet
from claudlobby.loader import frontmatter_error, parse_frontmatter
from claudlobby.validator import validate
from tests.conftest import make_paths
from tests.test_printify_skill import _grant_allows

REPO_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_DIR / "library" / "skills" / "gitguardian"
SCRIPT = SKILL_DIR / "gitguardian_api.sh"
TRIAGE = SKILL_DIR / "triage.py"
SUITE = SKILL_DIR / "test.sh"
INTEGRATION = REPO_DIR / "library" / "integrations" / "gitguardian.md"
PROTOCOL = REPO_DIR / "library" / "protocols" / "secret-incidents.md"
HELPER = ".claude/skills/gitguardian/gitguardian_api.sh"
READ_VERBS = ("status", "incidents", "incident", "triage", "help")
WRITE_VERBS = ("ignore", "resolve", "reopen")

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="jq not installed — the helper parses every answer with it",
)
needs_scrypt = pytest.mark.skipif(
    not hasattr(hashlib, "scrypt"), reason="this Python has no hashlib.scrypt"
)


@pytest.fixture(scope="module")
def hermetic_run() -> subprocess.CompletedProcess:
    """One run of the suite with every GITGUARDIAN_* variable stripped, and
    this interpreter first on PATH: triage.py runs as python3 and needs
    hashlib.scrypt. Module scope, because the suite is deterministic and the
    tests below read different facts off one run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITGUARDIAN_")}
    env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), env.get("PATH", "")))
    return subprocess.run(
        ["bash", str(SUITE)],
        cwd=SKILL_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@needs_jq
def test_skill_suite_passes_hermetically(hermetic_run):
    assert hermetic_run.returncode == 0, (
        f"gitguardian test.sh failed:\n{hermetic_run.stdout}\n{hermetic_run.stderr}"
    )
    assert re.search(r"\b0 failed\b", hermetic_run.stdout), hermetic_run.stdout


@needs_jq
@pytest.mark.parametrize(
    "check",
    [
        "status: HTTP 200 with a list reads reachable, exit 0; the token rides stdin only",
        "status: HTTP 403 reads insufficient_scope, exit 4, and prints no body",
        "ignore: refused, exit 7, with no request",
        "gg_get refuses /v1/incidents/secrets/101/ignore before any request",
        "gg_get sends a listed route",
        "incidents: a next link to a write route is refused before a second request",
        pytest.param(
            "triage: a placeholder in a fixture reads false_positive",
            marks=needs_scrypt,
        ),
        pytest.param(
            "triage: a value this session holds reads live and names its variable",
            marks=needs_scrypt,
        ),
        pytest.param(
            "triage: bytes the hash does not confirm are never read as the secret",
            marks=needs_scrypt,
        ),
        pytest.param(
            "triage: no output names a secret, path, commit, repository or hash",
            marks=needs_scrypt,
        ),
    ],
    ids=[
        "reachable", "insufficient-scope", "write-verb", "write-route", "listed-route",
        "write-next-link", "triage-placeholder", "triage-ours", "triage-unconfirmed",
        "triage-no-leak",
    ],
)
def test_the_load_bearing_checks_ran(hermetic_run, check):
    """The suite skips its triage block where python3 has no hashlib.scrypt,
    and a skip prints no failure, so 0 failed alone could hide one. Each of
    these must have run and passed; the triage ones skip, as the hash test
    does, where this Python has no scrypt."""
    assert f"ok   - {check}" in hermetic_run.stdout, hermetic_run.stdout


def test_the_helper_requests_two_read_routes_and_no_other():
    """GG_ROUTES is the whole list gg_allowed admits: the incident list and one
    incident, both GETs."""
    m = re.search(r"^GG_ROUTES='([^']*)'$", SCRIPT.read_text(), re.M)
    assert m, "GG_ROUTES not found in gitguardian_api.sh"
    assert m.group(1).splitlines() == ["/v1/incidents/secrets", "/v1/incidents/secrets/ID"]


# Routes that change an incident, share it, return a secret's value, send
# content out to be scanned, or manage tokens. None may appear in the helper or
# in triage.py, even in a comment, so no later edit wires one in unseen.
FORBIDDEN = (
    "/assign", "/unassign", "/resolve", "/ignore", "/reopen", "/notes",
    "/share", "/unshare", "/grant_access", "/revoke_access", "/check-presence",
    "/public-incidents", "/v1/secrets", "/content", "/scan", "/multiscan",
    "/api_tokens", "/code-fix-requests",
)


@pytest.mark.parametrize("path", [SCRIPT, TRIAGE], ids=lambda p: p.name)
def test_no_write_or_value_route_appears(path):
    text = path.read_text()
    found = [route for route in FORBIDDEN if route in text]
    assert not found, f"{path.name} names {found}"


def test_one_curl_call_and_it_sets_no_method_and_sends_no_body():
    """The argument list only. curl also reads a config on stdin, which could
    set a method or a second URL; test.sh's token_kept requires that config to
    be the one header line."""
    text = SCRIPT.read_text()
    sites = [m.start() for m in re.finditer(r"\bcurl\s+-q\s+--", text)]
    assert len(sites) == 1, f"expected one curl call, starting with -q, found {len(sites)}"
    call = text[sites[0]:text.index('--url "$url"', sites[0])]
    assert "--config -" in call and "--proto =https" in call, call
    flags = re.findall(
        r"(?<![\w-])(-X|--request|-d|--data[\w-]*|-F|--form[\w-]*|-T|--upload-file"
        r"|--json|-L|--location[\w-]*)(?![\w-])",
        call,
    )
    assert not flags, f"the curl call carries {flags}"


def _load_triage():
    """triage.py as a module, compiled from its source so that no bytecode
    lands in library/: the walkers over library/ read every file as text."""
    module = types.ModuleType("gitguardian_triage")
    module.__file__ = str(TRIAGE)
    exec(compile(TRIAGE.read_text(), str(TRIAGE), "exec"), module.__dict__)
    return module


@needs_scrypt
def test_the_hash_is_the_one_gitguardian_publishes():
    """triage.py takes a candidate as the secret only when its hash equals the
    incident's hmsl_hash, so one wrong parameter would make every check read
    cannot_check. GitGuardian's ggshield docs print this hash for one of their
    example passwords (docs.gitguardian.com, ggshield-docs/reference/hmsl/check).
    The password is assembled here, so this file holds no password-shaped
    string for a scanner to flag."""
    sample = "".join(("sup3r", "str0ng", "pass1", "ForGG"))
    assert _load_triage().hmsl(sample) == (
        "d775db0302080c1b7516109e929dd4b214a0f353ed3b66ff2e56c47d55a102ed"
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("REPLACE_ME", True),
        ("x" * 24, True),
        ("ghp_" + "x" * 36, True),
        ("8888888:" + "A" * 20, True),
        ("1234567890", True),
        ("ab_" + "x" * 5, False),
        ("x" * 12 + "q1w2e3r4t5y6", False),
        ("AKIA" + "x" * 16, False),
        ("a1b2c3d4e5f6g7h8", False),
        ("---", False),
        ("", False),
    ],
    ids=[
        "replace-me", "one-character", "prefix-and-run", "two-runs", "ascending",
        "short-parts", "run-then-noise", "no-separator", "random", "separators", "empty",
    ],
)
def test_placeholder_shapes(value, expected):
    assert _load_triage().is_placeholder(value) is expected


def test_an_unexpected_error_prints_a_fixed_line(monkeypatch, capsys):
    """An exception's message can hold what triage read, such as a path or a
    commit, so main prints only the exception's type."""
    module = _load_triage()

    def fail(argv):
        raise RuntimeError("src/app.py at 0123abcd")

    monkeypatch.setattr(module, "_triage", fail)
    assert module.main([]) == 6
    out = capsys.readouterr().out
    assert out == "unknown: triage.py stopped on RuntimeError, and printed nothing it read.\n"


@needs_scrypt
def test_the_cap_on_hashes_ends_a_search_with_note_budget(monkeypatch, capsys):
    """MAX_HASHES bounds the hashes one triage spends on candidates. Lower it to
    CAP and put the secret where it takes CAP + 1 hashes to reach: triage stops
    with note=budget. With CAP + 1 the same incident finds the secret, so the
    cap alone decided the first answer. The file read is replaced, so no git
    runs, and the environment is empty, so no session value is hashed."""
    module = _load_triage()
    cap = 3
    secret = "".join(("q1w2", "e3r4", "t5y6"))
    match = {
        "indice_start": cap,
        "indice_end": cap + len(secret),
        "post_line_start": 1,
        "post_line_end": 1,
    }
    occurrence = {
        "sha": "0" * 40,
        "filepath": "src/app.py",
        "change_type": "addition",
        "matches": [match],
    }
    incident = {
        "id": 101,
        "validity": "no_checker",
        "hmsl_hash": module.hmsl(secret),
        "occurrences_count": 1,
        "occurrences": [occurrence],
    }
    # The secret starts after CAP other positions on its line.
    monkeypatch.setattr(module, "_file_at", lambda *args: ("ok", "a" * cap + secret + "\n"))
    monkeypatch.setattr(module, "os", types.SimpleNamespace(environ={}))

    def triage(max_hashes: int) -> str:
        monkeypatch.setattr(module, "MAX_HASHES", max_hashes)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(incident)))
        rc = module.main(["checkout"])
        out = capsys.readouterr().out
        assert rc == 0, out
        return out

    stem = "triage: incident=101 class=needs_operator validity=no_checker"
    tail = "ours=no path_class=source occurrences=1/1"
    assert triage(cap) == f"{stem} placeholder=cannot_check {tail} note=budget\n"
    assert triage(cap + 1) == f"{stem} placeholder=no {tail}\n"


def test_the_shipped_cap_is_1500_hashes():
    """MAX_HASHES as shipped. The test above lowers it and sees the search stop
    there, so the search reads it; this pins the value it reads."""
    assert _load_triage().MAX_HASHES == 1500


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/fixtures/answer.json", "fixture"),
        ("tests/test_app.py", "test"),
        ("pkg/app_test.go", "test"),
        ("web/app.spec.ts", "test"),
        ("docs/setup.md", "docs"),
        ("README.md", "docs"),
        ("src/app.py", "source"),
        (None, "unknown"),
    ],
    ids=["fixture", "test-dir", "test-suffix", "spec", "docs-dir", "markdown", "source", "none"],
)
def test_path_classes(path, expected):
    assert _load_triage().path_class(path) == expected


def test_helpers_are_executable_in_the_git_index():
    """A release copies the skill's bytes and a development compose links them,
    and either way the helper runs at the mode git recorded."""
    r = subprocess.run(
        ["git", "ls-files", "-s", "--", "library/skills/gitguardian"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:  # not a git checkout (sdist): the on-disk check below applies
        pytest.skip("not a git checkout")
    modes = {}
    for line in r.stdout.splitlines():
        meta, _, tracked = line.partition("\t")
        modes[tracked] = meta.split()[0]
    for name in ("gitguardian_api.sh", "test.sh"):
        assert modes.get(f"library/skills/gitguardian/{name}") == "100755", modes


@pytest.mark.parametrize("script", [SCRIPT, SUITE], ids=lambda p: p.name)
def test_helper_executable_on_disk(script):
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_the_env_contract_is_the_key_the_helper_reads():
    """The integration declares the token, secret and at the bot tier, so no
    other bot's environment holds it. The helper also reads GITGUARDIAN_API_URL,
    which stays undeclared: a contract cannot mark a name optional yet, and a
    declared name left unset warns on every validate."""
    meta, _ = parse_frontmatter(INTEGRATION.read_text())
    contract = meta["env_contract"]
    assert set(contract) == {"GITGUARDIAN_API_KEY"}
    assert contract["GITGUARDIAN_API_KEY"]["secret"] is True
    assert contract["GITGUARDIAN_API_KEY"]["default_tier"] == "bot"
    read = set(re.findall(r"\$\{?(GITGUARDIAN_[A-Z_]+)", SCRIPT.read_text()))
    assert read == {"GITGUARDIAN_API_KEY", "GITGUARDIAN_API_URL"}


@pytest.mark.parametrize(
    "path", [SKILL_DIR / "SKILL.md", INTEGRATION, PROTOCOL], ids=["skill", "integration", "protocol"]
)
def test_frontmatter_parses(path):
    text = path.read_text()
    assert frontmatter_error(text) is None, frontmatter_error(text)


def test_the_integration_requires_the_skill_and_the_protocol():
    meta, _ = parse_frontmatter(INTEGRATION.read_text())
    assert meta["title"] == "GitGuardian"
    assert meta["requires"] == {"skills": ["gitguardian"], "protocols": ["secret-incidents"]}
    skill, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    assert skill["name"] == SKILL_DIR.name
    protocol, _ = parse_frontmatter(PROTOCOL.read_text())
    assert protocol["title"] == "Secret Incidents"


def _skill_grants() -> list[str]:
    meta, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    return meta["tool_grants"]


def test_the_reads_are_granted_and_the_writes_are_not():
    """Each grant names the helper by its path from the bot directory, with no
    variable (tests/test_printify_skill.py says why). The helper refuses the
    write verbs; leaving them ungranted also keeps a bot from running them
    unasked."""
    grants = _skill_grants()
    for grant in grants:
        assert grant.startswith(f"Bash({HELPER} ") and "$" not in grant, grant
    for verb in READ_VERBS:
        for command in (f"{HELPER} {verb}", f"{HELPER} {verb} 101"):
            assert any(_grant_allows(g, command) for g in grants), command
    for verb in WRITE_VERBS:
        for command in (f"{HELPER} {verb}", f"{HELPER} {verb} 101 --reason false_positive"):
            assert not any(_grant_allows(g, command) for g in grants), command


def test_every_call_the_skill_documents_is_a_granted_read():
    grants = _skill_grants()
    calls = [
        line.split(" #")[0].strip()
        for line in (SKILL_DIR / "SKILL.md").read_text().splitlines()
        if line.startswith(f"{HELPER} ")
    ]
    assert len(calls) >= 5, calls
    for call in calls:
        assert call.split()[1] in READ_VERBS, call
        assert any(_grant_allows(g, call) for g in grants), call


def test_fixtures_are_synthetic():
    """The answers the suite replays are made up: they parse, name no host but
    GitGuardian's dashboard and a reserved example one, and hold no hex run a
    commit or a hash could be. test.sh fills in the commit, path and hash."""
    fixtures = sorted((SKILL_DIR / "fixtures").glob("*.json"))
    assert len(fixtures) == 4, fixtures
    for fixture in fixtures:
        raw = fixture.read_text()
        json.loads(raw)
        hosts = set(re.findall(r"https?://([^/\"]+)", raw))
        assert hosts <= {"dashboard.gitguardian.com", "example.invalid"}, (fixture.name, hosts)
        assert not re.search(r"\b[0-9a-f]{16,}\b", raw), fixture.name


# --- Composition from the shipped library -----------------------------------

LINE = "      integrations: [gitguardian]\n"
SECTIONS = ("### GitGuardian", "### Secret Incidents")


def _equip(fleet_dir: Path) -> None:
    """Give the fixture fleet's lead the one line, by the same text surgery as
    tests/test_requires_linking.py."""
    text = (fleet_dir / "fleet.yaml").read_text()
    marker = "    lead:\n"
    assert marker in text, "bot 'lead' not found in fleet.yaml"
    (fleet_dir / "fleet.yaml").write_text(text.replace(marker, marker + LINE, 1))


def _compose_lead(fleet_dir: Path) -> tuple[Path, list[str], str]:
    """Compose the lead from the shipped library and template."""
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = make_paths(fleet_dir)
    compose_bot(fleet.bots["lead"], fleet, paths, log=lambda m: None)
    bot_dir = paths.bot_runtime("lead")
    settings = json.loads((bot_dir / ".claude" / "settings.local.json").read_text())
    return bot_dir, settings["permissions"]["allow"], (bot_dir / "CLAUDE.md").read_text()


def test_one_integrations_line_equips_the_skill_and_the_protocol(fleet_dir):
    (fleet_dir / "templates" / "claude.md.j2").unlink()
    _equip(fleet_dir)

    bot_dir, allow, claude_md = _compose_lead(fleet_dir)

    link = bot_dir / ".claude" / "skills" / "gitguardian"
    assert link.is_symlink() and link.resolve() == SKILL_DIR.resolve()
    assert "Skill(gitguardian)" in allow
    assert f"Bash({HELPER} status *)" in allow
    for heading in SECTIONS:
        assert heading in claude_md, heading

    text = (fleet_dir / "fleet.yaml").read_text()
    (fleet_dir / "fleet.yaml").write_text(text.replace(LINE, "", 1))
    bot_dir, allow, claude_md = _compose_lead(fleet_dir)

    assert not link.exists() and not link.is_symlink()
    assert not [rule for rule in allow if "gitguardian" in rule], allow
    for heading in SECTIONS:
        assert heading not in claude_md, heading
    assert "Skill(fleet-ops)" in allow  # the second compose ran


def test_validate_finds_nothing_wrong_in_the_shipped_files(fleet_dir):
    """No other test runs validate's library-wide checks (frontmatter,
    requires:, env_contract) over the shipped library itself, so run them over
    a bot that declares the integration."""
    _equip(fleet_dir)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    report = validate(fleet, make_paths(fleet_dir))
    named = [e for e in report.errors if "gitguardian" in e or "secret-incidents" in e]
    assert not named, named
