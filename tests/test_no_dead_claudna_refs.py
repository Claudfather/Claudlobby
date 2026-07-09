"""Guard: no renamed-away clauDNA skill names in composable/doc/script content (#541).

The clauDNA #165 consolidation collapsed the hyphenated skills into positional
multiplexers (``/claudna:audit security``, ``/claudna:session handoff``, …)
with no aliases. Library fragments compose into live bot CLAUDE.md files and
lib/ scripts inject skill keystrokes into live sessions, so a dead name here
becomes a dead runtime affordance in every fleet. Both the plugin-namespaced
form (``/claudna:security-audit``) and the bare doc-idiom form
(``/security-audit``) are banned.
"""

import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent

# (directory, glob) pairs — .j2 covered so template regressions fail too.
SCAN_GLOBS = (
    ("library", "*.md"),
    ("documentation", "*.md"),
    ("templates", "*.j2"),
    ("lib", "*.sh"),
)

# Deferred #541 follow-ups (non-mechanical) and #543 (alex's in-flight lib/
# session-ref fix). Drop entries as those land; the guard then covers them.
EXCLUDE_FILES = (
    # #541 follow-up: coupled to known_values.AUTO_ELIGIBLE_SKILLS decision
    "library/skills/autonomous-runner/SKILL.md",
    "library/skills/autonomous-runner/archetype.md",
    # #541 follow-up: snowflake skills have no post-consolidation equivalent
    "library/integrations/snowflake.md",
    # #543: lib/ session-ref renames (alex)
    "lib/start-bot.sh",
    "lib/pre-stop-handoff.sh",
    "lib/validate-bot-change.sh",
    "lib/lib-common.sh",
)

# Files allowed to keep /snowflake-* refs only (no rename target exists yet —
# #541 follow-up). Every other dead name in these files still fails the guard.
SNOWFLAKE_DEFERRED = (
    "documentation/integrations.md",
    "documentation/bot-archetypes.md",
)

DEAD_REF = re.compile(
    r"/(?:claudna:)?(?:"
    r"(?:vercel|neon|railway|modal)-[a-z-]+"
    r"|snowflake-(?:query|cutover)"
    r"|security-audit|tech-debt|docs-review|data-model-audit|design-review"
    r"|frontend-performance-audit|access-path-audit|repo-health"
    r"|session-handoff|session-resume|name-session"
    r"|review-pr|review-changes|skill-health|product-brainstorm"
    r")\b"
)


def test_no_dead_claudna_skill_refs():
    offenders = []
    for base, pattern in SCAN_GLOBS:
        for path in sorted((REPO_DIR / base).rglob(pattern)):
            rel = path.relative_to(REPO_DIR).as_posix()
            if rel in EXCLUDE_FILES or rel.startswith("documentation/plans/"):
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                match = DEAD_REF.search(line)
                if not match:
                    continue
                if "snowflake-" in match.group(0) and rel in SNOWFLAKE_DEFERRED:
                    continue
                offenders.append(f"{rel}:{lineno}: {match.group(0)}")
    assert not offenders, (
        "renamed-away clauDNA skill names found (see #541 rename map):\n  "
        + "\n  ".join(offenders)
    )
