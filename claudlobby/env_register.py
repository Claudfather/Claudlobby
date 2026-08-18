"""The credential register — DERIVED, never written (#1214 F6 / #1226 stage 4).

The ask was "a note of things that work this way". A hand-kept note is stale
the first time someone adds an integration and forgets, and its staleness is
invisible: it still reads like an answer. So this is generated from the two
things that already know — the declaration surfaces (what the fleet says it
needs) and the runtime resolver (what a boot would actually find).

**It reports SHADOWING, not just resolution.** A register that printed only
what resolved would miss the state that motivated it: a var resolving to the
EMPTY string from a more specific tier while a real value sits upstream. That
state is invisible to every existing check — the key IS set, so nothing reports
it missing; a value DOES exist, so nothing reports it unconfigured — and it is
one host-tier PAT away on this estate, where two fleets carry a pristine
``export GITHUB_PAT=`` composer stub at the fleet tier. So BLANKED sorts first
and is counted in the summary.

No credential value is ever emitted. Rows carry a key, a tier, and a state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from .env_tiers import ResolverUnavailable, resolve

if TYPE_CHECKING:
    from .config import FleetConfig
    from .paths import Paths

#: Row states, worst first. The order IS the report order — see module docstring.
BLANKED = "BLANKED"  #: resolves EMPTY over a real value upstream
EMPTY = "EMPTY"  #: resolves empty, nothing upstream was lost
UNSET = "UNSET"  #: no tier assigns it
SET = "SET"  #: resolves to a value

_SEVERITY = {BLANKED: 0, EMPTY: 1, UNSET: 2, SET: 3}


class RegisterRow(NamedTuple):
    """One declared var: where it resolves from, and by what means."""

    name: str
    state: str
    tier: str  #: resolving tier, or "-" when nothing assigns it
    declared_by: str  #: "mcp/github", "integration/neon", "telegram", …
    scaffold_tier: str  #: where this fleet writes its stub, if anywhere
    means: str  #: declared credential source, or "operator" when none
    shadowed: tuple[str, ...]  #: tiers the winner overrode
    blanked: tuple[str, ...]  #: tiers that held a REAL value and lost to empty

    @property
    def severity(self) -> int:
        return _SEVERITY[self.state]


class Register(NamedTuple):
    rows: tuple[RegisterRow, ...]
    bot: str | None
    tiers: tuple[tuple[str, str, str], ...]  #: (tier, path, state) as reported
    undeclared: tuple[str, ...]  #: keys present in a tier that nothing declares


def build(fleet: FleetConfig, paths: Paths, bot: str | None = None) -> Register:
    """Derive the register for *bot* (or the fleet's shared tiers without one).

    Declarations come from ``collect_env_contracts`` — the same walk that drives
    scaffolding and doctor — never a private scan, so the register cannot report
    a var the compositor does not believe in, or miss one it does.
    """
    from .composer import collect_env_contracts

    declared = {ev.name: ev for ev in collect_env_contracts(fleet, paths)}
    resolved = resolve(paths, bot_name=bot)
    tiers = tuple(
        (t.tier, str(t.path) if t.path else "-", t.state)
        for t in paths.env_tiers(bot)
    )

    rows: list[RegisterRow] = []
    for name, ev in sorted(declared.items()):
        res = resolved.get(name)
        if res is None:
            state, tier, shadowed, blanked = UNSET, "-", (), ()
        elif res.blanked_upstream:
            state, tier = BLANKED, res.tier
            shadowed, blanked = res.shadowed, res.blanked_upstream
        elif res.empty:
            state, tier = EMPTY, res.tier
            shadowed, blanked = res.shadowed, ()
        else:
            state, tier = SET, res.tier
            shadowed, blanked = res.shadowed, ()
        rows.append(
            RegisterRow(
                name=name,
                state=state,
                tier=tier,
                declared_by=ev.source,
                scaffold_tier=ev.scaffold_tier(),
                means=_means(fleet, ev.name, bot),
                shadowed=shadowed,
                blanked=blanked,
            )
        )

    # A key sitting in a tier that no contract declares. Reported, never
    # actioned: it is usually legitimate (an operator's own variable), but it is
    # also what a renamed contract key leaves behind, and nothing else says so.
    undeclared = tuple(sorted(set(resolved) - set(declared)))

    rows.sort(key=lambda r: (r.severity, r.name))
    return Register(tuple(rows), bot, tiers, undeclared)


def _means(fleet: FleetConfig, var: str, bot: str | None) -> str:
    """How the value is meant to arrive: a declared source, or an operator.

    Reads the per-scope override (#1214 F6c) when a bot is named. Absent any
    declaration the answer is "operator", which is how 47 of 48 vars behave —
    stated positively rather than left blank, because an empty column reads as
    "unknown" when it is in fact the norm.
    """
    if bot and bot in fleet.bots:
        declared = fleet.bots[bot].credential_sources.get(var)
        if declared:
            return declared
    seen = {
        b.credential_sources[var]
        for b in fleet.bots.values()
        if var in b.credential_sources
    }
    if len(seen) == 1:
        return next(iter(seen))
    if len(seen) > 1:
        return "varies-by-bot"
    return "operator"


def format_report(reg: Register) -> str:
    """Render the register. Worst rows first; never any value."""
    scope = f"bot '{reg.bot}'" if reg.bot else "fleet tiers only (no --bot)"
    out = [f"Credential register — {scope}", ""]

    out.append("Tiers, in runtime cascade order (later wins):")
    for tier, path, state in reg.tiers:
        out.append(f"  {tier:6} {state:11} {path}")
    if not reg.bot:
        out.append(
            "  NOTE: without --bot the bot tier is unresolved, so a var a bot "
            "overrides for itself is not shown here."
        )
    out.append("")

    if not reg.rows:
        out.append("No declared env vars.")
        return "\n".join(out)

    width = max(len(r.name) for r in reg.rows)
    out.append(
        f"{'VAR'.ljust(width)}  {'STATE':8} {'TIER':6} {'MEANS':14} "
        f"{'STUB':6} DECLARED BY"
    )
    for r in reg.rows:
        line = (
            f"{r.name.ljust(width)}  {r.state:8} {r.tier:6} {r.means:14} "
            f"{r.scaffold_tier:6} {r.declared_by}"
        )
        if r.blanked:
            line += f"  <-- EMPTY here overrides a real value at: {', '.join(r.blanked)}"
        elif r.shadowed:
            line += f"  (overrides: {', '.join(r.shadowed)})"
        out.append(line)

    counts = {s: sum(1 for r in reg.rows if r.state == s) for s in _SEVERITY}
    out.append("")
    out.append(
        f"{len(reg.rows)} declared — {counts[SET]} set, {counts[UNSET]} unset, "
        f"{counts[EMPTY]} empty, {counts[BLANKED]} BLANKED"
    )
    if counts[BLANKED]:
        out.append(
            f"  {counts[BLANKED]} var(s) resolve to the EMPTY STRING while a real "
            f"value sits at a less specific tier. Sourcing is assignment, so the "
            f"more specific empty one wins. Nothing else reports this state: the "
            f"key is set, so it is not 'missing', and a value exists, so it is "
            f"not 'unconfigured'. Delete the empty assignment at the tier named."
        )
    if reg.undeclared:
        out.append(
            f"  {len(reg.undeclared)} key(s) present in a tier that no contract "
            f"declares: {', '.join(reg.undeclared)}"
        )
    return "\n".join(out)


def exits_nonzero(reg: Register) -> bool:
    """Only BLANKED. UNSET is an ordinary un-filled credential and failing on it
    would train an operator to ignore the command, taking the real signal with
    it — the same reasoning that keeps creds-reconcile's UNKNOWN at rc 0."""
    return any(r.state == BLANKED for r in reg.rows)


__all__ = [
    "BLANKED",
    "EMPTY",
    "Register",
    "RegisterRow",
    "ResolverUnavailable",
    "SET",
    "UNSET",
    "build",
    "exits_nonzero",
    "format_report",
]
