"""R7 — guard-the-guard tripwire (#702).

The L1 source guard only protects the source values it actually sees: the
BotConfig dataclass walk, the MCP fragments passed to ``audit_bot_sources``, the
finalized grants, resolved tool params, and timer scripts. A NEW raw parse/read
of a fleet-controlled file (an ``open()`` + ``.read()``, a ``json.load(s)`` /
``yaml.safe_load``, a ``.read_text()``) is a new source surface that could
smuggle an unguarded absolute path past the guard.

This test AST-scans every file-read call in composer.py + config.py and fails
when the set changes — forcing a conscious decision: route the new source
through ``path_audit.audit_bot_sources`` (or the grant/fragment choke), or record
it here as a documented exempt (a tool ``.j2`` body, a prose charter, or
pre-existing runtime state — none of which is a fleet.yaml-shaped source).
"""

from __future__ import annotations

import ast
from pathlib import Path

import claudlobby
from claudlobby.path_audit import denied_source_paths

_PKG = Path(claudlobby.__file__).parent

# What counts as a file read: bare ``open()``; a file-like ``.read`` /
# ``.read_text`` / ``.read_bytes`` / ``.readlines``; or a ``json`` / ``yaml`` /
# ``tomllib`` / ``pickle`` ``.load`` / ``.loads`` / ``.safe_load`` parse. The scan
# is AST-based, not a text regex, so a new read cannot slip past on spelling — a
# plain ``open() + .read()`` or ``json.load(fh)`` (no trailing ``s``) registers
# just like ``json.loads`` / ``.read_text()`` did (#731; the old regex missed both).
_READ_METHODS = {"read", "read_text", "read_bytes", "readlines"}
_PARSE_METHODS = {"load", "loads", "safe_load"}
_PARSE_BASES = {"json", "yaml", "tomllib", "pickle"}


def _is_read_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "open"
    if isinstance(func, ast.Attribute):
        if func.attr in _READ_METHODS:
            return True
        if func.attr in _PARSE_METHODS and isinstance(func.value, ast.Name):
            return func.value.id in _PARSE_BASES
    return False


def _scan(module: str) -> set[tuple[str, str]]:
    """Every file-read call in *module*, keyed by its whitespace-normalized source
    expression (so formatting churn doesn't trip the guard)."""
    tree = ast.parse((_PKG / module).read_text())
    return {
        (module, "".join(ast.unparse(node).split()))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_read_call(node)
    }


# Every file-read site is either WIRED to the L1 guard or a documented EXEMPT.
# When this set changes, wire the new site or justify the exemption.
_BLESSED_RAW_READS = {
    # config.py — fleet.yaml / system.yaml parse. The parsed BotConfig feeds
    # audit_bot_sources (the dataclass walk); system.yaml is asserted L1-clean below.
    ("config.py", "yaml.safe_load(f)"),
    # composer.py — MCP fragment loads feed audit_bot_sources(fragments=...);
    # grant/integration frontmatter reads feed the grant choke; template / prose /
    # runtime-state reads are exempt-as-code (tool .j2 → #703) or non-source.
    ("composer.py", "frag_path.read_text()"),
    ("composer.py", "json.loads(frag_path.read_text())"),
    ("composer.py", "template_path.read_text()"),
    ("composer.py", "charter.read_text()"),
    ("composer.py", "access_path.read_text()"),
    ("composer.py", "json.loads(access_path.read_text())"),
    ("composer.py", "int_path.read_text()"),
    ("composer.py", "env_path.read_text()"),
    ("composer.py", "dotenv.read(env_path)"),
    # _upstream_env_names walks $HOME/.env, the repo-root .env and the fleet
    # .env, because start-bot.sh sources all three above the bot tier. EXEMPT,
    # narrowly: only the KEYS and the emptiness of each value are consumed, to
    # decide whether a bot-tier stub must be commented rather than emitted live.
    # No value crosses into a path, a grant, or composed output, so this adds no
    # fleet.yaml-shaped source surface for L1 to miss.
    ("composer.py", "dotenv.read(tier)"),
    # _fleet_bot_count reads a SIBLING fleet's manifest to size its block of the
    # host-global boot ladder (#1002). EXEMPT, narrowly, on the dotenv.read(tier)
    # precedent: the only thing consumed is len(fleet.bots) — an integer. No
    # value from the parsed document crosses into a path, a grant, or composed
    # output, so this adds no fleet.yaml-shaped source surface for L1 to miss.
    # It is deliberately raw and fail-soft rather than routed through the
    # validating loader: a sibling fleet with a broken manifest must not be able
    # to fail this fleet's generate.
    ("composer.py", "fleet_yaml.read_text(encoding='utf-8')"),
    ("composer.py", "yaml.safe_load(fleet_yaml.read_text(encoding='utf-8'))"),
}


def test_no_unguarded_raw_source_reads():
    found = _scan("config.py") | _scan("composer.py")
    new = found - _BLESSED_RAW_READS
    gone = _BLESSED_RAW_READS - found
    assert not new, (
        "New raw parse/read site(s) — a new fleet-source surface. Route each "
        "through path_audit.audit_bot_sources (or the grant/fragment choke), or "
        f"add to _BLESSED_RAW_READS with justification:\n{sorted(new)}"
    )
    assert not gone, (
        f"Blessed raw-read site(s) removed — update _BLESSED_RAW_READS:\n{sorted(gone)}"
    )


def _find_system_yaml() -> Path | None:
    for cand in (_PKG / "system.yaml", _PKG.parent / "system.yaml"):
        if cand.exists():
            return cand
    return None


def test_system_yaml_scripts_are_l1_clean():
    """O4 — every system.yaml job script is anchor-expressed ($CLAUDLOBBY_ROOT/…),
    so the host/fleet timer surface is L1-clean by construction."""
    import yaml

    sy = _find_system_yaml()
    assert sy is not None, "system.yaml not found"
    data = yaml.safe_load(sy.read_text()) or {}
    scripts = [
        (section, name, (cfg or {}).get("script", ""))
        for section in ("host", "defaults")
        for name, cfg in ((data.get(section) or {}).get("jobs") or {}).items()
    ]
    assert scripts, "expected system.yaml to declare jobs"
    for section, name, script in scripts:
        assert denied_source_paths(script, []) == [], (
            f"system.yaml {section}.jobs.{name} script is not anchor-clean: {script!r}"
        )
