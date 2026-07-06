"""Parse gate: every lib/ bash script must survive `bash -n` under the
system bash.

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


def _bash_scripts() -> list[Path]:
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


LIB_SCRIPTS = _bash_scripts()


@pytest.mark.parametrize("script", LIB_SCRIPTS, ids=lambda p: p.name)
def test_lib_script_parses_under_system_bash(script):
    r = subprocess.run(
        ["/bin/bash", "-n", str(script)], capture_output=True, text=True
    )
    assert r.returncode == 0, f"/bin/bash -n {script.name} failed:\n{r.stderr}"
