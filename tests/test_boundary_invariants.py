"""Boundary invariants frozen as CI gates (boundary phase L4, deliverable 3).

Turns the boundary spec's "never" clauses into machine checks so wave-2/3 drift
is caught where it lands, not in the next visioning pass. Each invariant ships
with a deliberate-violation proof (a checker fed a synthetic offender) so the
gate can never be a green rubber stamp:

  1. no *runtime* module outside ``paths.py`` imports ``claudron.*`` (the single
     sanctioned import seam; tests are exempt — this file itself imports nothing
     from claudron);
  2. no code path opens files under a *resolved* vault's note tiers — knowledge
     is consumed through the ``claudron`` CLI door only (Python + bash);
  3. ``validator.py`` carries no ``claudron`` MCP assertion (guards L1's fix —
     the MCP fragment is a parked, unbuilt surface, decision C);
  4. composed vault-wired bot env names only ``CLAUDRON_VAULT_PATH`` — never a
     deprecated alias;
  5. no composed settings file grants the ``Bash(claudron *)`` wildcard (guards
     L2's narrow-verb-grant rule).

Docstring-truth (deliverable 4) lives with the compat table it describes, in
``tests/test_claudron_compat.py``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from claudlobby.composer import compose_bot_conf, compose_settings_local
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "claudlobby"
LIB = REPO_ROOT / "lib"

# The one module allowed to import claudron — the sanctioned [vault] import seam.
CLAUDRON_IMPORT_SEAM = "paths.py"

# ── Vault note tiers Claudron owns (VAULT-STRUCTURE.md §Directory contract) ──
# Single source of truth for the tier segment set — the Python checker reads
# these, never a hardcoded inline list. `<fleet>/shared` and the coming
# `<system>/shared` (Claudlobby #602/#609 nesting) are *both* matched by the
# trailing-`shared` alternative, so the future system tier needs no re-list.
# The bash checker is stricter still — it flags any descent under the resolved
# vault root, which subsumes every tier, present or future, by construction.
# TODO(#602): once nesting lands, assert this set against Claudron's manifest.
VAULT_NOTE_TIERS = ("_shared", "projects", "shared")


# ---------------------------------------------------------------------------
# helpers shared by the behavioral invariants (4, 5)
# ---------------------------------------------------------------------------


def _paths(tmp_path: Path) -> Paths:
    root = tmp_path / "claudlobby"
    (root / "runtime" / "bots").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(exist_ok=True)
    return Paths(root=root, fleet_dir=root)


def _fleet(bot: BotConfig) -> FleetConfig:
    return FleetConfig(name="t", service_prefix="p", bots={bot.bot_id: bot})


# ===========================================================================
# Invariant 1 — only paths.py imports claudron.*
# ===========================================================================


def claudron_imports(source: str) -> list[str]:
    """Absolute ``claudron[.x]`` imports in *source* (relative imports excluded —
    ``from .paths import …`` is claudlobby-internal, never the engine)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [
                a.name
                for a in node.names
                if a.name == "claudron" or a.name.startswith("claudron.")
            ]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mod = node.module or ""
            if mod == "claudron" or mod.startswith("claudron."):
                found.append(mod)
    return found


class TestModuleImportSeam:
    def test_only_paths_imports_claudron(self):
        offenders = {
            py.relative_to(REPO_ROOT).as_posix(): imps
            for py in sorted(PKG.rglob("*.py"))
            if (imps := claudron_imports(py.read_text())) and py.name != CLAUDRON_IMPORT_SEAM
        }
        assert not offenders, (
            "claudron.* imported outside the paths.py seam — route through "
            f"paths.detect_vault / paths.vault_api_available instead: {offenders}"
        )

    def test_the_seam_actually_imports_claudron(self):
        # Positive control: if paths.py stopped importing claudron the invariant
        # above would pass vacuously, so pin that the seam is really the seam.
        assert claudron_imports((PKG / CLAUDRON_IMPORT_SEAM).read_text())

    def test_import_checker_fires_on_a_violation(self):
        assert claudron_imports("import claudron.hooks")
        assert claudron_imports("from claudron.vault import detect")
        # …and does not false-positive on the legal shapes:
        assert not claudron_imports("from .paths import detect_vault")
        assert not claudron_imports("import claudlobby.config")


# ===========================================================================
# Invariant 2 — no code opens files under a resolved vault's note tiers
# ===========================================================================

# Python: an open-like call whose path expression names BOTH a vault address and
# a note tier — i.e. it opens a tier *under a resolved vault*, the exact shape
# the boundary forbids (address-resolve + CLI door only).
_OPEN_CALLS = frozenset(
    {
        "open", "read_text", "write_text", "read_bytes", "write_bytes",
        "glob", "rglob", "iterdir", "scandir", "listdir", "walk",
    }
)
_VAULT_ADDR_RE = re.compile(r"vault", re.I)
_TIER_RE = re.compile(r"['\"/](?:" + "|".join(VAULT_NOTE_TIERS) + r")(?:['\"/]|\b)")

# Bash: any descent under the contract-resolved vault address. The `claudron`
# CLI reads the vault from this env itself; a script constructing a path *into*
# it has reached past the door. Comment lines are stripped first.
_VAULT_DESCENT_RE = re.compile(r"\$\{?CLAUDRON_VAULT_PATH\b(?:[:#%!^,][^}]*)?\}?/")


def python_vault_tier_opens(source: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name not in _OPEN_CALLS:
            continue
        text = ast.unparse(node)
        if _VAULT_ADDR_RE.search(text) and _TIER_RE.search(text):
            hits.append((getattr(node, "lineno", -1), text))
    return hits


def bash_vault_descents(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        code = raw.split("#", 1)[0]  # drop trailing comments (rough but safe here)
        if _VAULT_DESCENT_RE.search(code):
            hits.append((i, raw.strip()))
    return hits


class TestNoVaultTierAccess:
    def test_python_opens_no_vault_note_tier(self):
        offenders = {
            py.relative_to(REPO_ROOT).as_posix(): python_vault_tier_opens(py.read_text())
            for py in sorted(PKG.rglob("*.py"))
        }
        offenders = {k: v for k, v in offenders.items() if v}
        assert not offenders, (
            "claudlobby opens files under a resolved vault's note tiers — consume "
            f"knowledge through the claudron CLI, never the filesystem: {offenders}"
        )

    def test_bash_opens_no_resolved_vault_path(self):
        # freshbox-boot-gate.sh writes to a FABRICATED fixture vault ($VAULT it
        # created), never to the contract-resolved $CLAUDRON_VAULT_PATH — so it is
        # correctly not flagged here (it is a test harness, like tests/ in Python).
        offenders = {
            sh.relative_to(REPO_ROOT).as_posix(): bash_vault_descents(sh.read_text())
            for sh in sorted(LIB.rglob("*.sh"))
        }
        offenders = {k: v for k, v in offenders.items() if v}
        assert not offenders, (
            "a lib script descends into $CLAUDRON_VAULT_PATH — resolve the address "
            f"and shell `claudron`; never open under the vault: {offenders}"
        )

    def test_python_checker_fires_on_a_violation(self):
        assert python_vault_tier_opens(
            "def f(v):\n    return (v.vault_root / '_shared' / 'n.md').read_text()\n"
        )
        assert python_vault_tier_opens(
            "import os\ndef f(vault_root):\n    open(os.path.join(vault_root, 'projects', 'p.md'))\n"
        )
        # …and stays quiet on a non-vault open and a non-open vault reference:
        assert not python_vault_tier_opens("open('/etc/hostname').read()")
        assert not python_vault_tier_opens("p = vault_root / 'index.json'")

    def test_bash_checker_fires_on_a_violation(self):
        assert bash_vault_descents('cat "$CLAUDRON_VAULT_PATH/_shared/CONVENTIONS.md"')
        assert bash_vault_descents('echo x > "${CLAUDRON_VAULT_PATH}/projects/p/n.md"')
        # …the sanctioned wedge shapes (dir test, CLI arg) are NOT descents:
        assert not bash_vault_descents('[ -d "${CLAUDRON_VAULT_PATH:-}" ] || return 0')
        assert not bash_vault_descents('claudron lookup --json "$CLAUDRON_VAULT_PATH"')
        assert not bash_vault_descents('# never open $CLAUDRON_VAULT_PATH/_shared/x')


# ===========================================================================
# Invariant 3 — validator.py carries no claudron MCP assertion
# ===========================================================================

# The claudron MCP fragment (library/mcp/claudron.json → mcp__claudron__*) is a
# PARKED, unbuilt surface (claudron_compat decision C). L1 removed the validator
# assertion that warned on its absence; this freezes that removal. Targets the
# claudron-MCP tokens specifically — validator's generic MCP validation is fine.
_CLAUDRON_MCP_RE = re.compile(r"mcp__claudron|claudron\.json|mcp/claudron")


class TestValidatorNoClaudronMcp:
    def test_validator_source_has_no_claudron_mcp(self):
        src = (PKG / "validator.py").read_text()
        hits = [
            (i, ln.strip())
            for i, ln in enumerate(src.splitlines(), 1)
            if _CLAUDRON_MCP_RE.search(ln)
        ]
        assert not hits, (
            "validator.py asserts on the parked claudron MCP surface (regressed "
            f"L1): {hits}"
        )

    def test_claudron_mcp_checker_fires(self):
        assert _CLAUDRON_MCP_RE.search('tools.append("mcp__claudron__lookup")')
        assert _CLAUDRON_MCP_RE.search('frag = paths.find("mcp", "claudron.json")')
        # generic MCP validation (what validator legitimately does) is untouched:
        assert not _CLAUDRON_MCP_RE.search('avail_mcp = _available_names(paths, "mcp")')


# ===========================================================================
# Invariant 4 — composed vault-wired env names only CLAUDRON_VAULT_PATH
# ===========================================================================

DEPRECATED_VAULT_ALIAS = "CLAUDRON_VAULT="  # the pre-_PATH spelling (v0.2.0 read it)


def has_deprecated_vault_alias(text: str) -> bool:
    # `CLAUDRON_VAULT_PATH=` contains `_PATH` between VAULT and =, so the bare
    # `CLAUDRON_VAULT=` substring matches only the deprecated alias.
    return DEPRECATED_VAULT_ALIAS in text


class TestVaultEnvName:
    def test_bot_conf_names_only_canonical_vault_path(self, tmp_path):
        bot = BotConfig(
            bot_id="b1", name="b1", expertise=["eng"], claudron_vault_path="/srv/v"
        )
        conf = compose_bot_conf(bot, _fleet(bot), _paths(tmp_path))
        assert "CLAUDRON_VAULT_PATH=" in conf
        assert not has_deprecated_vault_alias(conf)

    def test_composer_source_emits_no_deprecated_alias(self):
        assert not has_deprecated_vault_alias((PKG / "composer.py").read_text())

    def test_alias_checker_fires(self):
        assert has_deprecated_vault_alias("export CLAUDRON_VAULT=/srv/v")
        assert not has_deprecated_vault_alias("export CLAUDRON_VAULT_PATH=/srv/v")


# ===========================================================================
# Invariant 5 — no composed settings grants Bash(claudron *)
# ===========================================================================

WILDCARD_GRANT = "Bash(claudron *)"


class TestNoWildcardGrant:
    def test_no_wildcard_in_any_composed_settings(self, tmp_path):
        for kw in (
            {"claudron_vault_path": "/srv/v"},  # loop on
            {"claudron_vault_path": "/srv/v", "claudron_session_loop": False},  # off
            {},  # no vault
        ):
            bot = BotConfig(bot_id="b1", name="b1", expertise=["eng"], **kw)
            settings = compose_settings_local(bot, _fleet(bot), _paths(tmp_path))
            assert WILDCARD_GRANT not in json.dumps(settings)

    def test_wildcard_checker_fires(self):
        assert WILDCARD_GRANT in json.dumps({"permissions": {"allow": [WILDCARD_GRANT]}})
