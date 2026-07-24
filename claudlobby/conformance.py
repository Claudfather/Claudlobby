"""Conformance gates for the clauDNA consumed surface (boundary phase L4).

**F8 rename-map drift gate.** ``known_values.CLAUDNA_SKILL_RENAMES`` is a
hand-maintained map of dead clauDNA skill names → their live verb forms
(``/claudna:security-audit`` → ``/claudna:audit security``). Every fleet's
dead-ref guard (``tests/test_no_dead_claudna_refs.py``) and the autonomous
runner derive from that map, so a live value pointing at a skill dir clauDNA
has since renamed away becomes a dead runtime affordance in every composed bot.
This gate resolves the clauDNA the fleet composes, lists its ``skills/`` dirs,
and fails when a live value's *skill-dir token* no longer names a real dir.

**Promise — STALE-LIVE-VALUES ONLY (semantics as locked, F8 lean a).** The
map's *keys* are dead names by design; a brand-new clauDNA rename with no map
entry is undetectable here *by construction* (F8(b)'s consumed-surface manifest
is the named upgrade if one ships unnoticed). This gate catches exactly one
thing: a live value whose skill dir is gone.

**Local-first (never a required hosted dependency).** Resolving the ref needs a
clone (network). On any clone failure the gate **skips with notice** (exit 0) —
CI convenience, not a hosted dependency the suite hangs on. Point
``CLAUDNA_SKILLS_DIR`` at an existing checkout to run it fully offline; CI
checks clauDNA out and sets exactly that, so the network path is a local-run
fallback, never the CI path.

Runnable as ``python -m claudlobby.conformance rename-map``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_MARKETPLACES
from .known_values import CLAUDNA_SKILL_RENAMES

#: The clauDNA plugin namespace stripped off a live value before tokenizing.
CLAUDNA_NAMESPACE = "/claudna:"

#: Marketplace key whose repo carries clauDNA's skills (config.DEFAULT_MARKETPLACES
#: is the SSOT — the gate must clone exactly the repo the composer defaults to).
CLAUDNA_MARKETPLACE = "Claudfather"

_CLONE_TIMEOUT = 60.0  # seconds — a hung clone must skip, not pin CI


# --- pure logic (unit-testable offline) --------------------------------------


def skill_token(live_value: str) -> str:
    """The skill-dir token a live rename value invokes.

    Strip the ``/claudna:`` namespace, then take the first whitespace-delimited
    token — the engine multiplexer's dir (``/claudna:audit security`` → ``audit``,
    ``/claudna:session handoff`` → ``session``), or the whole-skill successor
    when there is no argument (``/claudna:review-work`` → ``review-work``,
    ``/claudna:using-claudna`` → ``using-claudna``).
    """
    body = live_value.removeprefix(CLAUDNA_NAMESPACE).strip()
    return body.split()[0] if body else ""


def live_value_tokens(renames: dict[str, str] | None = None) -> dict[str, str]:
    """``{live_value: skill_token}`` for every value in the rename map.

    Keyed on the live value (distinct strings; several dead names collapse onto
    one verb, e.g. eight ``…-audit`` skills → ``audit``), so a report names the
    exact value that went stale, not just the token.
    """
    renames = CLAUDNA_SKILL_RENAMES if renames is None else renames
    return {value: skill_token(value) for value in renames.values()}


def list_skill_dirs(skills_dir: Path) -> set[str]:
    """Immediate subdirectory names under a clauDNA ``skills/`` directory.

    Every dir counts (not only ``SKILL.md``-bearing ones): the gate asks "does
    the token still name a directory", and a coarser membership set only ever
    *under*-reports drift — it never invents a stale value that is really fine.
    """
    if not skills_dir.is_dir():
        return set()
    return {p.name for p in skills_dir.iterdir() if p.is_dir()}


def stale_live_values(
    existing_dirs: set[str], renames: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Live rename values whose skill-dir token is absent from *existing_dirs*.

    Returns ``[(live_value, token), …]`` sorted for stable reporting. Empty ⇒
    the map is in sync with the consumed skill surface.
    """
    return sorted(
        (value, token)
        for value, token in live_value_tokens(renames).items()
        if token not in existing_dirs
    )


# --- ref resolution + clone (the network leg; skip-safe) ---------------------


def _claudna_repo() -> str:
    """``owner/name`` of the clauDNA marketplace repo, from config's SSOT."""
    return DEFAULT_MARKETPLACES[CLAUDNA_MARKETPLACE]["source"]["repo"]


def resolve_claudna_ref(repo_root: Path | None = None) -> str | None:
    """The clauDNA ref the fleet composes: ``bot.claudna_version`` when a fleet
    pins one, marketplace-latest (``None`` → default branch) otherwise.

    ``CLAUDNA_REF`` overrides everything (the knob CI pins its checkout to). Then
    the first ``claudna_version:`` found across the repo's ``fleet.yaml*`` files
    (a light scan — the pin is a scalar). ``None`` ⇒ let the clone track the
    marketplace default branch (the unpinned ``claudna@Claudfather`` default).
    """
    env_ref = os.environ.get("CLAUDNA_REF")
    if env_ref:
        return env_ref
    if repo_root is not None:
        pin = re.compile(r"^\s*claudna_version:\s*['\"]?([^'\"\s#]+)")
        for name in ("fleet.yaml", "fleet.yaml.example", "fleet.yaml.seed"):
            fleet_yaml = repo_root / name
            if not fleet_yaml.is_file():
                continue
            for line in fleet_yaml.read_text().splitlines():
                m = pin.match(line)
                if m and m.group(1).lower() not in ("null", "~"):
                    return m.group(1)
    return None


def clone_skills_dir(ref: str | None, dest: Path) -> Path | None:
    """Shallow-clone clauDNA at *ref* into *dest*; return its ``skills/`` dir.

    Returns ``None`` on any failure (git missing, network down, bad ref, no
    ``skills/``) — the caller turns that into skip-with-notice. Never raises.
    """
    url = f"https://github.com/{_claudna_repo()}.git"
    base = ["git", "clone", "--depth", "1"]
    cmds = [[*base, "--branch", ref, url, str(dest)]] if ref else [[*base, url, str(dest)]]
    try:
        for cmd in cmds:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_CLONE_TIMEOUT
            )
            if proc.returncode != 0:
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    skills = dest / "skills"
    return skills if skills.is_dir() else None


# --- gate entry point --------------------------------------------------------


def check_rename_map(skills_dir: Path) -> tuple[int, str]:
    """Run the drift check against a resolved ``skills/`` dir → (exit_code, report)."""
    existing = list_skill_dirs(skills_dir)
    stale = stale_live_values(existing)
    if stale:
        lines = [
            "STALE rename-map live values (skill dir renamed away in clauDNA):",
            *(
                f"  {value!r}  →  skills/{token}/  (gone)"
                for value, token in stale
            ),
            "",
            "Fix: update known_values.CLAUDNA_SKILL_RENAMES to clauDNA's current "
            "verb form, or (if the skill returned) restore the dir.",
        ]
        return 1, "\n".join(lines)
    n = len(live_value_tokens())
    return 0, f"OK — all {n} rename-map live values resolve to a live clauDNA skill dir."


def _run(argv: list[str]) -> int:
    if not argv or argv[0] != "rename-map":
        print("usage: python -m claudlobby.conformance rename-map", file=sys.stderr)
        return 2

    override = os.environ.get("CLAUDNA_SKILLS_DIR")
    if override:
        skills = Path(override).expanduser()
        if not skills.is_dir():
            print(
                f"SKIP: CLAUDNA_SKILLS_DIR={override} is not a directory — "
                "cannot run the rename-map gate (local-first skip).",
            )
            return 0
        code, report = check_rename_map(skills)
        print(report)
        return code

    # No pre-provided checkout → clone (network). Skip-with-notice on any failure.
    repo_root = Path(__file__).resolve().parent.parent
    ref = resolve_claudna_ref(repo_root)
    with tempfile.TemporaryDirectory(prefix="claudna-conformance-") as tmp:
        skills = clone_skills_dir(ref, Path(tmp) / "clauDNA")
        if skills is None:
            print(
                f"SKIP: could not clone {_claudna_repo()}"
                + (f"@{ref}" if ref else " (marketplace-latest)")
                + " — the rename-map gate needs the clauDNA skill dirs. This is a "
                "local-first skip (offline / no git / bad ref), not a failure; set "
                "CLAUDNA_SKILLS_DIR to an existing checkout to run it offline.",
            )
            return 0
        code, report = check_rename_map(skills)
        print(f"(clauDNA ref: {ref or 'marketplace-latest'})")
        print(report)
        return code


def main() -> int:
    return _run(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover - module CLI
    raise SystemExit(main())
