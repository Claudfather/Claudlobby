"""A failed step's reason keeps the first 300 bytes of its detail, cleaned as the
manager's pane shows it (#3107).

step_failure_reason (lib-common.sh) words a failed step as `<label> failed (exit
<rc>): <detail>`, the detail being the last non-blank line the step printed, or
for exit 127 the command and the PATH it was not found on. It is the reason the
daily reload's reload_failed alert and a bot start's plugin_update_failed alert
carry to the manager's pane and to Telegram, and Telegram refuses a message over
4,096 characters. So the detail is cleaned by sanitize_tmux_input, the cleaning
a pane send applies to its text, loses the space that cleaning can leave at
either end, and keeps its first 300 bytes, never part of a character.

Run through bash with lib-common sourced, under a UTF-8 locale and under
LC_ALL=C: the cut counts bytes under either.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess

import pytest

from tests.conftest import constructed_env

LIB = Path(__file__).resolve().parent.parent / "claudlobby" / "_runtime_scripts"
LABEL = "claude plugin update alpha@Market"
CUT = 300
LONG = b"x" * 5000
E_ACUTE = b"\xc3\xa9"            # a two-byte character
GRIN = b"\xf0\x9f\x98\x80"       # a four-byte character

# The step's output, its exit status, and the detail its reason ends with.
CASES = {
    "a-short-clean-line": (b'Checking for updates\nPlugin "alpha" not found\n\n', 3,
                           b'Plugin "alpha" not found'),
    "a-long-line": (LONG + b"\n", 1, b"x" * CUT),
    "colour-and-control-bytes": (b"\x1b[31merror\x1b[0m:\tplugin\x07 not\x7ffound\r\n", 1,
                                 b"error: plugin not found"),
    "spaces-at-either-end": (b"\t\t  indented  error\t\r\n", 1, b"indented error"),
    "a-space-at-the-cut": (b"a" * (CUT - 1) + b" and more\n", 1, b"a" * (CUT - 1)),
    "the-cut-counts-the-cleaned-text": (b"\x1b[1m" * 100 + LONG + b"\n", 1, b"x" * CUT),
    "a-character-ending-at-the-cut": (b"a" * (CUT - 2) + E_ACUTE + b"zz\n", 1,
                                      b"a" * (CUT - 2) + E_ACUTE),
    "a-two-byte-character-across-the-cut": (b"a" * (CUT - 1) + E_ACUTE + b"zz\n", 1, b"a" * (CUT - 1)),
    "a-four-byte-character-across-the-cut": (b"a" * (CUT - 3) + GRIN + b"zz\n", 1, b"a" * (CUT - 3)),
    "nothing-but-control-bytes": (b"\x1b[0m\n", 1, b""),
}
LOCALES = {"utf-8": {}, "lc-all-c": {"LC_ALL": "C"}}

# Prints the reason, a newline, and the reason as a pane send cleans it.
SCRIPT = ('. "$LIB/lib-common.sh"\n'
          'reason=$(step_failure_reason "$LABEL" "$RC" claude)\n'
          "printf '%s\\n%s' \"$reason\" \"$(sanitize_tmux_input \"$reason\")\"\n")


def _reason(output: bytes, rc: int, **env) -> tuple[bytes, bytes]:
    result = subprocess.run(["bash", "-c", SCRIPT], input=output, capture_output=True, timeout=60,
                            env=constructed_env(LIB=LIB, LABEL=LABEL, RC=str(rc), **env))
    assert result.returncode == 0, result.stderr
    reason, _, pane = result.stdout.partition(b"\n")
    return reason, pane


def _expected(rc: int, detail: bytes) -> bytes:
    return f"{LABEL} failed (exit {rc})".encode() + (b": " + detail if detail else b"")


@pytest.mark.parametrize("locale", sorted(LOCALES))
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_reason_keeps_the_first_300_bytes_of_its_detail_cleaned(case, locale):
    output, rc, detail = CASES[case]
    reason, pane = _reason(output, rc, **LOCALES[locale])
    assert reason == _expected(rc, detail)
    # The manager's pane shows what the chat shows: the pane's own cleaning
    # leaves the reason as it is.
    assert pane == reason


@pytest.mark.parametrize("locale", sorted(LOCALES))
@pytest.mark.parametrize("extra", [0, 400], ids=["the-path-whole", "a-path-past-the-cut"])
def test_exit_127_names_the_command_and_the_path_within_the_cut(extra, locale):
    """The PATH is the detail for exit 127, whatever the step printed, and the
    cut applies to it as to a printed line."""
    path = "/usr/bin:/bin" + (":/absent/" + "d" * extra if extra else "")
    reason, pane = _reason(b"claude: command not found\n", 127, PATH=path, **LOCALES[locale])
    detail = f"claude not found on PATH={path}".encode()
    assert reason == _expected(127, detail[:CUT])
    assert (len(detail) > CUT) is bool(extra)
    assert pane == reason


def test_a_megabyte_line_is_cut_well_inside_ten_seconds():
    """The line can be any length. Under a UTF-8 locale bash took seconds to
    strip a space from a 100 KB line with ${detail# } or ${detail% }, so the
    leading space goes by a case test and the cut comes before the trailing
    one: a megabyte took about a third of a second on a Raspberry Pi. The run
    is its own process group, so the cap ends all of it."""
    script = '. "$LIB/lib-common.sh"\nstep_failure_reason "$LABEL" 1 claude\n'
    run = subprocess.Popen(["bash", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, start_new_session=True,
                           env=constructed_env(LIB=LIB, LABEL=LABEL))
    try:
        out, err = run.communicate(b"x" * 1_000_000 + b"\n", timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(run.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        run.communicate()
        pytest.fail("the reason for a megabyte line took over 10 s")
    assert run.returncode == 0, err
    assert out == _expected(1, b"x" * CUT)
