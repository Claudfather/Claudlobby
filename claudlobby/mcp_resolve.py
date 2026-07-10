"""Shared MCP env-var resolution.

Single source of truth for translating ``${VAR}`` placeholders in MCP
fragments into their canonical, instance-aware names.  Both the composer
(which renders ``.mcp.json``) and the validator (which checks that required
vars are set) call into this module so the two can never disagree.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .config import BotConfig, McpEntry
    from .paths import Paths

_log = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def canonical_var_name(var: str, contract: dict, entry: McpEntry, instance: str) -> str:
    """Return the canonical env-var name for *var* given its contract scope.

    Instance-scoped vars get the entry/instance prefix
    (e.g. ``TOKEN`` → ``NOTION_WORK_TOKEN``).
    Shared vars pass through unchanged.
    """
    meta = contract.get(var, {})
    scope = meta.get("scope", "shared")
    if scope == "instance":
        return entry.instance_prefix(instance) + var
    return var


def resolve_placeholders(
    val: object, contract: dict, entry: McpEntry, instance: str
) -> object:
    """Recursively resolve ``${VAR}`` placeholders in *val*.

    Works on strings, lists, and dicts.  Non-string leaves pass through
    unchanged.
    """
    if isinstance(val, str):

        def _replace(m: re.Match) -> str:
            return (
                "${" + canonical_var_name(m.group(1), contract, entry, instance) + "}"
            )

        return _VAR_RE.sub(_replace, val)
    if isinstance(val, dict):
        return {
            k: resolve_placeholders(v, contract, entry, instance)
            for k, v in val.items()
        }
    if isinstance(val, list):
        return [resolve_placeholders(v, contract, entry, instance) for v in val]
    return val


class ContractVar(NamedTuple):
    """One operator-facing env var enumerated from an MCP ``_env_contract`` entry."""

    canonical_name: (
        str  # instance-renamed when instance-scoped, else the raw contract key
    )
    tier: str  # "fleet" | "bot"
    instance: str | None  # instance label when instance-scoped, else None (shared)
    description: str  # meta.get("description", "")


def iter_operator_contract_vars(
    contract: dict, entry: McpEntry
) -> Iterator[ContractVar]:
    """Single home for the operator-facing MCP ``_env_contract`` walk.

    Yields one ContractVar per operator-supplied var in *contract*, applying the
    ``isinstance`` guard, the ``provided_by == "composer"`` skip, tier/scope
    defaults, and per-instance canonical naming. Every enumerator that surfaces
    vars to an operator — ``required_vars`` (validate), ``collect_env_contracts``
    (.env scaffolding + doctor) — consumes this, so the skip and naming rules can
    never drift again (#568, finishes #233).

    NOT for ``compose_mcp_json``: that must KEEP composer-provided vars to
    substitute them into the real ``.mcp.json``, so it reads the contract directly.
    """
    for var_name, meta in contract.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("provided_by") == "composer":
            continue
        tier = meta.get("tier", "fleet")
        scope = meta.get("scope", "shared")
        description = meta.get("description", "")
        if scope == "instance":
            for inst in entry.instances:
                yield ContractVar(
                    canonical_var_name(var_name, contract, entry, inst),
                    tier,
                    inst,
                    description,
                )
        else:
            yield ContractVar(var_name, tier, None, description)


def required_vars(
    bot: BotConfig, paths: Paths
) -> list[tuple[str, str, str, str | None]]:
    """Return ``[(canonical_var, tier, source, instance)]`` this bot needs.

    Walks MCP fragments and integration docs, applying instance-scope
    prefixing so callers see the final var names that land in ``.mcp.json``.
    """
    from .loader import parse_frontmatter

    out: list[tuple[str, str, str, str | None]] = []

    # --- MCP fragment contracts ---
    seen_mcp: set[str] = set()
    for entry in bot.mcp:
        if entry.name in seen_mcp:
            continue
        seen_mcp.add(entry.name)
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            _log.warning("failed to parse %s, skipping", frag_path)
            continue
        contract = frag.get("_env_contract", {})
        # Shared operator-facing walk: skips provided_by:composer + applies
        # instance naming (one home, no drift — #568, was #547). Map to the
        # validator's (var, tier, source, instance) tuple shape.
        for cv in iter_operator_contract_vars(contract, entry):
            out.append((cv.canonical_name, cv.tier, f"mcp/{entry.name}", cv.instance))

    # --- Integration doc contracts (auto-pair fallback matches composer) ---
    integration_names = bot.integrations or [
        e.name
        for e in bot.mcp
        if paths.find_library_file("integrations", e.name, ".md") is not None
    ]
    seen_int: set[str] = set()
    for int_name in integration_names:
        if int_name in seen_int:
            continue
        seen_int.add(int_name)
        int_path = paths.find_library_file("integrations", int_name, ".md")
        if int_path is None:
            continue
        try:
            fm, _ = parse_frontmatter(int_path.read_text())
        except (OSError, ValueError, KeyError):
            _log.warning("failed to parse frontmatter in %s, skipping", int_path)
            continue
        contract = fm.get("env_contract", {}) if isinstance(fm, dict) else {}
        if not isinstance(contract, dict):
            continue
        for var_name, meta in contract.items():
            if not isinstance(meta, dict):
                continue
            tier = meta.get("tier", "fleet")
            out.append((var_name, tier, f"integration/{int_name}", None))

    return out
