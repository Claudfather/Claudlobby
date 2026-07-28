"""Parse gate: every shipped bash script must survive `bash -n` under the
system bash — lib/ plus any helper a library/ item ships (e.g. a skill's
executable, which is symlinked into the bot dir and run as-is).

macOS /bin/bash is bash 3.2 — the shebang target of every lib script, so a
production parser, not a relic. Its command-substitution scanner does not
strip comments inside $( ): an apostrophe there opens a string and corrupts
quoting for the rest of the file (see the warning at the bite site in
lib-common.sh bridge_state). On Linux CI this is a plain syntax gate; on
macOS it also catches the 3.2-only class.
"""

import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent


def _lib_scripts() -> list[Path]:
    """Every lib/ file that is bash: *.sh plus suffix-less scripts whose
    shebang names bash."""
    scripts = set((REPO_DIR / "lib").glob("*.sh"))
    for p in (REPO_DIR / "lib").iterdir():
        if p.is_file() and p.suffix == "":
            try:
                first = p.open("rt", errors="ignore").readline()
            except OSError:
                continue
            if first.startswith("#!") and "bash" in first:
                scripts.add(p)
    return sorted(scripts)


# lib/ only. Kept as its own set because tests/test_no_dead_session_command.py
# imports it for a guard that is deliberately lib/-scoped (it is about the
# slash commands start-bot.sh and pre-stop-handoff.sh inject as keystrokes).
# Widening the parse gate below must not silently widen that guard.
LIB_SCRIPTS = _lib_scripts()

# The parse gate's own set: lib/ plus any *.sh a library/ item ships. A skill's
# helper is symlinked into the bot dir rather than rendered, so a syntax error
# in one reaches the bot verbatim — same trust properties as lib/.
SHIPPED_SCRIPTS = sorted(set(LIB_SCRIPTS) | set((REPO_DIR / "library").rglob("*.sh")))


def _script_id(p: Path) -> str:
    # Repo-relative: bare names now collide across lib/ and library/.
    return str(p.relative_to(REPO_DIR))


@pytest.mark.parametrize("script", SHIPPED_SCRIPTS, ids=_script_id)
def test_shipped_script_parses_under_system_bash(script):
    r = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"/bin/bash -n {_script_id(script)} failed:\n{r.stderr}"
