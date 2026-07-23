"""Lessons migration (L3, boundary re-architecture).

Migrate the *referential* subset of ``library/lessons/`` — incident residue,
environment facts, domain knowledge — into the Claudron vault through the typed
write door (``claudron capture --type knowledge --stdin --json``). Behavior-class
lessons (imperative bot-steering) are NOT migrated: a vault pointer cannot steer
behavior, so they re-home to ``library/protocols/`` / ``library/guardrails/`` and
keep rendering in-context. The per-note verdicts are the triage ledger at
``documentation/plans/2026-07-23-l3-lessons-triage-ledger.md``; the classification
below is that ledger's machine mirror (a test asserts the two never drift).

Dry-run by default (prints the capture plan, needs no ``claudron``); ``--apply``
shells the write door per note and branches on ``data.action`` per
docs/CLI_CONTRACT.md §capture — ``created``/``updated`` land, ``suggest_*`` are
listed for a human (NEVER ``--force``), ``rejected`` is an error. Idempotent:
re-running ``--apply`` dedup-routes every note to ``suggest_update``.
"""

from __future__ import annotations

import json as _json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._helpers import _resolve_paths

log = logging.getLogger("claudlobby")

# ---------------------------------------------------------------------------
# The triage classification — machine mirror of the L3 ledger.
#
# REFERENTIAL: migrate to the vault. Value = extra recall tags beyond the
# auto-derived ['lesson', <topic-subdir>].
# BEHAVIOR: do NOT migrate. Value = the protocol/guardrail home that renders it
# in-context (a note whose behavior an existing surface already carries points
# at that surface).
#
# Paths are relative to library/lessons/, without a leading ``./``. README.md is
# excluded structurally (not a note). Every other *.md MUST appear in exactly one
# map — _classify() raises on any unclassified file so a new lesson can't slip
# through the freeze silently.
# ---------------------------------------------------------------------------

REFERENTIAL_LESSONS: dict[str, list[str]] = {
    "dbt/dim-first-architecture.md": [],
    "dbt/incremental-unique-key-discipline.md": [],
    "dbt/parse-vs-execute-time.md": [],
    "dbt/semantic-layer-discipline.md": [],
    "design/addition-earns-place.md": [],
    "design/whitespace-earns-weight.md": [],
    "migration/dotenv-export-prefix.md": ["env"],
    "migration/preserve-existing-env.md": ["env"],
    "migration/tmux-server-env-inheritance.md": ["tmux", "env"],
    "railway/fail-loud.md": [],
    "raspberry-pi/sdhci-uhs-quirk.md": ["hardware"],
    "review/empirical-verification.md": [],
    "review/mutation-testing-default.md": [],
    "review/root-cause-not-symptom.md": [],
    "review/stacked-pr-squash-corruption.md": ["git"],
    "snowflake/clustering-earns-its-cost.md": [],
    "snowflake/transient-table-recovery.md": [],
    "telegram/mcp-drops.md": [],
    "telegram/orphaned-poller-single-consumer.md": [],
    "telegram/plain-text-escape-incident.md": [],
    "private-repo-screenshots.md": ["github", "screenshots"],
    "telegram-bot-group-setup.md": ["telegram", "setup"],
}

# Behavior-class → re-homed; kept for the non-vault fallback + provenance.
BEHAVIOR_LESSONS: dict[str, str] = {
    "messaging-channel-discipline.md": "protocols/messaging-channel-discipline (new)",
    "tmux-dispatch-shell-expansion.md": "protocols/dispatch (already carries `set +H;`)",
    "orchestration/consensus-before-escalation.md": "protocols/consensus-loop (already carries the loop)",
}


@dataclass
class CaptureMapping:
    """One referential note → one ``claudron capture`` invocation."""

    source: str  # relpath under library/lessons/
    title: str
    body: str
    tags: list[str]
    fleet: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def strict_valid(self) -> bool:
        return not self.problems

    def stdin_payload(self) -> dict:
        """The ``--stdin`` JSON envelope (docs/CLI_CONTRACT.md §capture fields)."""
        return {
            "type": "knowledge",
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
        }

    def capture_argv(self, claudron_bin: str) -> list[str]:
        argv = [claudron_bin, "capture", "--type", "knowledge", "--stdin", "--json"]
        if self.fleet:
            argv += ["--fleet", self.fleet]
        return argv


def _all_lesson_files(lessons_dir: Path) -> list[str]:
    """Every ``*.md`` under lessons_dir except README.md, as sorted relpaths."""
    out: list[str] = []
    for p in lessons_dir.rglob("*.md"):
        if p.name == "README.md":
            continue
        out.append(p.relative_to(lessons_dir).as_posix())
    return sorted(out)


def _classify(lessons_dir: Path) -> list[str]:
    """Assert every lesson file is classified; return the unclassified relpaths.

    An empty return is the contract the freeze relies on: no note escapes a
    verdict. Callers treat a non-empty list as a hard error.
    """
    known = set(REFERENTIAL_LESSONS) | set(BEHAVIOR_LESSONS)
    return [rel for rel in _all_lesson_files(lessons_dir) if rel not in known]


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a note into (frontmatter dict, body). Tolerant of a missing block."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, parts[2].lstrip("\n")
    return {}, text


def _derive_tags(relpath: str, meta: dict, extra: list[str]) -> list[str]:
    """['lesson'] + <topic-subdir> + frontmatter tags + curated extras, deduped."""
    tags = ["lesson"]
    if "/" in relpath:
        tags.append(relpath.split("/", 1)[0])
    fm_tags = meta.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]
    for t in [*fm_tags, *extra]:
        t = str(t).strip()
        if t and t not in tags:
            tags.append(t)
    return tags


def build_capture_plan(lessons_dir: Path, fleet: str | None = None) -> list[CaptureMapping]:
    """One CaptureMapping per referential-verdict note. Pure; needs no claudron."""
    plan: list[CaptureMapping] = []
    for relpath, extra in sorted(REFERENTIAL_LESSONS.items()):
        note = lessons_dir / relpath
        problems: list[str] = []
        if not note.is_file():
            plan.append(
                CaptureMapping(
                    source=relpath, title="", body="", tags=["lesson"],
                    fleet=fleet, problems=[f"missing file: {note}"],
                )
            )
            continue
        meta, body = _split_frontmatter(note.read_text())
        title = str(meta.get("title") or "").strip()
        body = body.strip()
        tags = _derive_tags(relpath, meta, extra)
        if not title:
            problems.append("empty title")
        if not body:
            problems.append("empty body")
        if "lesson" not in tags:
            problems.append("missing 'lesson' tag")
        plan.append(
            CaptureMapping(
                source=relpath, title=title, body=body, tags=tags,
                fleet=fleet, problems=problems,
            )
        )
    return plan


def _apply_one(
    mapping: CaptureMapping, claudron_bin: str, vault: Path
) -> tuple[str, str]:
    """Shell one ``claudron capture``. Returns (action, detail).

    action ∈ {created, updated, suggest_update, suggest_supersede, rejected,
    error}. Branches on ``data.action`` (never the exit code); ``error`` covers a
    non-JSON / crashed invocation. NEVER passes --force.
    """
    env = {**os.environ, "CLAUDRON_VAULT_PATH": str(vault)}
    try:
        proc = subprocess.run(
            mapping.capture_argv(claudron_bin),
            input=_json.dumps(mapping.stdin_payload()),
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        return "error", f"claudron not found: {claudron_bin!r}"
    try:
        env_json = _json.loads(proc.stdout)
    except _json.JSONDecodeError:
        return "error", (proc.stderr or proc.stdout or "no output").strip()[:200]
    data = env_json.get("data") or {}
    action = data.get("action") or ("rejected" if not env_json.get("ok") else "error")
    detail = data.get("reason") or data.get("path") or ""
    return action, str(detail)


def cmd_lessons_migrate(args) -> int:
    """Migrate referential lessons into the Claudron vault via the write door.

    Dry-run by default (prints the capture plan); ``--apply`` writes through
    ``claudron capture``. Behavior-class lessons are never migrated.
    """
    paths = _resolve_paths(args)
    lessons_dir = paths.base_lessons
    if not lessons_dir.is_dir():
        log.error("no lessons directory at %s", lessons_dir)
        return 1

    unclassified = _classify(lessons_dir)
    if unclassified:
        log.error(
            "unclassified lesson(s) — add a triage verdict (referential→migrate, "
            "behavior→re-home) before migrating: %s",
            ", ".join(unclassified),
        )
        return 1

    fleet = getattr(args, "fleet_scope", None)
    plan = build_capture_plan(lessons_dir, fleet=fleet)
    tier = f"fleet '{fleet}'" if fleet else "_shared/"

    log.info("=== lessons-migrate plan ===")
    log.info("lessons dir: %s", lessons_dir)
    log.info("target tier: %s", tier)
    log.info(
        "referential (migrate): %d · behavior (re-home, kept): %d",
        len(plan), len(BEHAVIOR_LESSONS),
    )

    invalid = [m for m in plan if not m.strict_valid]
    for m in plan:
        flag = "  OK " if m.strict_valid else "FAIL "
        log.info("%s %-42s → knowledge (tags: %s)", flag, m.source, ",".join(m.tags))
        log.info("        title: %s", m.title or "(none)")
        if not m.strict_valid:
            log.warning("        problems: %s", "; ".join(m.problems))

    log.info("behavior-class (NOT migrated — re-homed, retained for fallback):")
    for src, home in sorted(BEHAVIOR_LESSONS.items()):
        log.info("  --  %-42s → %s", src, home)

    if invalid:
        log.error("%d mapping(s) are not strict-valid — fix before --apply", len(invalid))
        return 1

    if not args.apply:
        log.info(
            "%d referential note(s) map cleanly. (dry-run — pass --apply to write "
            "to the vault via `claudron capture`)", len(plan),
        )
        return 0

    # --- apply ---
    vault_str = getattr(args, "vault", None) or os.environ.get("CLAUDRON_VAULT_PATH")
    if not vault_str:
        log.error("--apply requires --vault <path> (or CLAUDRON_VAULT_PATH)")
        return 2
    vault = Path(vault_str).expanduser()
    if not vault.is_dir():
        log.error("vault path is not a directory: %s", vault)
        return 2
    claudron_bin = getattr(args, "claudron_bin", None) or "claudron"

    written = 0
    suggested: list[tuple[str, str]] = []
    rejected: list[tuple[str, str]] = []
    for m in plan:
        action, detail = _apply_one(m, claudron_bin, vault)
        if action in ("created", "updated"):
            written += 1
            log.info("  %-9s %s", action, m.source)
        elif action in ("suggest_update", "suggest_supersede"):
            suggested.append((m.source, detail))
            log.info("  %-9s %s — %s", action, m.source, detail)
        else:  # rejected / error
            rejected.append((m.source, detail))
            log.error("  %-9s %s — %s", action, m.source, detail)

    log.info(
        "applied: %d written, %d suggested (human review), %d rejected/error",
        written, len(suggested), len(rejected),
    )
    if suggested:
        log.info("suggest_* (already-present dedup routes — review, do not --force):")
        for src, detail in suggested:
            log.info("  %s — %s", src, detail)
    return 1 if rejected else 0
