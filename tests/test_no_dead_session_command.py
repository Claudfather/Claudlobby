"""CI guard: no dead session-skill slash command survives in a lib/ script.

clauDNA collapsed its three standalone session skills into one `/claudna:session`
engine with SPACE-form subcommands (`/claudna:session resume|handoff|checkpoint|name`);
the old spellings were hard-removed. `start-bot.sh` and `pre-stop-handoff.sh` inject
these commands as live keystrokes into the bot's Claude REPL on restart/stop, so a dead
spelling resolves to "Unknown command" and the restart silently loses its handoff (#543).

Two dead spellings must never reappear in lib/:
  - the namespaced hyphen-form `/claudna:session-resume` / `/claudna:session-handoff`, and
  - the prefix-less old standalone names `/session-resume` / `/session-handoff`.

The correct form is always `/claudna:session <verb>` — a space, never a hyphen.
"""

import re

import pytest

from tests.test_bash_parse import LIB_SCRIPTS

# Dead session-skill spellings. Scoped so it never flags the correct space-form
# `/claudna:session resume` (space, not hyphen) nor the unrelated tmux
# `session-name` resolution in lib-common.sh (bare, no leading slash).
_DEAD_SESSION_CMD = re.compile(
    r"claudna:session-|/session-(?:handoff|resume|checkpoint|name)\b"
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
