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

import json
import shutil
from datetime import datetime, timezone
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

    ``CLAUDNA_REF`` overrides local clone resolution; the rename-map CI lane
    uses marketplace-latest. The separate export-contract lane pins its checkout
    with ``contracts/claudna.ref``. Then
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


def export_schema_errors(value, schema: dict) -> list[str]:
    """Offline required/type contract floor, including local refs and nested items.

    This is intentionally the keyword subset needed by the vendored export
    schema. The connected conformance leg also uses the producer's validator.
    """
    errors = []
    def walk(value, rule, path):
        if "$ref" in rule:
            target = schema
            for key in rule["$ref"].removeprefix("#/").split("/"):
                target = target[key]
            return walk(value, target, path)
        kinds = rule.get("type")
        kinds = [kinds] if isinstance(kinds, str) else kinds
        matches = {"null": value is None, "object": isinstance(value, dict),
                   "array": isinstance(value, list), "string": isinstance(value, str),
                   "integer": type(value) is int, "number": type(value) in (int, float),
                   "boolean": type(value) is bool}
        if kinds and not any(matches.get(k, False) for k in kinds):
            errors.append(f"{path}: expected {kinds}")
            return
        if "const" in rule and value != rule["const"]:
            errors.append(f"{path}: wrong const")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{path}: wrong enum")
        if isinstance(value, dict):
            for key in rule.get("required", []):
                if key not in value:
                    errors.append(f"{path}.{key}: required")
            for key, child in value.items():
                child_rule = rule.get("properties", {}).get(key, rule.get("additionalProperties"))
                if isinstance(child_rule, dict):
                    walk(child, child_rule, f"{path}.{key}")
        if isinstance(value, list) and "items" in rule:
            for i, child in enumerate(value):
                walk(child, rule["items"], f"{path}[{i}]")
    walk(value, schema, "$")
    return errors


def check_export_contract(clone: Path) -> tuple[int, str]:
    """Real released export door over a capture-backed copied store; no network."""
    source = clone / "lib/claudna/session_store"
    if not clone.is_dir():
        return 0, f"SKIP: clauDNA checkout {clone} is absent (local-first export-contract gate)"
    vendored = Path(__file__).parent / "contracts/claudna-export.schema.json"
    try:
        upstream = source / "schemas/export.schema.json"
        if upstream.read_bytes() != vendored.read_bytes():
            return 1, "FAIL: vendored clauDNA export schema differs from checkout; review the ref/schema/fixture move together"
        fixture = Path(__file__).parent.parent / "tests/fixtures/claudna_export/store"
        with tempfile.TemporaryDirectory(prefix="claudna-export-contract-") as tmp:
            store = Path(tmp) / "store"
            shutil.copytree(fixture, store)
            proc = subprocess.run([sys.executable, "-S", str(source), "export", "--root", str(store),
                                   "--consumer", "claudlobby", "--include-skipped", "--json"],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode:
                return 1, f"FAIL: clauDNA export exited {proc.returncode}: {proc.stderr[:1000]}"
            envelope = json.loads(proc.stdout)
            schema = json.loads(vendored.read_text())
            errors = export_schema_errors(envelope, schema)
            # Import the clone's own validator in its own process; never mutate
            # this process's claudna import path or trust a producer test fixture.
            validate = subprocess.run([sys.executable, "-S", "-c",
                "import sys,json;sys.path.insert(0,sys.argv[1]);from claudna.session_store.schema import validate;"
                "errors=validate(json.load(sys.stdin),json.load(open(sys.argv[2])));"
                "print(json.dumps(errors));sys.exit(bool(errors))", str(clone / "lib"), str(vendored)],
                input=proc.stdout, capture_output=True, text=True, timeout=60)
            if errors or validate.returncode:
                return 1, f"FAIL: export envelope schema: {errors}; producer validator: {validate.stdout} {validate.stderr}"
            from .session_export import summary_record
            for item in envelope["items"]:
                summary_record(item, fleet="contract", bot="capture", now=datetime.now(timezone.utc))
            statuses = {"ok" if i["summary"] is not None else "skipped" for i in envelope["items"]}
            if statuses != {"ok", "skipped"}:
                return 1, "FAIL: captured store must exercise both ok and skipped export items"
            return 0, f"OK: export-contract: {len(envelope['items'])} captured items validate and map through summary_record"
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        return 1, f"FAIL: export-contract: {exc}"


def _run(argv: list[str]) -> int:
    if argv and argv[0] == "export-contract":
        if len(argv) != 2:
            print("usage: python -m claudlobby.conformance export-contract <clone root>", file=sys.stderr)
            return 2
        code, report = check_export_contract(Path(argv[1]))
        print(report)
        return code
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
