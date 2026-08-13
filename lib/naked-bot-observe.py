#!/usr/bin/env python3
"""The #1168 Phase 3 naked-bot observation gate.

Compose a fleet that declares NOTHING and record what a bot receives anyway —
observed, never reasoned about. Phase 2 populates ``DEFAULT_*`` one entity type
per PR; this is what those PRs diff against, so a default that lands is visible
as a delta rather than argued about in a review thread.

WHY A HARNESS AND NOT A ONE-OFF READING. A baseline captured by hand is a claim
about a moment nobody can re-enter. Phase 2's whole gate is "any INSTRUCT-class
addition must be present in this diff", which needs the *same* observation
re-derivable on demand at a later commit. So the reading and the record are one
mechanism.

TWO SURFACES, DELIBERATELY NOT MERGED (the reason the gate exists at all). A
skill symlink that exists but composes no instruction is a DIFFERENT OUTCOME
from one that adds a section, and only reading the composed ``CLAUDE.md``
distinguishes them. ``SURFACES`` below therefore records, per entity type, the
file artifact AND the instruction section separately; a type that lands in one
and not the other is the interesting case, not a rounding error.

WHY ``claudlobby freshbox`` IS NOT SUFFICIENT HERE, though the plan names it the
primary instrument. Measured on the naked fleet: freshbox reports
``OK — Self-contained`` while the bot is carrying a protocol it never declared.
It audits GRANTS — ``settings.local.json``, ``.mcp.json``, ``bot.conf``,
rendered ``tools/`` — and never opens ``CLAUDE.md`` (zero matches in
``freshbox.py``). Composed prose is not a grant, so freshbox is blind to the
INSTRUCT tier BY CONSTRUCTION — precisely the tier Phase 3 gates. Freshbox
remains the right instrument for the WIRE/RESTRICT half and this harness runs
it; it is a floor, not the gate.

WHY THE TREE IS EXPORTED RATHER THAN COMPOSED IN PLACE. Two independent
reasons, and each one alone would force it:

  * A checkout under a bot's ``projects/`` dir sits inside ``…/runtime/bots/…``,
    which ``path_audit._fleet_layout_needles`` matches as fleet-owned BY SHAPE.
    ``CLAUDLOBBY_ROOT`` then reads as a cross-fleet leak and ``generate`` fails
    on a bot that is perfectly well-formed. That is an artifact of where the
    repo happens to live, and composing in place would report it as a defect of
    the probe.
  * The gate must observe a NAMED REF, not a working tree. ``git archive``
    gives a history-free tree at an exact commit, so a baseline is attributable
    to a SHA instead of to whatever was uncommitted that afternoon.

THE ASSERTION THAT MAKES THE RESULT MEAN ANYTHING (``_assert_compositor``). An
editable install of this same package is normally on ``sys.path``. If the
subprocess resolved THAT instead of the exported tree, every arm would compose
against a compositor of unknown vintage and come back green having tested
nothing — the failure mode is a PASS, which is why it must be checked rather
than assumed. This gate ran for the first time on a host whose shared install
had been 27 commits stale hours earlier; that is not a hypothetical.

Standalone stdlib module (the ``dispatch-overdue.py`` / ``who-reviewed.py``
precedent), so the parsing and diffing are unit-testable without composing
anything. Wrapped by ``tests/test_naked_bot_observe.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Schema version of the emitted inventory. Bump when a consumer would have to
#: change; a baseline recorded under an older version is not silently comparable.
#: v2 added `composed_content` (the mcp/permissions blind spot). Adding a field
#: WITHOUT bumping this let the version guard pass and `diff_reports` then
#: KeyError'd on the older record — caught in development, and the reason the
#: field access below is `.get` rather than `[]`.
SCHEMA = 2

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- surfaces


@dataclass(frozen=True)
class Surface:
    """Where one entity type would become visible, if it defaulted to anything.

    ``section`` is the ``## <label>`` heading the template renders for this type
    (``templates/claude.md.j2``, ``render_section``). ``None`` means the type has
    NO instruction surface at all — it cannot add prose to ``CLAUDE.md`` however
    it is populated.

    ``artifacts`` are bot-dir-relative globs the type writes to. ``None`` means
    it writes no file of its own.

    A type with a section and no artifact (``protocols``) can change behaviour
    while leaving the directory byte-identical. A type with an artifact and no
    section (``skills``, ``mcp``, ``tools``) can do the reverse. Recording one
    number for both is how a gate misses half of what it was built to catch.

    ``content_keys`` names a JSON file whose KEYS must be read, because the file
    exists on a naked bot either way. ``skills`` and ``tools`` write one artifact
    PER ENTRY, so a path inventory sees their defaults arrive; ``mcp`` and
    ``permissions`` write into a single always-present file, so a path-only
    inventory would report "no change" for a default that landed inside it —
    silently blind for two of twelve, in exactly the direction that reads clean.
    """

    section: str | None
    artifacts: tuple[str, ...] = ()
    #: (path, json-pointer-ish accessor) — the keys to record from a file that
    #: is present regardless of whether this type defaulted to anything.
    content_keys: tuple[str, str] | None = None


#: Derived by reading `templates/claude.md.j2` (the eight `render_section` calls)
#: and the composer's per-type emitters. Pinned by
#: `test_surface_sections_match_the_template`, so the template moving without
#: this map moving is a test failure rather than a silently blind gate.
SURFACES: dict[str, Surface] = {
    # INSTRUCT — every one of these can add prose to a bot that did not ask.
    "expertise": Surface(section=None),  # composes as the title + body, not a section
    "skills": Surface(section=None, artifacts=(".claude/skills/*",)),
    "protocols": Surface(section="Protocols"),
    "principles": Surface(section="Principles"),
    "post_actions": Surface(section="Post-actions"),
    # RESTRICT
    "guardrails": Surface(section="Guardrails"),
    "permissions": Surface(
        section="Permissions",
        artifacts=(".claude/settings.local.json",),
        content_keys=(".claude/settings.local.json", "permissions.allow"),
    ),
    # WIRE
    "mcp": Surface(
        section=None,
        artifacts=(".mcp.json",),
        content_keys=(".mcp.json", "mcpServers"),
    ),
    "tools": Surface(section=None, artifacts=("tools/*",)),
    "integrations": Surface(section="Integrations"),
    "resources": Surface(section="Resources"),
    "lessons": Surface(section="Lessons"),
}


# ------------------------------------------------------------------ pure parsing


def parse_sections(markdown: str) -> dict[str, list[str]]:
    """Map each ``## H2`` in a composed CLAUDE.md to its ``### H3`` titles.

    Fenced code blocks are skipped: composed library content routinely contains
    ``#``-commented shell inside a fence, and counting those as headings would
    invent sections that no entity type produced. Both fence markers are honoured
    (``` and ~~~) because library authors use each.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for raw in markdown.splitlines():
        stripped = raw.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            continue
        if raw.startswith("## ") and not raw.startswith("###"):
            current = raw[3:].strip()
            sections.setdefault(current, [])
        elif raw.startswith("### ") and current is not None:
            sections[current].append(raw[4:].strip())
    return sections


def inventory_dir(bot_dir: Path) -> list[str]:
    """Bot-dir-relative paths of everything composed, sorted and stable.

    Symlinks are recorded as ``path -> target`` because a skill arrives as a
    symlink and "the link exists" is the fact worth diffing. Directories are
    recorded with a trailing ``/`` so an EMPTY ``.claude/skills/`` (what a naked
    bot gets) stays distinguishable from the directory being absent — those are
    different states and Phase 2 moves between them.
    """
    out: list[str] = []
    for p in sorted(bot_dir.rglob("*")):
        rel = p.relative_to(bot_dir).as_posix()
        if p.is_symlink():
            out.append(f"{rel} -> {os.readlink(p)}")
        elif p.is_dir():
            out.append(f"{rel}/")
        else:
            out.append(rel)
    return out


def match_artifacts(entries: list[str], globs: tuple[str, ...]) -> list[str]:
    """Entries from :func:`inventory_dir` matching any of *globs*.

    Matching is on the path portion only, so a symlink's ``-> target`` suffix
    never has to be encoded into a pattern.
    """
    import fnmatch

    hits: list[str] = []
    for e in entries:
        path = e.split(" -> ", 1)[0].rstrip("/")
        if any(fnmatch.fnmatch(path, g) for g in globs):
            hits.append(e)
    return hits


@dataclass
class TypeObservation:
    """What one entity type actually produced on one arm."""

    tier: str
    #: Entries the registry resolves for this type — the DECLARED intent.
    registry_entries: list[str]
    #: `### ` titles under this type's `## ` section. `None` = type has no
    #: instruction surface; `[]` = it has one and composed nothing into it.
    composed_instructions: list[str] | None
    #: Bot-dir entries this type produced.
    composed_artifacts: list[str]
    #: Keys read from an always-present file (see `Surface.content_keys`).
    #: `None` = this type has no such file.
    composed_content: list[str] | None = None

    @property
    def instructs(self) -> bool:
        """True when this type put prose in front of the bot on this arm."""
        return bool(self.composed_instructions)

    @property
    def inert(self) -> bool:
        """The registry declares entries and NOTHING of this type composed.

        This is the state that makes a Phase 2 PR look landed while changing no
        bot: populating ``REGISTRY[<type>].entries`` moves the constant, but
        ``config.py`` consumes only ``DEFAULT_GUARDRAILS`` — nothing feeds
        ``resolve(<type>)`` into the merge for the other eleven. Measured: a
        REAL skill (`doctor`) placed in the registry composed no symlink, while
        the same skill DECLARED in fleet.yaml composed one. So the registry is
        the source of the DECISION but not yet of the BEHAVIOUR, and a gate that
        only diffed composed output would report that PR as a clean no-op
        instead of as an unwired default.
        """
        return bool(self.registry_entries) and not (
            self.composed_instructions or self.composed_artifacts
        )


@dataclass
class Arm:
    """One composed observation: a fleet variant and everything it produced."""

    label: str
    #: The `system_defaults:` block under test, verbatim, or None for baseline.
    system_defaults: str | None
    #: Entity types this arm DECLARED (to test that declaring is not opting out).
    declared: dict[str, list[str]] = field(default_factory=dict)
    #: Wire the probe bot to a Claudron vault. A FLEET-SHAPE axis, not a
    #: `system_defaults` one: some defaults are conditional on how the fleet is
    #: wired rather than on what it switched off, and an inventory that only
    #: varies opt-outs cannot see them (#1172).
    vault_wired: bool = False
    generate_rc: int = -1
    generate_stderr_tail: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)
    dir_entries: list[str] = field(default_factory=list)
    types: dict[str, TypeObservation] = field(default_factory=dict)

    def instructing_types(self) -> list[str]:
        return sorted(t for t, o in self.types.items() if o.instructs)


# ------------------------------------------------------------------ composition


def export_tree(ref: str, dest: Path, repo: Path = REPO_ROOT) -> str:
    """History-free export of *ref* into *dest*. Returns the resolved SHA.

    ``git archive``, not ``clone`` — the same mechanism ``coldstart-harness.sh
    prepare`` uses and what the cold-start gate in ``CLAUDE.md`` prescribes. The
    absence of ``.git`` is asserted rather than trusted: an export that quietly
    carried history would let a later step read the very commits describing the
    defects being measured.
    """
    dest.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", sha],
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    if (dest / ".git").exists():
        raise RuntimeError(f"export at {dest} carries .git — not history-free")
    return sha


#: The probe's expertise. `expertise` is a REQUIRED field, so a fleet declaring
#: literally nothing does not validate — the naked bot is "one bot, one
#: expertise, nothing else", and that requirement is itself part of the baseline.
#: Content is a single sentinel line so that ANY other prose in the composed
#: CLAUDE.md is attributable to a default or the template, never to the role we
#: happened to pick.
PROBE_EXPERTISE = """---
title: probe-minimal
description: Minimal expertise for the #1168 Phase 3 naked-bot observation gate.
---

# probe-minimal

PROBE_EXPERTISE_SENTINEL — the only content this fleet declares.
"""

FLEET_TEMPLATE = """# NAKED PROBE — generated by lib/naked-bot-observe.py (#1168 Phase 3).
fleet:
  name: naked-probe
  service_prefix: com.example.nakedprobe
  telegram_group_chat_id: "-1001234567890"
{system_defaults}
  accounts:
    default: ~/.claude

  bots:
    nakedbot:
      name: nakedbot
      expertise: [probe-minimal]
{declared}"""


def write_probe(root: Path, arm: Arm) -> None:
    """Write the probe fleet for *arm* into the exported tree at *root*."""
    overlay = root / "local" / "naked-probe"
    (overlay / "library" / "expertise").mkdir(parents=True, exist_ok=True)
    (overlay / "library" / "expertise" / "probe-minimal.md").write_text(PROBE_EXPERTISE)
    sd = ""
    if arm.system_defaults is not None:
        sd = "\n  system_defaults:\n" + "".join(
            f"    {line}\n" for line in arm.system_defaults.splitlines()
        )
    declared = ""
    for etype, names in sorted(arm.declared.items()):
        declared += f"      {etype}: [{', '.join(names)}]\n"
    if arm.vault_wired:
        # A path, not a real vault. The composer branches on the field being
        # SET; nothing here reads the tree, and pointing at a real vault would
        # make the observation depend on the host's knowledge corpus.
        declared += "      claudron_vault_path: /tmp/naked-probe-vault\n"
    (overlay / "fleet.yaml").write_text(
        FLEET_TEMPLATE.format(system_defaults=sd, declared=declared)
    )


def _assert_compositor(root: Path, python: str) -> None:
    """Refuse to run unless the subprocess resolves the EXPORTED compositor.

    An editable install of this package is normally importable, and a green run
    against a stale one is indistinguishable from a green run against the tree
    under test. The failure mode is a PASS, so it is checked, not assumed.
    """
    got = subprocess.run(
        [python, "-c", "import claudlobby, sys; sys.stdout.write(claudlobby.__file__)"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    want = str(root / "claudlobby" / "__init__.py")
    if Path(got).resolve() != Path(want).resolve():
        raise RuntimeError(
            "REFUSING TO OBSERVE: the subprocess resolved a different claudlobby "
            f"than the exported tree.\n  loaded: {got}\n  export: {want}\n"
            "A run against a stale compositor comes back green having tested nothing."
        )


def scrub(text: str, root: Path) -> str:
    """Replace the run's temp export path with a stable token.

    The recorded inventory is COMMITTED and diffed by hand as well as by
    :func:`diff_reports`. A per-run ``mktemp`` path (and the log timestamps
    beside it) makes two observations of the SAME commit differ, which trains a
    reader to skim past drift in the one artifact whose entire job is to make
    drift visible.
    """
    return text.replace(str(root), "$EXPORT")


def run_generate(root: Path, python: str) -> tuple[int, str]:
    """``claudlobby --fleet naked-probe generate`` inside the exported tree.

    The output tail is kept ONLY on failure. On success it is a timestamped
    progress log that says nothing the per-type observation does not, and
    committing it would put unstable noise in a baseline.
    """
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root))
    proc = subprocess.run(
        [python, "-m", "claudlobby", "--fleet", "naked-probe", "generate"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return 0, ""
    tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:])
    return proc.returncode, scrub(tail, root)


def run_freshbox(root: Path, python: str) -> tuple[int, str]:
    """``claudlobby freshbox --strict`` on the composed probe.

    Recorded as evidence for the WIRE/RESTRICT half AND as the standing
    demonstration of its bound: it passes while an undeclared protocol composes.
    """
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root))
    proc = subprocess.run(
        [python, "-m", "claudlobby", "--fleet", "naked-probe", "freshbox", "--strict"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, scrub((proc.stdout or proc.stderr).strip(), root)


def read_content_keys(bot_dir: Path, probe: tuple[str, str] | None) -> list[str] | None:
    """Keys at a dotted path inside a composed JSON file, sorted.

    Unreadable or missing is recorded as a sentinel rather than as ``[]``: an
    empty list means "the file said nothing was configured", which is a real
    observation, and a parse failure must never be able to impersonate it.
    """
    if probe is None:
        return None
    rel, dotted = probe
    path = bot_dir / rel
    if not path.is_file():
        return ["<ABSENT>"]
    try:
        node = json.loads(path.read_text())
        for part in dotted.split("."):
            node = node[part]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [f"<UNREADABLE: {type(exc).__name__}>"]
    if isinstance(node, dict):
        return sorted(node)
    if isinstance(node, list):
        return sorted(str(x) for x in node)
    return [str(node)]


def observe_arm(root: Path, python: str, arm: Arm, registry) -> Arm:
    """Compose one arm and fill in everything it produced."""
    write_probe(root, arm)
    arm.generate_rc, arm.generate_stderr_tail = run_generate(root, python)
    bot_dir = root / "local" / "naked-probe" / "runtime" / "bots" / "nakedbot"
    if arm.generate_rc != 0 or not bot_dir.is_dir():
        return arm  # a failed arm records its rc and stays empty, never green

    arm.dir_entries = inventory_dir(bot_dir)
    arm.sections = parse_sections((bot_dir / "CLAUDE.md").read_text())

    for etype, surface in sorted(SURFACES.items()):
        disp = registry.REGISTRY.get(etype)
        arm.types[etype] = TypeObservation(
            tier=disp.tier.value if disp else "UNREGISTERED",
            registry_entries=list(registry.resolve(etype)),
            composed_instructions=(
                arm.sections.get(surface.section, [])
                if surface.section is not None
                else None
            ),
            composed_artifacts=match_artifacts(arm.dir_entries, surface.artifacts),
            composed_content=read_content_keys(bot_dir, surface.content_keys),
        )
    return arm


def build_arms(types: list[str]) -> list[Arm]:
    """Baseline, one opt-out arm per entity type, plus the two control arms.

    The two controls are not decoration. ``guardrails`` is the ONLY type with a
    populated default today, so it is the only arm that can demonstrate the
    probe detects a working opt-out at all — without it, twelve no-ops read as
    twelve findings instead of one finding plus a broken instrument. The bogus
    key separates "this opt-out is unimplemented" from "unknown keys are
    rejected", which have opposite remedies.
    """
    arms = [Arm(label="baseline", system_defaults=None)]
    for t in types:
        arms.append(Arm(label=f"optout:{t}", system_defaults=f"{t}: false"))
    arms.append(Arm(label="control:kill-switch", system_defaults="enabled: false"))
    arms.append(Arm(label="control:unknown-key", system_defaults="not_a_type: false"))
    # Declaring a list must NOT suppress the default. Only a type with a
    # non-empty default can show this at compose time; the property is pinned
    # for all twelve at the merge layer in tests/test_naked_bot_observe.py.
    arms.append(
        Arm(
            label="declared:guardrails",
            system_defaults=None,
            declared={"guardrails": ["no-push-main"]},
        )
    )
    # A SECOND FLEET SHAPE, not a second opt-out. Every arm above varies what the
    # fleet switched OFF; this one varies how it is WIRED, which is a different
    # axis and the one the inventory was blind to. #1172 lived exactly there: a
    # vault-wired bot composed the template's "never open the tree by hand"
    # section beside a protocol telling it to hand-scan that tree, and no arm
    # could see it because all 16 were vault-less. The two `shared-documentation`
    # forms are mutually exclusive, so this arm is also what would catch a gate
    # regression that composed both or neither.
    arms.append(Arm(label="shape:vault-wired", system_defaults=None, vault_wired=True))
    return arms


# ---------------------------------------------------------------------- reporting


def build_report(sha: str, arms: list[Arm], freshbox: tuple[int, str]) -> dict:
    baseline = next(a for a in arms if a.label == "baseline")
    return {
        "schema": SCHEMA,
        "ref": sha,
        "freshbox": {"rc": freshbox[0], "output": freshbox[1]},
        "baseline_instructing_types": baseline.instructing_types(),
        # Types whose registry entry composed NOTHING — an unwired default. A
        # non-empty list here means a Phase 2 PR moved a constant and changed no
        # bot; see `TypeObservation.inert`.
        "baseline_inert_defaults": sorted(
            t for t, o in baseline.types.items() if o.inert
        ),
        "arms": [asdict(a) for a in arms],
    }


def diff_reports(old: dict, new: dict) -> list[str]:
    """Human-readable drift between two inventories, or [] when identical.

    Compares the ARMS, not the ref: the point is that composing the same naked
    fleet at a later commit yields the same thing, and the SHA is expected to
    move. Reported per (arm, entity type) so a Phase 2 PR sees exactly which
    type it changed.
    """
    if old.get("schema") != new.get("schema"):
        return [
            f"schema changed {old.get('schema')} -> {new.get('schema')}; "
            "baselines across schema versions are not comparable"
        ]
    out: list[str] = []
    old_arms = {a["label"]: a for a in old.get("arms", [])}
    new_arms = {a["label"]: a for a in new.get("arms", [])}
    for label in sorted(set(old_arms) | set(new_arms)):
        if label not in old_arms:
            out.append(f"[{label}] NEW ARM")
            continue
        if label not in new_arms:
            out.append(f"[{label}] ARM REMOVED")
            continue
        o, n = old_arms[label], new_arms[label]
        if o["generate_rc"] != n["generate_rc"]:
            out.append(
                f"[{label}] generate rc {o['generate_rc']} -> {n['generate_rc']}"
            )
        for etype in sorted(set(o["types"]) | set(n["types"])):
            ot, nt = o["types"].get(etype), n["types"].get(etype)
            if ot is None or nt is None:
                out.append(f"[{label}] {etype}: type appeared/disappeared")
                continue
            for fld in (
                "registry_entries",
                "composed_instructions",
                "composed_artifacts",
                "composed_content",
            ):
                # `.get`, never `[]`: a record written before a field existed
                # must degrade to a reported difference, not a traceback. The
                # schema guard above is the first line of defence; this is the
                # second, because forgetting to bump it is the likely slip — and
                # it is exactly the slip that happened while building this.
                if ot.get(fld) != nt.get(fld):
                    out.append(
                        f"[{label}] {etype}.{fld}: {ot.get(fld)!r} -> {nt.get(fld)!r}"
                    )
    return out


def render_text(report: dict) -> str:
    lines = [
        f"Naked-bot observation gate (#1168 Phase 3) — ref {report['ref'][:12]}",
        "",
        "A fleet declaring one bot, one expertise, and NOTHING else.",
        "",
    ]
    baseline = next(a for a in report["arms"] if a["label"] == "baseline")

    def cell(text: str, width: int) -> str:
        """Pad to *width*, or truncate with an ellipsis that SAYS it truncated.

        A silently clipped cell reads as the whole value, which is the coverage
        dishonesty this repo's guardrail names outright. The untruncated value
        is always in `--json`.
        """
        return text.ljust(width) if len(text) <= width else text[: width - 2] + "… "

    lines.append("BASELINE — what a naked bot receives")
    lines.append(
        f"  {'type':<14}{'tier':<10}{'registry':<30}{'instructions':<46}artifacts"
    )
    for etype, o in sorted(baseline["types"].items()):
        instr = o["composed_instructions"]
        shown = (
            "(no instruction surface)"
            if instr is None
            else (", ".join(instr) if instr else "-")
        )
        lines.append(
            f"  {cell(etype, 14)}{cell(o['tier'], 10)}"
            f"{cell(', '.join(o['registry_entries']) or '-', 30)}"
            f"{cell(shown, 46)}{', '.join(o['composed_artifacts']) or '-'}"
        )
    lines += ["", "OPT-OUT ARMS — does system_defaults.<type>: false remove it?"]
    for a in report["arms"]:
        if not a["label"].startswith(("optout:", "control:", "declared:")):
            continue
        instructing = (
            ",".join(
                sorted(t for t, o in a["types"].items() if o["composed_instructions"])
            )
            or "-"
        )
        lines.append(
            f"  {a['label']:<26} rc={a['generate_rc']}  instructing={instructing}"
        )
    inert = report.get("baseline_inert_defaults") or []
    lines += [
        "",
        f"freshbox --strict: rc={report['freshbox']['rc']} — "
        f"{report['freshbox']['output'].splitlines()[-1].strip() if report['freshbox']['output'] else ''}",
        "  NOTE: freshbox audits GRANTS and never opens CLAUDE.md, so it cannot",
        "  see the INSTRUCT tier. A green line above is not a clean gate.",
        "",
        "UNWIRED DEFAULTS — registry declares entries, nothing composed: "
        + (", ".join(inert) if inert else "none"),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="HEAD", help="commit to export and observe")
    ap.add_argument("--json", action="store_true", help="emit the inventory as JSON")
    ap.add_argument(
        "--baseline",
        type=Path,
        help="compare against a recorded inventory; exit 1 on drift",
    )
    ap.add_argument("--keep", action="store_true", help="keep the exported tree")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT))
    import claudlobby.defaults as registry  # noqa: PLC0415 — needs the path above

    workdir = Path(tempfile.mkdtemp(prefix="naked-bot-observe-"))
    # An export path that itself traverses `/runtime/bots/` would trip the very
    # L2 shape guard this harness exports to avoid, so refuse rather than
    # produce a run that fails for a reason unrelated to what it measures.
    if "/runtime/bots/" in f"{workdir}/":
        shutil.rmtree(workdir, ignore_errors=True)
        print(
            f"REFUSING: temp dir {workdir} sits inside a bot runtime tree; "
            "set TMPDIR elsewhere.",
            file=sys.stderr,
        )
        return 2
    try:
        root = workdir / "export"
        sha = export_tree(args.ref, root)
        _assert_compositor(root, sys.executable)

        arms = [
            observe_arm(root, sys.executable, arm, registry)
            for arm in build_arms(sorted(SURFACES))
        ]
        # Freshbox is run on the baseline arm, so recompose it before asking.
        observe_arm(
            root, sys.executable, Arm(label="baseline", system_defaults=None), registry
        )
        report = build_report(sha, arms, run_freshbox(root, sys.executable))
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"kept: {workdir}", file=sys.stderr)

    if args.baseline:
        recorded = json.loads(args.baseline.read_text())
        drift = diff_reports(recorded, report)
        if drift:
            print(f"DRIFT vs {args.baseline} ({len(drift)} difference(s)):")
            for d in drift:
                print(f"  {d}")
            return 1
        print(f"No drift vs {args.baseline}.")
        return 0

    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
