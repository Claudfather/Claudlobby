"""usage-limit.py (#996): the reader keepalive asks about a pane a usage limit may hold.

The frames under tests/fixtures/pane-states/usage-limit-*.txt are live captures
from Claude Code (2.1.292, and 2.1.291 for the armed one), run with the bots'
renderer, flags, 80x24 pane and New York zone against a stand-in API that
answered 429 with the unified rate-limit headers; only the cwd line is
scrubbed. The variants below them are those frames edited to the shapes a live
run cannot be made to draw on demand (a server flag that puts credits first,
another menu over a limit).
"""

from __future__ import annotations

import datetime
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "claudlobby/_runtime_scripts/usage-limit.py"
FIXTURES = REPO / "tests/fixtures/pane-states"

_spec = importlib.util.spec_from_file_location("usage_limit", SCRIPT)
ul = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ul)

NY = "America/New_York"


def _epoch(iso: str) -> int:
    return int(datetime.datetime.fromisoformat(iso).timestamp())


def _frame(name: str) -> str:
    return (FIXTURES / f"usage-limit-{name}.txt").read_text()


# --- the reset time, in each form Claude Code prints it ----------------------------


@pytest.mark.parametrize(
    "text,anchor,tz,want",
    [
        # Within 24 hours: the time alone, with the zone the process ran in.
        (
            "10:50pm (America/New_York)",
            "2026-10-07T01:46:16+00:00",
            None,
            "2026-10-07T02:50:00+00:00",
        ),
        (
            "10:36am (America/New_York)",
            "2026-10-07T14:34:20+00:00",
            None,
            "2026-10-07T14:36:00+00:00",
        ),
        # Minutes are drawn only when they are not :00.
        (
            "3am (America/New_York)",
            "2026-10-07T05:00:00+00:00",
            None,
            "2026-10-07T07:00:00+00:00",
        ),
        # No zone printed: the bot's TZ decides.
        ("3am", "2026-10-07T05:00:00+00:00", NY, "2026-10-07T07:00:00+00:00"),
        ("2:50am", "2026-10-07T05:00:00+00:00", "UTC", "2026-10-08T02:50:00+00:00"),
        # Midnight and noon.
        ("12am (UTC)", "2026-10-07T20:00:00+00:00", None, "2026-10-08T00:00:00+00:00"),
        ("12pm (UTC)", "2026-10-07T09:00:00+00:00", None, "2026-10-07T12:00:00+00:00"),
        # A reset a few minutes before the first tick that saw it (the bot sat
        # busy, or keepalive was a tick behind) is still that reset, not tomorrow's.
        (
            "10:36am (America/New_York)",
            "2026-10-07T14:40:00+00:00",
            None,
            "2026-10-07T14:36:00+00:00",
        ),
        # Beyond 24 hours: the date too.
        (
            "Oct 8, 2:50am (America/New_York)",
            "2026-10-07T01:00:00+00:00",
            None,
            "2026-10-08T06:50:00+00:00",
        ),
        (
            "Oct 9, 4pm (America/New_York)",
            "2026-10-07T01:00:00+00:00",
            None,
            "2026-10-09T20:00:00+00:00",
        ),
        # The year only when it is not this one, and a date past New Year without it.
        (
            "Jan 2, 2027, 3am (UTC)",
            "2026-12-30T00:00:00+00:00",
            None,
            "2027-01-02T03:00:00+00:00",
        ),
        (
            "Jan 2, 3am (UTC)",
            "2026-12-30T00:00:00+00:00",
            None,
            "2027-01-02T03:00:00+00:00",
        ),
        # A zone with three parts, and one with a sign.
        (
            "9am (America/Argentina/Buenos_Aires)",
            "2026-10-07T10:00:00+00:00",
            None,
            "2026-10-07T12:00:00+00:00",
        ),
        (
            "9am (Etc/GMT+5)",
            "2026-10-07T10:00:00+00:00",
            None,
            "2026-10-07T14:00:00+00:00",
        ),
    ],
)
def test_the_reset_time_parses_in_each_form_it_takes(text, anchor, tz, want):
    assert ul.reset_epoch(text, _epoch(anchor), tz) == _epoch(want)


@pytest.mark.parametrize(
    "text",
    [
        "soon",
        "",
        "Oct 8",
        "13pm",
        "0am",
        "25:00",
        "10:61am",
        "Feb 30, 3am (UTC)",
        "10:50pm (Not/A_Zone) extra",
        "in 3 hours",
    ],
)
def test_a_reset_it_cannot_read_is_none(text):
    # None is fail-closed: keepalive reads LIMIT with no reset, and sends no key.
    assert ul.reset_epoch(text, _epoch("2026-10-07T00:00:00+00:00"), NY) is None


def test_the_printed_minute_plus_sixty_seconds_is_never_before_the_true_reset():
    """The print drops the seconds, so keepalive waits for the printed minute plus
    60 s: never early, whatever the second the limit really resets at."""
    for second in range(60):
        true_reset = _epoch(f"2026-10-07T14:36:{second:02d}+00:00")
        printed = datetime.datetime.fromtimestamp(true_reset, datetime.timezone.utc)
        text = printed.strftime("%-I:%M%p").lower() + " (UTC)"
        got = ul.reset_epoch(text, true_reset - 120)
        assert got is not None and got <= true_reset <= got + 60, (second, text, got)


# --- the pane -----------------------------------------------------------------------


def _read(text: str, anchor: str = "2026-10-07T14:34:20+00:00", tz: str = NY):
    return ul.read_pane(text, _epoch(anchor), tz)


def test_the_bots_frame_reads_limit_with_its_reset():
    """No menu and no countdown: what --remote-control leaves (measured, 2.1.292)."""
    state, reset, pointer, native, name, text = _read(_frame("held"))
    assert (state, pointer, native, name, text) == (
        "limit",
        "-",
        "-",
        "session limit",
        "10:36am (America/New_York)",
    )
    assert int(reset) == _epoch("2026-10-07T14:36:00+00:00")


def test_claude_codes_own_armed_continue_is_seen():
    state, _r, _p, native, _n, text = _read(
        _frame("native-armed"), "2026-10-07T04:31:00+00:00"
    )
    assert (state, native, text) == ("limit", "armed", "12:34am (America/New_York)")


def test_the_usage_limit_menu_reads_menu_with_the_pointer_on_the_wait_option():
    state, _r, pointer, _nat, _n, text = _read(
        _frame("menu"), "2026-10-07T14:43:00+00:00"
    )
    assert (state, pointer, text) == ("menu", "wait", "10:45am (America/New_York)")


def test_the_box_back_after_the_menu_reads_limit():
    state, *_ = _read(_frame("menu-closed"), "2026-10-07T14:43:00+00:00")
    assert state == "limit"


def test_a_resumed_bot_reads_none():
    """The resume prompt echoed under the limit line: the bot has moved on."""
    assert _read(_frame("resumed"))[0] == "none"


def test_a_pane_with_no_limit_reads_none():
    assert _read((FIXTURES / "idle-prompt.txt").read_text())[0] == "none"


def _menu_with(options: list[str]) -> str:
    """The live menu frame with its option lines replaced."""
    lines = _frame("menu").split("\n")
    start = next(i for i, l in enumerate(lines) if "1. Stop and wait" in l)
    end = next(i for i, l in enumerate(lines) if "Enter to confirm" in l) - 1
    return "\n".join(lines[:start] + options + lines[end:])


def test_credits_first_with_the_pointer_on_credits_is_not_the_wait_option():
    """A server flag puts the credits options first, where the pointer starts:
    keepalive must see that its pointer is NOT on the wait option."""
    frame = _menu_with(
        [
            "   ❯ 1. Switch to usage credits",
            "     2. Stop and wait for limit to reset",
            "     3. Wait here, then continue automatically at 10:45am",
        ]
    )
    state, _r, pointer, *_ = _read(frame, "2026-10-07T14:43:00+00:00")
    assert (state, pointer) == ("menu", "other")


def test_the_wait_option_is_found_by_its_label_wherever_it_sits():
    frame = _menu_with(
        [
            "     1. Add funds to continue with usage credits",
            "   ❯ 2. Stop and wait for limit to reset",
            "     3. Upgrade your plan",
        ]
    )
    state, _r, pointer, *_ = _read(frame, "2026-10-07T14:43:00+00:00")
    assert (state, pointer) == ("menu", "wait")


def test_a_menu_without_the_exact_wait_label_is_another_dialog():
    """The usage-billed account's label is "Stop": not the exact label, so not ours."""
    frame = _menu_with(
        [
            "   ❯ 1. Stop",
            "     2. Switch to usage credits",
        ]
    )
    assert _read(frame, "2026-10-07T14:43:00+00:00")[0] == "modal"


def test_another_menu_over_a_limit_is_another_dialog():
    frame = _frame("menu").replace("What do you want to do?", "Do you want to proceed?")
    assert _read(frame, "2026-10-07T14:43:00+00:00")[0] == "modal"


def test_a_frame_with_neither_the_box_nor_the_menu_is_another_dialog():
    """A safeguards pause, a permission prompt, a frame mid-redraw: no box under
    the limit line and no usage-limit menu, so the reader claims nothing."""
    lines = _frame("held").split("\n")
    cut = next(i for i, l in enumerate(lines) if set(l.strip()) == {"─"})
    frame = "\n".join(
        lines[:cut] + ["", "  This conversation is paused.", "   Esc to dismiss"]
    )
    assert _read(frame)[0] == "modal"


def test_a_limit_line_quoted_in_an_answer_reads_none():
    """A bot explaining a limit (this issue's own bots will) writes the line in
    an answer that goes on after it: not a stop."""
    quoted = _frame("held").replace(
        "✻ Baked for 0s · done 10:34 AM",
        "● That line is what the pane showed tonight.\n  It printed the reset without seconds.\n\n✻ Baked for 0s · done 10:34 AM",
    )
    assert _read(quoted)[0] == "none"


def test_claude_codes_resumed_notice_reads_none():
    frame = _frame("held").replace(
        "✻ Baked for 0s · done 10:34 AM",
        "● Usage limit available again · continuing now\n\n✻ Baked for 0s · done 10:34 AM",
    )
    assert _read(frame)[0] == "none"


def test_a_wrapped_limit_line_still_parses():
    long_zone = _frame("held").replace(
        "  ⎿  You've hit your session limit · resets 10:36am (America/New_York)",
        "  ⎿  You've hit your session limit · resets 10:36am\n     (America/New_York)",
    )
    state, reset, *_ = _read(long_zone)
    assert state == "limit" and int(reset) == _epoch("2026-10-07T14:36:00+00:00")


def test_the_command_line_prints_one_tab_separated_line():
    out = subprocess.run(
        [
            sys.executable,
            "-S",
            "-E",
            str(SCRIPT),
            "read",
            "--anchor",
            str(_epoch("2026-10-07T14:34:20+00:00")),
            "--tz",
            NY,
        ],
        input=_frame("held"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.rstrip("\n").split("\t") == [
        "limit",
        str(_epoch("2026-10-07T14:36:00+00:00")),
        "-",
        "-",
        "session limit",
        "10:36am (America/New_York)",
    ]
    epoch = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "epoch",
            "3am (UTC)",
            "--anchor",
            str(_epoch("2026-10-07T00:00:00+00:00")),
        ],
        capture_output=True,
        text=True,
    )
    assert (epoch.returncode, epoch.stdout.strip()) == (
        0,
        str(_epoch("2026-10-07T03:00:00+00:00")),
    )
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "epoch", "soon"], capture_output=True, text=True
    )
    assert (bad.returncode, bad.stdout) == (1, "")
