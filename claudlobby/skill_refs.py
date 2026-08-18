"""Resolution check for backticked ``/name`` references in composable content (#1253).

Every backtick-wrapped ``/name`` in ``library/`` must resolve to something a bot
can actually invoke. The check is on **resolution, not shape**, and that is the
whole design: a normaliser that rewrites ``/tech-debt`` into
``/claudna:tech-debt`` produces text that *looks* repaired while still naming a
command nobody can type. Both forms fail here, for the same reason.

Why a resolution check rather than another sweep: this class was hand-swept four
times and every pass found more than the last. A denylist of dead names has no
stopping rule, because you can only search names you already suspect.

**Stated bound, unchanged from the hand sweeps:** the predicate is pattern-based.
A reference written without backticks is invisible to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from claudlobby.known_values import CLAUDNA_LIVE_SKILLS

# A backtick span, and the first whitespace-delimited token inside it. Taking
# the FIRST token (not the whole span) is what lets the predicate see an
# invocation that carries arguments — `/claudna:audit tech-debt` is one ref, and
# a whole-span match would drop every post-consolidation verb form.
_SPAN = re.compile(r"`([^`\n]+)`")

# One leading slash, then no further slashes and no dots. That single rule is
# what excludes the false positives the first hand sweep drowned in:
# `dist/index.js` (no leading slash), `/bin/bash` and `/tmp/tmux-$(id -u)/default`
# (second slash), `/<skill-name>` (angle brackets).
_TOKEN = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9:_-]*$")

# Claude Code's own commands and bundled skills. Not fleet-local, not a plugin.
BUILTIN_COMMANDS: frozenset[str] = frozenset(
    {
        "/compact",
        "/clear",
        "/reload",
        "/mcp",
        # Ships with Claude Code. `vera`'s #1250 enumeration filed this under
        # "fleet-local, confirmed via frontmatter"; there is no
        # library/skills/simplify/, so it resolves here instead. The only
        # reclassification in an otherwise-correct seed.
        "/simplify",
    }
)

# Tokens that match the predicate but are not invocations at all. Each entry is
# a deliberate one-time declaration with the reason it is not a command —
# `vera` enumerated and reasoned this set on #1250, and it is taken as given.
EXTERNAL_ALLOWLIST: dict[str, str] = {
    "/tmp": "filesystem path",
    "/proc": "filesystem path",
    "/setprivacy": "Telegram BotFather command, documented as an external instruction",
    "/newbot": "Telegram BotFather command, documented as an external instruction",
    "/mybots": "Telegram BotFather command, documented as an external instruction",
    "/readonly": "fragment of a Linear URL pattern, not an invocation",
    "/check-runs": "GitHub check-runs API surface, contrasted with the status rollup",
}

# Genuinely broken and tracked. DELIBERATELY NOT the allowlist: an allowlist
# entry asserts "this is not an invocation", a deferral admits "this names a
# command nobody can type, and it is someone's open work". Collapsing the two
# would let real debt read as a pass, which is the failure this gate exists to
# prevent. Entries leave by being fixed.
KNOWN_UNRESOLVED: dict[str, str] = {
    "/ironclad": "bare clauDNA skill name — remediation tracked in #1250",
    "/forge": "bare clauDNA skill name — remediation tracked in #1250",
    "/claudna:snowflake-query": "no post-consolidation equivalent yet — #541 follow-up",
    "/claudna:snowflake-cutover": "no post-consolidation equivalent yet — #541 follow-up",
}


@dataclass(frozen=True)
class Resolution:
    """Why a ref does or does not resolve. ``rung`` is set only when resolved."""

    resolved: bool
    rung: str | None = None
    deferred_to: str | None = None


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    token: str
    deferred_to: str | None = None


def iter_refs(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(lineno, token)`` for every backticked ``/name`` in ``text``."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _SPAN.finditer(line):
            parts = match.group(1).split()
            if not parts:
                continue
            token = parts[0]
            if _TOKEN.match(token):
                yield lineno, token


def resolve_ref(token: str, fleet_skills: Iterable[str]) -> Resolution:
    """Resolve one ``/name`` against the three rungs, or report why it cannot be."""
    if token in KNOWN_UNRESOLVED:
        return Resolution(False, deferred_to=KNOWN_UNRESOLVED[token])
    if ":" in token:
        # Plugin-namespaced. Resolves only against the declared live set, so a
        # retired name fails even though it is correctly prefixed.
        if token in CLAUDNA_LIVE_SKILLS:
            return Resolution(True, rung="plugin-skill")
        return Resolution(False)
    if token in BUILTIN_COMMANDS:
        return Resolution(True, rung="builtin")
    if token in EXTERNAL_ALLOWLIST:
        return Resolution(True, rung="allowlist")
    if token.removeprefix("/") in set(fleet_skills):
        return Resolution(True, rung="fleet-skill")
    return Resolution(False)


def fleet_skill_names(library_dir: Path) -> frozenset[str]:
    """Frontmatter ``name:`` of every ``<library>/skills/*/SKILL.md``."""
    names: set[str] = set()
    for skill in sorted((library_dir / "skills").glob("*/SKILL.md")):
        for line in skill.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("name:"):
                names.add(line.split(":", 1)[1].strip())
                break
    return frozenset(names)


def scan_tree(tree: Path, skills: Iterable[str]) -> list[Finding]:
    """Every unresolvable ref under ``tree``, deferrals included and marked."""
    if not tree.is_dir():
        return []
    findings: list[Finding] = []
    for path in sorted(tree.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, token in iter_refs(text):
            result = resolve_ref(token, skills)
            if result.resolved:
                continue
            findings.append(
                Finding(str(path), lineno, token, deferred_to=result.deferred_to)
            )
    return findings


def scan_library(library_dir: Path) -> list[Finding]:
    """Test-level entry: scan one library tree against its own skills."""
    return scan_tree(library_dir, fleet_skill_names(library_dir))


def scan_composable(base_library: Path, overlay_library: Path | None) -> list[Finding]:
    """Scan the base library and a fleet overlay against the UNION of their skills.

    The union matters in both directions: an overlay may declare a skill that
    base content references, and base may declare one the overlay references.
    Scanning either tree against only its own skills reports the other's as
    broken.
    """
    skills = set(fleet_skill_names(base_library))
    if overlay_library and overlay_library.is_dir():
        skills |= set(fleet_skill_names(overlay_library))
    findings = scan_tree(base_library, skills)
    if overlay_library and overlay_library.resolve() != base_library.resolve():
        findings += scan_tree(overlay_library, skills)
    return findings
