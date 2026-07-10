"""CI guard: no dead session-skill slash command survives in a lib/ script.

clauDNA collapsed its three standalone session skills into one `/claudna:session`
engine with SPACE-form subcommands (`/claudna:session resume|handoff|checkpoint|name`);
the old spellings were hard-removed. `start-bot.sh` and `pre-stop-handoff.sh` inject
these commands as live keystrokes into the bot's Claude REPL on restart/stop, so a dead
spelling resolves to "Unknown command" and the restart silently loses its handoff (#543).

Two dead spelling classes must never reappear in lib/:
  - any namespaced hyphen-form `/claudna:session-<verb>` — a rename or a typo of
    the live space-form alike, and
  - the renamed-away standalone names from the canonical map (`/session-handoff`,
    `/session-resume`, `/name-session`), bare or namespaced.

The correct form is always `/claudna:session <verb>` — a space, never a hyphen.
"""

import re

import pytest

from claudlobby.known_values import CLAUDNA_SKILL_RENAMES
from tests.test_bash_parse import LIB_SCRIPTS

# The enumerated alternation derives from the canonical rename map, so this
# guard can never drift from the doc guard again (#570). The `claudna:session-`
# catch-all is guard-local pattern policy: it also bans hyphen-typos of live
# verbs that were never skills (session-checkpoint, session-name). Scoped so it
# never flags the correct space-form `/claudna:session resume` nor the
# unrelated tmux `session-name` resolution in lib-common.sh (bare, no leading
# slash).
_DEAD_SESSION_NAMES = "|".join(
    sorted(
        re.escape(k.removeprefix("/claudna:"))
        for k, live in CLAUDNA_SKILL_RENAMES.items()
        if live.startswith("/claudna:session ")
    )
)
_DEAD_SESSION_CMD = re.compile(
    rf"claudna:session-|/(?:claudna:)?(?:{_DEAD_SESSION_NAMES})\b"
)


@pytest.mark.parametrize("script", LIB_SCRIPTS, ids=lambda p: p.name)
def test_no_dead_session_skill_ref(script):
    hits = [
        f"  {script.name}:{i}: {line.strip()}"
        for i, line in enumerate(script.read_text().splitlines(), 1)
        if _DEAD_SESSION_CMD.search(line)
    ]
    assert not hits, (
        "Dead session-skill slash command in lib/ — restart/resume injects these "
        "as live keystrokes, so the hyphen/old form loses the handoff (#543). "
        "Use the space-form `/claudna:session <verb>`:\n" + "\n".join(hits)
    )
