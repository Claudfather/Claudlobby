"""Parse gate: every lib/*.sh must survive `bash -n` under the system bash.

macOS ships bash 3.2 as /bin/bash, and every lib script's shebang is
#!/bin/bash — so 3.2 is a production parser on any macOS fleet host, not a
relic. Bash 3.2's command-substitution scanner is naive: it does not strip
comments inside $( ), so an apostrophe in a $( )-internal comment opens a
string that silently inverts quoting for the rest of the file, surfacing as
a syntax error hundreds of lines later (bridge_state's "caller's" comment
broke every macOS `source` of lib-common.sh — introduced in 26500fd, caught
only because the reference fleet runs bash 5 on Linux).

On Linux CI /bin/bash is modern, so there this is a plain syntax gate; the
3.2-specific protection bites on macOS dev machines and hosts. Both are
worth having, and the failure mode (fleet-wide, silent until sourced) more
than earns the ~100ms this costs.
"""

import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent


def _bash_scripts() -> list[Path]:
    """Every lib/ file that is bash: *.sh plus suffix-less scripts whose
    shebang names bash (setup-system, setup-fleet, setup-fleets)."""
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
