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
    # THE TEST FOR `secret`: can the integration AUTHENTICATE without this
    # value? No -> true. Yes -> false. It is not "is this string sensitive to
    # print". Worked example: PRINTIFY_SHOP_ID is false because without it you
    # still authenticate fine, you just cannot target a shop — a config failure,
    # not a credential failure. Stated as a test rather than as a list of
    # example types, because a list settles only the cases already on it.
    # The split is what keeps the fail-loud rung from firing on `PORT`,
    # becoming noise, and being suppressed along with the real alerts.
    #
    # `source` is OPTIONAL. Absent means a human supplies the value, as today
    # (47 of the 48 declared vars). When present it must be a whole identifier
    # from `known_values.KNOWN_CREDENTIAL_SOURCES` — never invent one.
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
            # the same two facts, and it is backfilled and gated exactly like
            # the MCP one -- deliberately, because 10 of its 21 vars have NO
            # paired MCP fragment (`type: cli`: neon, railway, snowflake), so a
            # gate covering only fragments could never reach RAILWAY_API_TOKEN,
            # NEON_API_KEY or the Snowflake key vars. Those are the credentials
            # whose silent blanking started this workstream; exempting them
            # would have been the #1213 shape relocated one surface over.
            #
            # The default below therefore applies only to a contract the
            # validator is already rejecting, same as the MCP branch.
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

    return _reconcile_secret(out)


def _reconcile_secret(records: list[ContractVar]) -> list[ContractVar]:
    """Make ``secret`` independent of traversal order for a both-surface var.

    11 real vars are declared on BOTH surfaces (e.g. ``PRINTIFY_API_KEY`` in
    both ``library/mcp/printify.json`` and ``library/integrations/printify.md``),
    and this function emits a record per declaration — so without reconciling,
    the same var arrives twice carrying two different answers to "is this a
    credential", and which one a consumer sees is decided by walk order. **A
    required field whose value depends on file traversal order is not a
    required field.**

    Records are NOT deduped: both origins are real and a caller may want to know
    a var is declared twice. Only the *value* is unified, so no consumer can
    observe an order-dependent ``secret``.

    Reconciliation is OR, and the direction is the safe one rather than the
    tidy one: if either surface calls a var a credential, it is treated as one.
    Over-alerting is visible and gets corrected; under-alerting is #1213 exactly
    — a real credential nothing ever fires on. The validator refuses a
    disagreement outright (``_validate_env_contracts``), so this is the second
    line: it keeps the value sound for anything that reaches these records
    without having gone through validate.
    """
    secret_by_var: dict[str, bool] = {}
    for r in records:
        secret_by_var[r.name] = secret_by_var.get(r.name, False) or r.secret
    return [
        r if r.secret == secret_by_var[r.name] else r._replace(secret=True)
        for r in records
    ]
