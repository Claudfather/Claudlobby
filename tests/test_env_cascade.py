"""The .env tier cascade — one resolver, and proof both sides read it (#1226).

Three things are gated here, and they are not the same thing:

1. **The rewire preserved behaviour.** ``start-bot.sh`` used to hold the tier
   order inline; it now consumes ``env_tier_present_files``. The order is the
   reference implementation and must not have moved, so the pre-#1226 block is
   transcribed below and run against the new door over every presence
   combination.
2. **The cascade matches the SHELL**, not merely matches itself. The merge is
   asserted against a real ``bash`` sourcing the same files, because "most
   specific wins" is a claim about shell assignment and a Python-only assertion
   would only prove Python agrees with Python.
3. **The instrument can fail.** Each agreement test has a mutation twin that
   breaks the thing under test and requires the check to go red — a check that
   silently stops applying returns its negative verdict, which reads as a pass.
"""

from __future__ import annotations

import itertools
import subprocess
import textwrap
from pathlib import Path

import pytest

from claudlobby.env_tiers import EnvTier, ResolverUnavailable, cascade, read_tiers
from claudlobby.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib" / "lib-common.sh"
RESOLVER = REPO_ROOT / "lib" / "env-tiers.sh"

TIERS = ("host", "root", "fleet", "bot")

#: Revisions of lib/start-bot.sh to scan for the legacy shape. Bounded so the
#: test cannot walk an unbounded history; stated rather than silent, and the
#: failure message names the bound it searched.
_HISTORY_SCAN_CAP = 60

# The pre-#1226 inline block from lib/start-bot.sh, transcribed verbatim.
# `test_the_transcription_is_faithful` pins it against git history so this
# cannot quietly become a copy of the NEW behaviour and agree with itself.
LEGACY_BLOCK = textwrap.dedent(
    """\
    legacy_source_list() {
        [ -f "$HOME/.env" ]                                && printf '%s\\n' "$HOME/.env"
        if [ -n "${CLAUDLOBBY_ROOT:-}" ] && [ -f "$CLAUDLOBBY_ROOT/.env" ]; then
            printf '%s\\n' "$CLAUDLOBBY_ROOT/.env"
        fi
        if [ -n "${FLEET_NAME:-}" ] && [ -n "${CLAUDLOBBY_ROOT:-}" ]; then
            _sb_fleet_dir=$(resolve_fleet_dir "$FLEET_NAME") || _sb_fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET_NAME"
            local_fleet_env="$_sb_fleet_dir/.env"
            [ -f "$local_fleet_env" ] && printf '%s\\n' "$local_fleet_env"
        fi
        [ -f "$BOT_DIR/.env" ]                             && printf '%s\\n' "$BOT_DIR/.env"
        return 0
    }
    """
)

FLEET_YAML = "fleet:\n  name: {name}\n  bots:\n    solo:\n      expertise: [x]\n"


def _bash(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {"PATH": "/usr/bin:/bin", **env}
    return subprocess.run(
        ["bash", "-c", f'set +e; source "{LIB}" >/dev/null 2>&1; set +e; {snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env=full,
    )


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    """A root with one fleet and one bot, no .env files anywhere yet."""
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()
    fleet = tmp_path / "local" / "acme"
    (fleet / "runtime" / "bots" / "solo").mkdir(parents=True)
    (fleet / "fleet.yaml").write_text(FLEET_YAML.format(name="acme"))
    return tmp_path


def _tier_paths(root: Path) -> dict[str, Path]:
    return {
        "host": root / "fakehome" / ".env",
        "root": root / ".env",
        "fleet": root / "local" / "acme" / ".env",
        "bot": root / "local" / "acme" / "runtime" / "bots" / "solo" / ".env",
    }


def _env_for(root: Path) -> dict[str, str]:
    (root / "fakehome").mkdir(exist_ok=True)
    return {
        "HOME": str(root / "fakehome"),
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_NAME": "acme",
        "BOT_DIR": str(root / "local" / "acme" / "runtime" / "bots" / "solo"),
    }


# --------------------------------------------------------------------------
# 1. The rewire preserved the runtime's order
# --------------------------------------------------------------------------


@pytest.mark.parametrize("present", list(itertools.product([False, True], repeat=4)))
def test_new_door_matches_the_pre_rewire_block(estate: Path, present: tuple) -> None:
    """Every presence combination produces the identical source list.

    Sixteen cases rather than one: the tiers are guarded by four different
    conditions in the old block, so a single all-present fixture would exercise
    exactly one path through it and pass on a rewire that dropped a guard.
    """
    paths = _tier_paths(estate)
    env = _env_for(estate)
    for tier, is_present in zip(TIERS, present):
        if is_present:
            paths[tier].parent.mkdir(parents=True, exist_ok=True)
            paths[tier].write_text(f"export FROM_{tier.upper()}=1\n")

    legacy = _bash(LEGACY_BLOCK + "legacy_source_list", env)
    new = _bash('env_tier_present_files "$BOT_DIR" "$FLEET_NAME"', env)

    assert legacy.stdout == new.stdout, (
        f"presence={dict(zip(TIERS, present))}\n"
        f"legacy:\n{legacy.stdout}\nnew:\n{new.stdout}"
    )
    # Not vacuous: with anything present there must BE a source list.
    if any(present):
        assert new.stdout.strip(), "agreement on two empty outputs proves nothing"


def test_the_agreement_check_can_fail(estate: Path) -> None:
    """Positive control for the test above.

    Reverse the cascade and require disagreement. Without this, an
    ``env_tier_present_files`` that silently emitted nothing would agree with a
    legacy block that also emitted nothing, and sixteen green cases would mean
    the door was never exercised.
    """
    paths = _tier_paths(estate)
    env = _env_for(estate)
    for tier in TIERS:
        paths[tier].parent.mkdir(parents=True, exist_ok=True)
        paths[tier].write_text(f"export FROM_{tier.upper()}=1\n")

    legacy = _bash(LEGACY_BLOCK + "legacy_source_list", env)
    mutated = _bash(
        'env_tier_present_files "$BOT_DIR" "$FLEET_NAME" | tac', env
    )
    assert legacy.stdout != mutated.stdout
    assert len(legacy.stdout.strip().splitlines()) == 4


def test_the_transcription_is_faithful() -> None:
    """The legacy block above is a shape that actually shipped.

    Pinned against git rather than trusted, because a transcription that
    drifted toward the new behaviour would make the agreement test compare the
    new door with itself — green, and evidence of nothing.

    Asserts that SOME revision of ``lib/start-bot.sh`` carried all four legacy
    lines together, rather than pinning one SHA. A single-SHA pin is only
    correct until this change lands (after which HEAD holds the new block) and
    ``git log -S`` returns the commit that INTRODUCED a string, which for these
    lines predates the nested-fleet rewrite that gave the block its final shape.
    """
    log = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%H", "-n", str(_HISTORY_SCAN_CAP),
         "--", "lib/start-bot.sh"],
        capture_output=True, text=True, timeout=120,
    )
    if log.returncode != 0 or not log.stdout.strip():
        pytest.skip("git history unavailable (shallow clone or exported tree)")
    shas = log.stdout.split()
    wanted = (
        '[ -f "$HOME/.env" ]',
        'if [ -n "${CLAUDLOBBY_ROOT:-}" ] && [ -f "$CLAUDLOBBY_ROOT/.env" ]; then',
        '_sb_fleet_dir=$(resolve_fleet_dir "$FLEET_NAME") || _sb_fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET_NAME"',
        '[ -f "$BOT_DIR/.env" ]',
    )
    for line in wanted:
        assert line in LEGACY_BLOCK, f"transcription lost: {line}"
    for sha in shas:
        blob = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{sha}:lib/start-bot.sh"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        if all(line in blob for line in wanted):
            return
    pytest.fail(
        f"no revision among the last {len(shas)} touching lib/start-bot.sh carried "
        f"the transcribed legacy block — the transcription has drifted"
    )


# --------------------------------------------------------------------------
# 2. The cascade matches the shell
# --------------------------------------------------------------------------


def _shell_resolved(estate: Path, var: str) -> str:
    """What a real shell resolves *var* to after sourcing the present tiers."""
    env = _env_for(estate)
    out = _bash(
        'while IFS= read -r f; do . "$f"; done '
        f'< <(env_tier_present_files "$BOT_DIR" "$FLEET_NAME"); printf %s "${{{var}-<UNSET>}}"',
        env,
    )
    return out.stdout


def _python_resolved(estate: Path, var: str):
    rows = _bash('env_tier_rows "$BOT_DIR" "$FLEET_NAME"', _env_for(estate)).stdout
    tiers = [
        EnvTier(t, Path(p) if p else None, s)
        for t, p, s in (ln.split("\t") for ln in rows.splitlines() if ln.strip())
    ]
    return cascade(tiers).get(var)


def _write(estate: Path, **by_tier: str) -> None:
    paths = _tier_paths(estate)
    for tier, line in by_tier.items():
        paths[tier].parent.mkdir(parents=True, exist_ok=True)
        paths[tier].write_text(line)


def test_most_specific_assignment_wins(estate: Path) -> None:
    _write(
        estate,
        host="export TOK=from_host\n",
        root="export TOK=from_root\n",
        fleet="export TOK=from_fleet\n",
        bot="export TOK=from_bot\n",
    )
    res = _python_resolved(estate, "TOK")
    assert res.value == "from_bot"
    assert res.tier == "bot"
    assert res.assigned_by == ("host", "root", "fleet", "bot")
    assert res.shadowed == ("host", "root", "fleet")
    assert _shell_resolved(estate, "TOK") == "from_bot"


def test_an_empty_assignment_beats_a_real_value_upstream(estate: Path) -> None:
    """The live #1213 case: a tier holding "" is a WIN, not a miss.

    This is the question the cascade had to answer — an empty string is neither
    "present" nor "absent" in the way a naive check means those words. Sourcing
    is assignment, so the empty one wins and the upstream secret is gone. On
    this estate that is why GITHUB_PAT resolves to "" on two of four fleets
    while a real 40-char token sits at the same tier on the other two.
    """
    _write(estate, root="export GITHUB_PAT=realtoken40chars\n", fleet="export GITHUB_PAT=\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.value == ""
    assert res.empty is True
    assert res.tier == "fleet"
    assert res.shadowed == ("root",)
    # And the shell agrees — the whole point. Set-but-empty, not unset.
    assert _shell_resolved(estate, "GITHUB_PAT") == ""


def test_a_value_downstream_of_an_empty_still_wins(estate: Path) -> None:
    """The mirror case, so 'empty wins' is not read as 'empty poisons'."""
    _write(estate, root="export GITHUB_PAT=\n", fleet="export GITHUB_PAT=realtoken\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.value == "realtoken"
    assert res.tier == "fleet"
    assert _shell_resolved(estate, "GITHUB_PAT") == "realtoken"


def test_an_unmentioned_var_is_absent_not_empty(estate: Path) -> None:
    """Absent and empty must stay distinguishable — different remedies."""
    _write(estate, fleet="export OTHER=1\n")
    assert _python_resolved(estate, "GITHUB_PAT") is None
    assert _shell_resolved(estate, "GITHUB_PAT") == "<UNSET>"


# --------------------------------------------------------------------------
# 3. Row semantics, isolation, and refusal
# --------------------------------------------------------------------------


def test_all_four_tiers_are_reported_even_when_absent(estate: Path) -> None:
    rows = _bash('env_tier_rows "$BOT_DIR" "$FLEET_NAME"', _env_for(estate)).stdout
    got = [ln.split("\t") for ln in rows.splitlines()]
    assert [r[0] for r in got] == list(TIERS)
    assert all(r[2] == "absent" for r in got), got


def test_unresolved_is_not_absent(estate: Path) -> None:
    """A tier that does not APPLY is a third state, not a missing file.

    Collapsing them would make a root-mode run report a fleet .env that could
    never exist as merely 'not created yet', which sends someone to create it.
    """
    env = {**_env_for(estate), "FLEET_NAME": "", "BOT_DIR": ""}
    rows = _bash('env_tier_rows "" ""', env).stdout
    states = dict((r[0], r[2]) for r in (ln.split("\t") for ln in rows.splitlines()))
    assert states["fleet"] == "unresolved"
    assert states["bot"] == "unresolved"


def test_ambient_env_cannot_redirect_the_answer(estate: Path, monkeypatch) -> None:
    """A caller's own session must not steer the resolver.

    Every var this resolver reads is one a live bot session exports. An
    inherited environment let a previous investigation's assertion read a
    different ledger than the one under test, and it came back clean on every
    arm at once.
    """
    (estate / "local" / "acme" / ".env").write_text("export TOK=correct\n")
    decoy = estate / "decoy"
    (decoy / "runtime" / "bots" / "solo").mkdir(parents=True)
    (decoy / "runtime" / "bots" / "solo" / ".env").write_text("export TOK=wrong\n")
    monkeypatch.setenv("BOT_DIR", str(decoy / "runtime" / "bots" / "solo"))
    monkeypatch.setenv("FLEET_NAME", "not-acme")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(decoy))

    paths = Paths(root=estate, fleet_dir=estate / "local" / "acme")
    (estate / "lib" / "env-tiers.sh").write_bytes(RESOLVER.read_bytes())
    (estate / "lib" / "lib-common.sh").write_bytes(LIB.read_bytes())
    tiers = read_tiers(paths, bot_name="solo")
    by_tier = {t.tier: t for t in tiers}
    assert by_tier["fleet"].path == estate / "local" / "acme" / ".env"
    assert by_tier["fleet"].state == "present"
    assert decoy not in by_tier["bot"].path.parents


def test_a_missing_resolver_raises_rather_than_guessing(estate: Path) -> None:
    """No fallback ordering. A fallback IS the second copy, consulted exactly
    when the two are most likely to have diverged."""
    paths = Paths(root=estate, fleet_dir=estate / "local" / "acme")
    with pytest.raises(ResolverUnavailable, match="will not substitute"):
        read_tiers(paths, bot_name="solo")


# --------------------------------------------------------------------------
# 4. The tier NAME registry, and what it is allowed to mean (#1226 stage 2)
# --------------------------------------------------------------------------


def test_the_tier_registry_matches_the_resolver(estate: Path) -> None:
    """``ENV_TIERS`` names exactly the tiers the runtime emits, in its order.

    Without this pin ``ENV_TIERS`` is a second copy of the cascade wearing a
    different hat — the precise failure the resolver exists to end. It is a
    NAME registry for validating a declaration and nothing more; the order and
    the paths stay in bash.
    """
    from claudlobby.known_values import ENV_TIERS

    rows = _bash('env_tier_rows "$BOT_DIR" "$FLEET_NAME"', _env_for(estate)).stdout
    emitted = tuple(ln.split("\t")[0] for ln in rows.splitlines() if ln.strip())
    assert emitted == ENV_TIERS


def test_deprecated_tiers_are_a_subset_of_the_registry() -> None:
    """A deprecation that names a tier the registry does not have is a typo
    nothing would catch — the warning branch simply never fires."""
    from claudlobby.known_values import DEPRECATED_ENV_TIERS, ENV_TIERS

    assert DEPRECATED_ENV_TIERS <= set(ENV_TIERS)
    assert DEPRECATED_ENV_TIERS, "an empty set would make the warn branch dead"


@pytest.mark.parametrize("tier", ["host", "root", "fleet", "bot"])
def test_every_cascade_tier_is_declarable(tier: str) -> None:
    """All four, not two. ``host`` was the tier the contract could not express
    at all, despite being FIRST in the runtime chain."""
    from claudlobby.validator import _env_contract_errors

    errs = _env_contract_errors({"TOK": {"secret": True, "default_tier": tier}}, "f")
    hard = [e for e in errs if "unknown default_tier" in e]
    assert not hard, errs


def test_an_unknown_tier_is_rejected() -> None:
    """It used to be accepted silently and bucketed as fleet.

    A typo'd tier is invisible at runtime — the var still resolves, from
    wherever a value happens to sit — so it must fail at declaration time or
    never.
    """
    from claudlobby.validator import _env_contract_errors

    errs = _env_contract_errors({"TOK": {"secret": True, "default_tier": "flete"}}, "f")
    assert any("unknown default_tier" in e for e in errs), errs
    assert any("fleet" in e for e in errs), "should suggest the near miss"


def test_the_deprecated_root_tier_is_reported_not_silently_accepted() -> None:
    from claudlobby.validator import _env_contract_errors

    errs = _env_contract_errors({"TOK": {"secret": True, "default_tier": "root"}}, "f")
    assert any("wound down" in e for e in errs), errs


def test_the_missing_var_warning_no_longer_names_one_tier_as_the_location() -> None:
    """The message is half the defect.

    "add to fleet-tier .env" sends an operator to one file for a var that would
    resolve from any of four, and is why a value already sitting at the host or
    bot tier still read as missing.
    """
    import claudlobby.validator as v

    src = Path(v.__file__).read_text()
    assert "add to {req.default_tier}-tier .env" not in src
    assert "add it at any tier" in src


# --------------------------------------------------------------------------
# 5. Shadowing must be visible, not merely survivable
# --------------------------------------------------------------------------


def test_an_empty_win_over_a_real_value_names_what_it_blanked(estate: Path) -> None:
    """The row worth interrupting someone over.

    A presence-only check cannot see this state: the key IS set, so nothing
    reports it missing, and a value DOES exist, so nothing reports it
    unconfigured — yet the runtime hands the integration "". It is what this
    estate enters the moment a host-tier PAT appears under the fleet .env's
    pristine composer stub.
    """
    _write(estate, host="export GITHUB_PAT=realhostvalue\n", fleet="export GITHUB_PAT=\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.empty is True
    assert res.tier == "fleet"
    assert res.blanked_upstream == ("host",)
    assert _shell_resolved(estate, "GITHUB_PAT") == ""


def test_blanked_upstream_is_empty_when_nothing_was_lost(estate: Path) -> None:
    """Positive control's twin: the property must not fire on every empty.

    An empty var that was never set upstream is an unconfigured var — ordinary,
    and a remedy of "add the secret". Reporting it as blanking would bury the
    real rows in noise, which is how a true signal gets suppressed.
    """
    _write(estate, fleet="export GITHUB_PAT=\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.empty is True
    assert res.blanked_upstream == ()


def test_a_real_value_downstream_reports_no_blanking(estate: Path) -> None:
    _write(estate, host="export GITHUB_PAT=\n", fleet="export GITHUB_PAT=real\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.blanked_upstream == ()
    assert res.shadowed == ("host",)


def test_assignments_never_carry_the_secret_itself(estate: Path) -> None:
    """The provenance trail is (tier, has_a_value) booleans by construction.

    This record is printed by the register and passed around; a value that
    never enters the structure cannot leave it. Presence is the whole question.
    """
    _write(estate, host="export GITHUB_PAT=supersecret\n", fleet="export GITHUB_PAT=\n")
    res = _python_resolved(estate, "GITHUB_PAT")
    assert res.assignments == (("host", True), ("fleet", False))
    assert "supersecret" not in repr(res.assignments)


# --------------------------------------------------------------------------
# 6. Per-scope credential source override (#1214 F6c)
# --------------------------------------------------------------------------


def test_a_bot_override_beats_the_fleet_default() -> None:
    """"(a) should work if configured at bot, fleet, or host level."

    Merged fleet-then-bot with the bot winning — the same direction the .env
    cascade runs and the same merge git_credentials already uses, so a reader
    who learns one has learned all three.
    """
    from claudlobby.config import _parse_credential_sources

    merged = {
        **_parse_credential_sources({"GITHUB_PAT": "cli:gh-token"}, where="fleet"),
        **_parse_credential_sources({"GITHUB_PAT": "literal"}, where="bot"),
    }
    assert merged == {"GITHUB_PAT": "literal"}


def test_an_override_key_must_be_an_env_var_name() -> None:
    from claudlobby.config import _parse_credential_sources

    with pytest.raises(ValueError, match="env var NAME"):
        _parse_credential_sources({"not-a-var": "literal"}, where="t")


def test_an_override_source_is_held_to_the_same_closed_registry() -> None:
    """A laxer door into the same resolver voids the injection guarantee for
    both doors, not just the new one."""
    from claudlobby.known_values import KNOWN_CREDENTIAL_SOURCES
    import claudlobby.validator as v

    src = Path(v.__file__).read_text()
    assert "credential_sources['{var_name}']" in src
    assert "KNOWN_CREDENTIAL_SOURCES" in src
    # And the registry it is checked against is the one the contract uses.
    assert "cli:gh-token" in KNOWN_CREDENTIAL_SOURCES


def test_set_but_empty_and_absent_get_different_remedies() -> None:
    """Two states a value-based check collapses, with opposite fixes.

    Telling an operator to ADD a var that is already assigned-empty sends them
    to write it at the very tier whose empty assignment is blanking it.
    """
    import claudlobby.validator as v

    src = Path(v.__file__).read_text()
    assert "SET BUT EMPTY" in src
    assert "adding it again at the same tier changes nothing" in src
    assert "no .env tier sets it" in src


def test_the_legacy_tier_key_fails_loudly_rather_than_defaulting() -> None:
    """A renamed schema key silently defaults every unmigrated declaration.

    A fleet overlay carrying the old `tier:` would fall back to 'fleet' and
    look fine — and unlike most schema drift this one is invisible at runtime,
    because a mis-tiered var still resolves from wherever a value sits.
    """
    from claudlobby.validator import _env_contract_errors

    errs = _env_contract_errors({"TOK": {"secret": True, "tier": "bot"}}, "f")
    assert any("renamed to 'default_tier'" in e for e in errs), errs


def test_one_bad_var_does_not_smear_across_the_others() -> None:
    """The per-var label must not accumulate.

    It did: the loop rebound the file-level label, so the eighth error on a
    file read "var 'A' var 'B' ... var 'H'" and pointed the reader at seven
    vars that were fine. A message naming the wrong location is worse than a
    terse one.
    """
    from claudlobby.validator import _env_contract_errors

    errs = _env_contract_errors({"A": {}, "B": {}, "C": {}}, "integration 'x.md'")
    assert len(errs) == 3
    for var, err in zip(("A", "B", "C"), errs):
        assert err.startswith(f"integration 'x.md' var '{var}':"), err
        assert err.count("var '") == 1, err
