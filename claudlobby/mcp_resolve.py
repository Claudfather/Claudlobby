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
    """One operator-facing env var enumerated from an env contract.

    Also what :func:`required_vars` yields — deliberately ONE record rather
    than a near-duplicate per consumer. An earlier draft had a second type
    differing only in carrying ``origin``, which meant every new contract field
    had to be added in three places and re-packed positionally between two
    records whose slots were permuted; a mis-ordered re-pack was silent,
    because the permuted slots share types.

    On the ``source`` / ``origin`` split: ``origin`` is the DECLARING SURFACE
    (``"mcp/github"``, ``"integration/notion"``), which the old bare tuple
    confusingly called ``source``. #1214 adds a contract field genuinely called
    ``source`` meaning the RESOLVER (``"cli:gh-token"``). Two unrelated facts
    under one name is how a reader picks the wrong one, so ``source`` matches
    the JSON key it carries and provenance is ``origin``.
    """

    canonical_name: (
        str  # instance-renamed when instance-scoped, else the raw contract key
    )
    tier: str  # "fleet" | "bot"
    instance: str | None  # instance label when instance-scoped, else None (shared)
    description: str  # meta.get("description", "")
    # --- #1214 Phase 1: the two fields that make a value obtainable ---
    # `secret` is whether an unresolved value is a CREDENTIAL FAILURE worth
    # alerting on, NOT whether the string is sensitive to print. Roughly 41% of
    # the declared surface is ports, IDs, URLs and paths; a fail-loud rung that
    # fires on `PORT` becomes noise, gets suppressed, and takes the real alerts
    # with it. `source` is a whole identifier from
    # `known_values.KNOWN_CREDENTIAL_SOURCES`; None means a human supplies it.
    secret: bool = False
    source: str | None = None
    # Set by whichever enumerator labelled this var; empty from the bare walk,
    # which does not know which surface asked.
    origin: str = ""

    @property
    def name(self) -> str:
        """Alias for :attr:`canonical_name` — reads better on a required-var."""
        return self.canonical_name


# What `required_vars` yields. Same record; the alias documents the intent at
# the call site without forking the type.
RequiredVar = ContractVar


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
        # Defaulting rather than raising keeps read-only callers (doctor, .env
        # scaffolding) working on a fragment the validator is already rejecting,
        # and False is the safe direction: it under-alerts on a malformed
        # fragment rather than firing a credential alert for every var in it.
        secret = bool(meta.get("secret", False))
        source = meta.get("source")
        if scope == "instance":
            for inst in entry.instances:
                yield ContractVar(
                    canonical_var_name(var_name, contract, entry, inst),
                    tier,
                    inst,
                    description,
                    secret,
                    source,
                )
        else:
            yield ContractVar(var_name, tier, None, description, secret, source)


def required_vars(bot: BotConfig, paths: Paths) -> list[RequiredVar]:
    """Return the :class:`RequiredVar` records this bot needs.

    Walks MCP fragments and integration docs, applying instance-scope
    prefixing so callers see the final var names that land in ``.mcp.json``.

    Yields records, not bare tuples — see :class:`RequiredVar` for why the
    provenance slot is ``origin`` and ``source`` now means the resolver.
    """
    from .loader import parse_frontmatter

    out: list[RequiredVar] = []

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
        # instance naming (one home, no drift — #568, was #547). Re-labelled
        # with this declaring surface; every other field is carried as-is, so
        # a new contract field is one edit rather than a hand-written re-pack
        # whose permuted slots would mis-bind silently.
        for cv in iter_operator_contract_vars(contract, entry):
            out.append(cv._replace(origin=f"mcp/{entry.name}"))

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
            # Integration-doc frontmatter is the OTHER declaration surface for
            # the same two facts, and Phase 1 backfills MCP fragments ONLY (the
            # plan's stated scope), so every var here reads secret=False.
            #
            # That is a KNOWN, MEASURED HOLE, not merely deferred work, and the
            # difference matters for whoever wires the Phase 3 rung:
            #   - 11 vars are declared on BOTH surfaces and now DISAGREE (e.g.
            #     SHOPIFY_ACCESS_TOKEN is secret here and not-secret there);
            #     required_vars appends both without reconciling.
            #   - 5 real credentials are unreachable by the MCP-scoped gate
            #     FOREVER, not until a backfill: NEON_API_KEY, RAILWAY_API_TOKEN,
            #     RAILWAY_PERSONAL_TOKEN, SNOWFLAKE_PRIVATE_KEY and
            #     SNOWFLAKE_PRIVATE_KEY_PATH belong to `type: cli` integrations
            #     that have no library/mcp/*.json to declare them in.
            # Wiring fail-loud to `secret` before this surface is backfilled
            # exempts exactly those, which is the #1213 shape relocated. Tracked
            # on #1214; do not treat the default as an answer.
            out.append(
                ContractVar(
                    var_name,
                    tier,
                    None,
                    meta.get("description", ""),
                    bool(meta.get("secret", False)),
                    meta.get("source"),
                    f"integration/{int_name}",
                )
            )

    return out
