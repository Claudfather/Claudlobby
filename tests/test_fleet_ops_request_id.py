"""fleet-ops' request-ID command runs no module from the working directory (#2061).

The skill grants one exact command for minting a request UUID, so every bot
runs it without a prompt, from whatever directory its shell is in: often a
checkout of a branch under review. ``python3 -c`` puts that directory first on
the import path, and ``uuid`` is a pure-Python module, so a ``uuid.py`` there
runs in place of the standard library's. So does a ``platform.py`` on Linux,
because ``uuid`` imports ``platform`` there. Isolated mode (``-I``) keeps the
working directory off the import path and ignores ``PYTHON*`` variables.

These run the command exactly as the skill grants it, through a shell, with
whatever ``python3`` the PATH finds.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from claudlobby.loader import parse_frontmatter

SKILL = (
    Path(__file__).resolve().parent.parent
    / "library"
    / "skills"
    / "fleet-ops"
    / "SKILL.md"
)

#: What the CLI's --request-id accepts: the lowercase canonical form.
UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def _request_id_command() -> str:
    """The one granted python3 command, read from the skill's own grants."""
    frontmatter, _body = parse_frontmatter(SKILL.read_text(encoding="utf-8"))
    commands = [
        grant[len("Bash(") : -1]
        for grant in frontmatter["tool_grants"]
        if grant.startswith("Bash(python3 ")
    ]
    assert len(commands) == 1, commands
    return commands[0]


def test_the_skill_tells_bots_to_run_exactly_the_granted_command():
    """A bot runs what the paragraph shows; anything else prompts or drifts."""
    _frontmatter, body = parse_frontmatter(SKILL.read_text(encoding="utf-8"))
    assert f"```bash\n{_request_id_command()}\n```" in body


@pytest.mark.parametrize("module", ["uuid", "platform"])
def test_the_request_id_command_runs_no_module_from_the_working_directory(
    tmp_path, module
):
    ran = tmp_path / "planted-module-ran"
    (tmp_path / f"{module}.py").write_text(
        f"open({str(ran)!r}, 'w').close()\n", encoding="utf-8"
    )

    # Control: an interpreter that is not isolated imports the planted file from here.
    subprocess.run(
        f"python3 -c 'import {module}'",
        shell=True,
        cwd=tmp_path,
        check=True,
        timeout=60,
    )
    assert ran.exists(), (
        f"the planted {module}.py did not run, so this test cannot see one run"
    )
    ran.unlink()

    result = subprocess.run(
        _request_id_command(),
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not ran.exists(), (
        f"the granted command ran ./{module}.py from the working directory"
    )
    assert result.returncode == 0, result.stderr
    assert UUID4.fullmatch(result.stdout.rstrip("\n")), result.stdout
