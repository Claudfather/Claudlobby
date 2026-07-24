"""R7 — guard-the-guard tripwire (#702).

The L1 source guard only protects the source values it actually sees: the
BotConfig dataclass walk, the MCP fragments passed to ``audit_bot_sources``, the
finalized grants, resolved tool params, and timer scripts. A NEW raw parse/read
of a fleet-controlled file (a new ``json.loads`` / ``yaml.safe_load`` /
``read_text``) is a new source surface that could smuggle an unguarded absolute
path past the guard.

This test inventories every raw parse/read site in composer.py + config.py and
fails when the set changes — forcing a conscious decision: route the new source
through ``path_audit.audit_bot_sources`` (or the grant/fragment choke), or record
it here as a documented exempt (a tool ``.j2`` body, a prose charter, or
pre-existing runtime state — none of which is a fleet.yaml-shaped source).
"""

from __future__ import annotations

import re
from pathlib import Path

import claudlobby
from claudlobby.path_audit import denied_source_paths

_PKG = Path(claudlobby.__file__).parent
_RAW_READ = re.compile(r"json\.loads|yaml\.safe_load|\.read_text\(")

# Every raw parse/read site is either WIRED to the L1 guard or a documented
# EXEMPT. When this set changes, wire the new site or justify the exemption.
_BLESSED_RAW_READS = {
    # config.py — fleet.yaml / system.yaml parse. The parsed BotConfig feeds
    # audit_bot_sources (the dataclass walk); system.yaml is asserted L1-clean below.
    ("config.py", "doc = yaml.safe_load(f)"),
    ("config.py", '_cache["data"] = yaml.safe_load(f) or {}'),
    # composer.py — MCP fragment loads feed audit_bot_sources(fragments=...);
    # grant/integration frontmatter reads feed the grant choke; template / prose /
    # runtime-state reads are exempt-as-code (tool .j2 → #703) or non-source.
    ("composer.py", "return json.loads(frag_path.read_text())"),
    ("composer.py", "frag = json.loads(frag_path.read_text())"),
    (
        "composer.py",
        "content = env.from_string(template_path.read_text()).render(context)",
    ),
    ("composer.py", "_demote_headings(charter.read_text())"),
    ("composer.py", "existing = json.loads(access_path.read_text())"),
    ("composer.py", "fm, _ = parse_frontmatter(int_path.read_text())"),
    ("composer.py", "existing_content = env_path.read_text()"),
}


def _scan(module: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for line in (_PKG / module).read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _RAW_READ.search(stripped):
            out.add((module, stripped))
    return out


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
