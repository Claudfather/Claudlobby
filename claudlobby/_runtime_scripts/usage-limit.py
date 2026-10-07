#!/usr/bin/env python3
"""usage-limit.py -- read a Claude Code pane for a usage-limit stop (#996).

A claude.ai usage limit stops a bot with one line in its transcript:

    ⎿  You've hit your session limit · resets 10:50pm (America/New_York)

On this estate nothing ever resumes it. Every bot runs with --remote-control, and
while that bridge is up Claude Code arms neither its own automatic continue nor its
limit menu (both check the bridge first, in 2.1.291 and 2.1.292), so the pane sits
at an empty prompt that keepalive used to read as IDLE. keepalive now asks this
reader about any pane whose text names a usage limit, and reads it as held by the
limit until the reset, then resumes the bot once (keepalive.sh, KEEPALIVE_LIMIT_RESUME).

    usage-limit.py read [--anchor EPOCH] [--tz ZONE] < PANE
        One line, tab-separated:  STATE  RESET_EPOCH  POINTER  NATIVE  LIMIT  RESET_TEXT
        STATE    none     no usage limit holds the pane now
                 limit    a usage limit holds it, and the input box is up
                 menu     the usage-limit menu is up ("What do you want to do?" with the
                          exact option "Stop and wait for limit to reset")
                 modal    a usage limit holds it under some other menu or dialog: no keys
        RESET_EPOCH  the printed reset minute as epoch seconds, or - when the text names none
        POINTER  menu only: wait when the menu's pointer is on the wait option, other when
                 it is on any other line; - otherwise
        NATIVE   armed when Claude Code's own automatic continue is waiting; - otherwise
    usage-limit.py epoch RESET_TEXT [--anchor EPOCH] [--tz ZONE]
        The reset instant RESET_TEXT names, as epoch seconds (exit 1 when it names none).

The reset text comes in each form Claude Code prints (its formatter drops the seconds,
shows the minutes only when they are not :00, lowercases am/pm and drops their space,
and adds the date only beyond 24 hours, the year only when it is not this one):
    3am · 2:50am · 2:50am (America/New_York) · Oct 8, 2:50am · Oct 8, 2026, 2:50am (UTC)
A time with no date is the first such minute after ANCHOR less SLACK_S: the line was
printed before the reset and at most 24 hours ahead of it, and ANCHOR is the first
tick that saw it (keepalive's data/.limit), at most a few ticks after the print.

Positive evidence only: a pane this reader does not recognise reads none or modal,
never menu, and keepalive sends keys only on limit and menu. Stdlib only, Python 3.9
(the system python3 on the estate's macOS hosts).
"""

import datetime
import os
import re
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9: no zone database, no parse
    ZoneInfo = None

SLACK_S = 600
WAIT_LABEL = "Stop and wait for limit to reset"
MENU_TITLE = "What do you want to do?"
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
    )
}
_RESET_RE = re.compile(
    r"^(?:(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?P<day>\d{1,2}),"
    r"(?: (?P<year>\d{4}),)? )?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?(?P<ampm>am|pm)?"
    r"(?: \((?P<tz>[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*)\))?$"
)
_LIMIT_RE = re.compile(
    r"You've hit your (?P<name>[^·]{1,60}?)(?: · resets (?P<reset>[^·]{1,80}?))?"
    r"(?: · progress saved)?\s*$"
)


def _zone(name):
    if ZoneInfo is None or not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def reset_epoch(text, anchor, tz_name=None):
    """The epoch second of the minute TEXT names, or None when it names none."""
    m = _RESET_RE.match(text.strip())
    if m is None:
        return None
    zone = (
        _zone(m.group("tz"))
        or _zone(tz_name)
        or _zone(os.environ.get("TZ", "").lstrip(":"))
    )
    if zone is None:
        zone = datetime.datetime.now().astimezone().tzinfo
    hour, minute = int(m.group("hour")), int(m.group("min") or 0)
    ampm = m.group("ampm")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    here = datetime.datetime.fromtimestamp(anchor, zone)
    try:
        if m.group("mon"):
            year = int(m.group("year") or here.year)
            when = datetime.datetime(
                year,
                _MONTHS[m.group("mon")],
                int(m.group("day")),
                hour,
                minute,
                tzinfo=zone,
            )
            if m.group("year") is None and when.timestamp() < anchor - 183 * 86400:
                when = when.replace(year=year + 1)
            return int(when.timestamp())
        for days in (-1, 0, 1, 2):
            day = here.date() + datetime.timedelta(days=days)
            when = datetime.datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=zone
            )
            if when.timestamp() > anchor - SLACK_S:
                return int(when.timestamp())
    except ValueError:
        return None
    return None


def _norm(line):
    return line.replace(" ", " ").replace(" ", " ").rstrip()


def _is_rule(line):
    """A border drawn across the pane: the input box's rules (─), or the top
    edge of a menu (▔)."""
    s = line.strip()
    return len(s) >= 10 and set(s) <= {"─", "▔"}


# What may follow the limit line while it is still the bot's last word: the
# error's own hint lines (indented, "/usage-credits to ...", "/upgrade to ..."),
# the turn summary ("✻ Baked for 0s · done 10:34 AM"), and Claude Code's own
# notices about this limit ("● Usage limit reached · continuing automatically
# ...", wrapped onto indented lines). Anything else, a prompt echoed back or an
# answer, means the bot has moved on and the line is history.
_SUMMARY_RE = re.compile(r"^\S \S+ for \d")
_HINT_RE = re.compile(
    r"^\s+(/[a-z][a-z-]*\b|Switch models|Run /|Ask your admin|Raise the cap)"
)
# Status drawn right-aligned above the box, not transcript: "◈ max · /effort"
# appears there once the limit menu closes (measured, 2.1.292).
_CHROME_RE = re.compile(r"^ {20,}\S")
_HELD_NOTICE_RE = re.compile(
    r"^\S (Usage limit reached|Automatic continue|Usage limit has reset)"
)
_RESUMED_NOTICE_RE = re.compile(r"^\S Usage limit available again")
_NATIVE_RE = re.compile(r"[Cc]ontinuing (automatically|shortly)")
_POINTER_RE = re.compile(r"^\s*❯\s*")
_OPTION_NUM_RE = re.compile(r"^\d+\.\s+")


def _limit_at(lines, i, anchor, tz_name):
    """The limit line at I, joined with one wrapped line when that is what parses."""
    tries = [(lines[i], 1)]
    if i + 1 < len(lines) and lines[i + 1].startswith(" ") and lines[i + 1].strip():
        nxt = lines[i + 1].strip()
        tries += [(lines[i] + " " + nxt, 2), (lines[i] + nxt, 2)]
    fallback = None
    for text, used in tries:
        m = _LIMIT_RE.search(text)
        if m is None:
            continue
        reset = (m.group("reset") or "").strip()
        when = reset_epoch(reset, anchor, tz_name) if reset else None
        if when is not None:
            return m.group("name").strip(), reset, when, used
        if fallback is None:
            fallback = (m.group("name").strip(), reset, None, used)
    if fallback is not None:
        return fallback
    name = lines[i].split("You've hit your", 1)[1].split("·", 1)[0].strip()
    return name, "", None, 1


def read_pane(text, anchor, tz_name=None):
    """(STATE, RESET_EPOCH, POINTER, NATIVE, LIMIT, RESET_TEXT) for one pane capture."""
    lines = [_norm(l) for l in text.split("\n")]
    hits = [i for i, l in enumerate(lines) if "You've hit your" in l]
    none = ("none", "-", "-", "-", "", "")
    if not hits:
        return none
    i = hits[-1]
    name, reset, when, used = _limit_at(lines, i, anchor, tz_name)
    j = i + used
    native = "-"
    while j < len(lines):
        line = lines[j]
        if _is_rule(line) or line.strip() == MENU_TITLE:
            break
        if (
            not line.strip()
            or _SUMMARY_RE.match(line)
            or _HINT_RE.match(line)
            or _CHROME_RE.match(line)
        ):
            j += 1
            continue
        if _RESUMED_NOTICE_RE.match(line):
            return none
        if _HELD_NOTICE_RE.match(line):
            if _NATIVE_RE.search(line):
                native = "armed"
            j += 1
            while j < len(lines) and lines[j].startswith(" ") and lines[j].strip():
                j += 1
            continue
        return none
    rest = lines[j:]
    if any(_NATIVE_RE.search(l) for l in rest):
        native = "armed"
    epoch = str(when) if when is not None else "-"
    title = [k for k, l in enumerate(rest) if l.strip() == MENU_TITLE]
    if title:
        options = rest[title[0] + 1 :]
        # An option's label: its line without the pointer, the indent and the
        # number, so the wait option is found by its words on any line.
        labels = [
            _OPTION_NUM_RE.sub("", _POINTER_RE.sub("", l).strip()).strip()
            for l in options
        ]
        if WAIT_LABEL not in labels:
            return ("modal", epoch, "-", native, name, reset)
        pointed = [k for k, l in enumerate(options) if _POINTER_RE.match(l)]
        pointer = "wait" if pointed and labels[pointed[0]] == WAIT_LABEL else "other"
        return ("menu", epoch, pointer, native, name, reset)
    # The input box: a rule with the prompt glyph on the line under it. A menu
    # draws its pointer under a blank line, never under a rule.
    box = [
        k
        for k in range(1, len(rest))
        if _is_rule(rest[k - 1])
        and "─" in rest[k - 1]
        and _POINTER_RE.match(rest[k])
    ]
    if not box:
        return ("modal", epoch, "-", native, name, reset)
    return ("limit", epoch, "-", native, name, reset)


def main(argv):
    if len(argv) < 2 or argv[1] not in ("read", "epoch"):
        print("usage: usage-limit.py read|epoch ...", file=sys.stderr)
        return 2
    args, opts, i = [], {"--anchor": None, "--tz": None}, 2
    while i < len(argv):
        if argv[i] in opts and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            args.append(argv[i])
            i += 1
    try:
        anchor = (
            int(opts["--anchor"])
            if opts["--anchor"]
            else int(datetime.datetime.now().timestamp())
        )
    except ValueError:
        print("usage-limit.py: --anchor takes epoch seconds", file=sys.stderr)
        return 2
    if argv[1] == "epoch":
        if len(args) != 1:
            print(
                "usage: usage-limit.py epoch RESET_TEXT [--anchor EPOCH] [--tz ZONE]",
                file=sys.stderr,
            )
            return 2
        got = reset_epoch(args[0], anchor, opts["--tz"])
        if got is None:
            return 1
        print(got)
        return 0
    print("\t".join(read_pane(sys.stdin.read(), anchor, opts["--tz"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
