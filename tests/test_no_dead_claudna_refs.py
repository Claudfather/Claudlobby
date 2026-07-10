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

from claudlobby.known_values import CLAUDNA_SKILL_RENAMES

REPO_DIR = Path(__file__).resolve().parent.parent

# (directory, glob) pairs — .j2 covered so template regressions fail too.
SCAN_GLOBS = (
    ("library", "*.md"),
    ("documentation", "*.md"),
    ("templates", "*.j2"),
    ("lib", "*.sh"),
)

# Files allowed to keep /snowflake-* refs only: snowflake skills have no
# post-consolidation equivalent, so those refs have no rename target yet
# (#541 follow-up). Every OTHER dead name in these files still fails the guard —
# a strictly tighter deferral than a whole-file skip.
SNOWFLAKE_DEFERRED = (
    "library/integrations/snowflake.md",
    "documentation/integrations.md",
    "documentation/bot-archetypes.md",
)

# Enumerated dead names derive from the canonical rename map, so this guard
# can never drift from known_values.py or the session guard (#570). Guard-local
# pattern families stay here: the integration CLI names are prefix globs, never
# enumerated renames, and `session-[a-z-]+` bans hyphen-typos of the live
# space-form session verbs (e.g. /claudna:session-checkpoint) that have no
# dead ancestor in the map.
_DEAD_NAMES = "|".join(
    sorted(re.escape(k.removeprefix("/claudna:")) for k in CLAUDNA_SKILL_RENAMES)
)
DEAD_REF = re.compile(
    r"/(?:claudna:)?(?:"
    r"(?:vercel|neon|railway|modal)-[a-z-]+"
    r"|snowflake-(?:query|cutover)"
    r"|session-[a-z-]+"
    rf"|{_DEAD_NAMES}"
    r")\b"
)


def test_no_dead_claudna_skill_refs():
    offenders = []
    for base, pattern in SCAN_GLOBS:
        for path in sorted((REPO_DIR / base).rglob(pattern)):
            rel = path.relative_to(REPO_DIR).as_posix()
            if rel.startswith("documentation/plans/"):
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
