"""Reconcile declared credentials against stored values against equipped bots (#1104).

Nothing on this estate validated that a declared credential has a value, that a
stored value has a consumer, or that a declaration and its consumer name the
same key. The measured consequence: a VALID Railway token sat in a fleet-tier
`.env` that nothing read, a DEAD one was ambient and `creds-check` read that,
and the real consumer read a third source entirely. A good credential nobody
used, while the checker false-FAILed against a stale one.

THIS MODULE ANSWERS TWO OF THE THREE SHAPES, AND SAYS SO IN ITS OUTPUT.

    shape 1  declared, no value          -> OK / FAIL   (soundly decidable here)
    shape 2  value, no equipped consumer -> OK / FAIL   (soundly decidable here)
    shape 3  declaration and consumer
             name DIFFERENT keys         -> UNKNOWN, unless a consumer contract
                                            is published

Shape 3 is deliberately NOT attempted, and the reason is a rule this repo
already holds: *consume siblings by contract, never by assertion*. Discovering
that the `railway-ops` plugin agent reads `~/.railway/config.json` requires
grepping the plugin tree — inferring a sibling's behaviour from a snapshot of
its source. That is right today and silently wrong the next time the plugin
changes its read path, with no signal at the moment it turns wrong. It would
reproduce this issue's own defect one level up.

So shape 3 resolves only where the CONSUMER declares what it reads. Until that
contract exists, every integration reports:

    UNKNOWN - no consumer contract published for <integration>

**UNKNOWN is printed and counted, never silence and never OK.** This is the
load-bearing property of the whole module. A validator that quietly omits shape
3 is indistinguishable from one that checked and found nothing — which is
exactly the bug it was built to remove, recreated inside its own remedy. The
summary line therefore always carries an `unknown=` count, and a run where
every integration is UNKNOWN says so loudly rather than printing a clean bill.

THE VISIBILITY TRAP, which is why shape 1 finds real failures here. `Paths.
env_file` returns the FLEET `.env` when one exists and the ROOT `.env`
otherwise — one or the other, never both. So a credential stored only at root
is invisible to every fleet that has its own `.env`, while still looking
present to anyone who greps the repo. `lib/creds-check.sh` had the mirror bug
(it read root while being invoked per-fleet). An inventory that resolved
tiers its own way would inherit one side or the other, so tier resolution here
goes through `Paths.env_file` — the shipped predicate — and every finding names
the tier it was decided on.

NO VALUE IS EVER EMITTED. Presence, emptiness and tier are facts about a
credential; the credential itself is not, and this output goes into PR bodies
and chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .dotenv import read as read_env_file

# Frontmatter key a consumer would use to declare what it actually READS —
# the seam shape 3 closes through. Nothing publishes it yet, which is precisely
# what UNKNOWN reports. Named here so the reconciler consumes a contract rather
# than inventing one, and so the other half of #1104 has a target to fill.
CONSUMER_CONTRACT_KEY = "consumer_contract"


@dataclass
class Declaration:
    """A credential some equipped thing says it needs."""

    var: str
    source: str  # "railway", "github" ...
    kind: str  # "integration" | "mcp"
    equipped_by: list[str] = field(default_factory=list)


@dataclass
class Finding:
    shape: int
    verdict: str  # OK | FAIL | UNKNOWN
    subject: str  # var name, or integration name for shape 3
    detail: str

    @property
    def is_failure(self) -> bool:
        return self.verdict == "FAIL"


# ----------------------------------------------------------------------
# Declaration side
# ----------------------------------------------------------------------


def parse_frontmatter_block(text: str, key: str) -> dict:
    """Pull one nested mapping out of a library file's YAML frontmatter.

    Hand-rolled rather than pulling yaml in, matching `dotenv.py`'s reasoning:
    the shape is fixed and shallow (`key:` then indented `VAR:` then indented
    attributes). A malformed block yields {} rather than raising — one bad
    integration file must not take out the whole inventory, and the file is
    reported as contributing nothing rather than silently as contributing
    correctly.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    front = text[3:end]

    out: dict[str, dict] = {}
    in_block = False
    current: str | None = None
    for raw in front.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if indent == 0:
            in_block = stripped.rstrip(":") == key and stripped.endswith(":")
            current = None
            continue
        if not in_block:
            continue
        if indent == 2 and stripped.endswith(":"):
            current = stripped[:-1].strip()
            out[current] = {}
        elif indent >= 4 and current and ":" in stripped:
            attr, _, value = stripped.partition(":")
            out[current][attr.strip()] = value.strip()
    return out


def declared_for_fleet(fleet, paths) -> tuple[list[Declaration], set[str]]:
    """(declarations, equipped integration names), via the SHIPPED resolver.

    Declarations come from `mcp_resolve.required_vars`, never from a private
    scan of the library. That is not a stylistic preference — the first version
    of this module regex-scraped `${VAR}` out of MCP fragments and reported
    `TOKEN` as an unsatisfied credential on the live estate. The fragment really
    does say `${TOKEN}`, but the contract scopes it per instance, so the name
    that must actually exist is `NOTION_TOKEN` — which was present the whole
    time. A hand-rolled reader disagreed with the framework's own resolver and
    manufactured a FAIL, in a tool built because a checker reading the wrong key
    produced a false FAIL.

    `required_vars` also walks integration docs, so both declaration surfaces
    arrive already canonicalised and instance-prefixed.
    """
    from .mcp_resolve import required_vars

    by_var: dict[tuple[str, str], Declaration] = {}
    integrations: set[str] = set()

    for bot_name, bot in getattr(fleet, "bots", {}).items():
        for name in getattr(bot, "integrations", []) or []:
            integrations.add(name)
        try:
            needed = required_vars(bot, paths)
        except Exception:  # noqa: BLE001 - one bad bot must not void the inventory
            continue
        for req in needed:
            # required_vars labels its ORIGIN "integration/<name>" and
            # "mcp/<name>". Split on that rather than testing membership in the
            # equipped-integration set, which silently mislabelled every
            # integration var as mcp because the prefixed string never matched
            # a bare name. Read `.origin`, never `.source` — since #1214 the
            # latter is the resolver descriptor and would partition to garbage.
            kind, _, short = req.origin.partition("/")
            if kind not in ("integration", "mcp"):
                kind, short = "mcp", req.origin
            key = (req.name, short)
            decl = by_var.get(key)
            if decl is None:
                decl = Declaration(var=req.name, source=short, kind=kind)
                by_var[key] = decl
            if bot_name not in decl.equipped_by:
                decl.equipped_by.append(bot_name)

    for decl in by_var.values():
        decl.equipped_by.sort()
    return (sorted(by_var.values(), key=lambda d: (d.var, d.source)), integrations)


# ----------------------------------------------------------------------
# Value side
# ----------------------------------------------------------------------


def env_tiers(paths) -> list[tuple[str, Path, dict]]:
    """[(tier label, path, vars)] for every tier that EXISTS, root first.

    Both tiers are enumerated even though only one is READ, because the gap
    between them is the defect: a value present at root and shadowed by a fleet
    `.env` is invisible in practice while looking present to a grep.
    """
    tiers: list[tuple[str, Path, dict]] = []
    root_env = paths.root / ".env"
    if root_env.is_file():
        tiers.append(("root", root_env, read_env_file(root_env)))
    fleet_dir = getattr(paths, "fleet_dir", None)
    if fleet_dir:
        fleet_env = Path(fleet_dir) / ".env"
        if fleet_env.is_file():
            tiers.append(
                (f"fleet:{Path(fleet_dir).name}", fleet_env, read_env_file(fleet_env))
            )
    return tiers


def visible_tier(paths) -> tuple[str, dict]:
    """The ONE tier this fleet actually reads, via the shipped resolver.

    Deliberately `Paths.env_file` rather than a private rule — a second
    resolution rule is how the root-versus-fleet split became invisible in the
    first place.
    """
    chosen = paths.env_file
    label = "root"
    fleet_dir = getattr(paths, "fleet_dir", None)
    if fleet_dir and Path(chosen) == Path(fleet_dir) / ".env":
        label = f"fleet:{Path(fleet_dir).name}"
    if not Path(chosen).is_file():
        return (label, {})
    return (label, read_env_file(Path(chosen)))


# ----------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------


def reconcile(
    paths, fleet, library_dir: Path | None = None
) -> tuple[list[Finding], dict]:
    """(findings, scope). Shapes 1 and 2 decided; shape 3 reported UNKNOWN."""
    library_dir = Path(library_dir or (paths.root / "library"))
    declarations, integrations = declared_for_fleet(fleet, paths)

    label, visible = visible_tier(paths)
    tiers = env_tiers(paths)
    all_tier_vars: dict[str, list[str]] = {}
    for tier_label, _path, values in tiers:
        for var, value in values.items():
            if str(value).strip():
                all_tier_vars.setdefault(var, []).append(tier_label)

    findings: list[Finding] = []

    # ---- shape 1: declared, no value ---------------------------------
    seen: set[str] = set()
    for decl in sorted(declarations, key=lambda d: (d.var, d.source)):
        if decl.var in seen:
            continue
        seen.add(decl.var)
        value = str(visible.get(decl.var, "")).strip()
        sources = ", ".join(
            sorted({f"{d.kind}:{d.source}" for d in declarations if d.var == decl.var})
        )
        if value:
            findings.append(
                Finding(
                    1,
                    "OK",
                    decl.var,
                    f"declared by {sources}; value present in {label}",
                )
            )
            continue
        elsewhere = [t for t in all_tier_vars.get(decl.var, []) if t != label]
        if elsewhere:
            # The visibility trap, called out by name: present, but not where
            # this fleet reads. A plain "missing" would send someone to add a
            # value that already exists.
            findings.append(
                Finding(
                    1,
                    "FAIL",
                    decl.var,
                    f"declared by {sources}; NO value in {label}, but a value exists in "
                    f"{', '.join(elsewhere)} — shadowed, not missing",
                )
            )
        elif decl.var in visible:
            # Present as a KEY but empty. Distinct from absent, and the remedy
            # differs: someone already provisioned the slot and left it blank,
            # rather than nobody knowing the credential was needed. Collapsing
            # the two sends a reader looking for a declaration that is already
            # there.
            findings.append(
                Finding(
                    1,
                    "FAIL",
                    decl.var,
                    f"declared by {sources}; key present in {label} but EMPTY",
                )
            )
        else:
            findings.append(
                Finding(
                    1,
                    "FAIL",
                    decl.var,
                    f"declared by {sources}; absent from every tier ({label} is what this "
                    "fleet reads)",
                )
            )

    # ---- shape 2: value, no equipped consumer ------------------------
    declared_vars = {d.var for d in declarations}
    for var in sorted(all_tier_vars):
        if var in declared_vars:
            continue
        findings.append(
            Finding(
                2,
                "FAIL",
                var,
                f"value stored in {', '.join(all_tier_vars[var])}; no equipped bot declares it",
            )
        )

    # ---- shape 3: UNKNOWN unless a consumer contract exists -----------
    for name in sorted(integrations):
        path = library_dir / "integrations" / f"{name}.md"
        contract = {}
        if path.is_file():
            contract = parse_frontmatter_block(
                path.read_text(encoding="utf-8", errors="replace"),
                CONSUMER_CONTRACT_KEY,
            )
        if contract:
            findings.append(
                Finding(
                    3,
                    "OK",
                    name,
                    f"consumer contract published: {', '.join(sorted(contract))}",
                )
            )
        else:
            findings.append(
                Finding(
                    3, "UNKNOWN", name, f"no consumer contract published for {name}"
                )
            )

    scope = {
        "fleet_visible_tier": label,
        "tiers_present": [t[0] for t in tiers],
        "integrations_equipped": sorted(integrations),
        "mcp_equipped": sorted({d.source for d in declarations if d.kind == "mcp"}),
        "declarations": len(declarations),
    }
    return findings, scope


def format_report(findings: list[Finding], scope: dict) -> str:
    lines = [
        "credential reconcile (#1104) — shapes 1 and 2 decided, shape 3 NOT attempted",
        f"  tiers present: {', '.join(scope['tiers_present']) or '(none)'}",
        f"  tier this fleet reads: {scope['fleet_visible_tier']}",
        f"  equipped: {len(scope['integrations_equipped'])} integrations, "
        f"{len(scope['mcp_equipped'])} mcp servers, {scope['declarations']} declarations",
        "",
    ]
    for shape, title in (
        (1, "shape 1 — declared, no value"),
        (2, "shape 2 — value, no equipped consumer"),
        (
            3,
            "shape 3 — declaration vs consumer key (NOT ATTEMPTED — contract required)",
        ),
    ):
        rows = [f for f in findings if f.shape == shape]
        lines.append(f"{title}  [{len(rows)}]")
        if not rows:
            lines.append("    (nothing equipped contributes to this shape)")
        for row in rows:
            lines.append(f"    {row.verdict:<7} {row.subject:<34} {row.detail}")
        lines.append("")

    fails = sum(1 for f in findings if f.verdict == "FAIL")
    unknown = sum(1 for f in findings if f.verdict == "UNKNOWN")
    ok = sum(1 for f in findings if f.verdict == "OK")
    lines.append(f"summary: ok={ok} fail={fails} unknown={unknown}")
    if unknown:
        lines.append(
            f"  {unknown} UNKNOWN means shape 3 was NOT checked for those integrations — "
            "no consumer contract exists to check against. This is a gap, not a pass."
        )
    return "\n".join(lines)


def exits_nonzero(findings: list[Finding]) -> bool:
    """UNKNOWN alone never fails the run — it is a disclosed gap, not a defect."""
    return any(f.is_failure for f in findings)
