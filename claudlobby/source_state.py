"""Reachability of a read door's source: absent vs unreadable vs present.

THE RULE, DECIDED ONCE (#1216, #1014)
-------------------------------------
**A reader that cannot reach its source must not return the same thing as a
reader that found nothing.** Those two answers have opposite remedies — "wire the
instrument" versus "there is genuinely no work" — and collapsing them fails
toward *everything is fine*, which is the direction nobody audits.

The line is **presence, not emptiness**. A ledger that exists and holds zero rows
is a legitimate state (a fleet that has not reported yet) and for it "nothing
matched" is the TRUE answer. Only absence or an IO failure makes the same answer
a fabrication. That sentence is ``brief.py``'s, and this module exists so the
five other readers stop re-deciding it — ``brief.py`` had it right and was the
only one that did.

WHY A SHARED MODULE RATHER THAN FIVE `is_file()` CHECKS
------------------------------------------------------
It was already fixed twice, differently, and missing three times:

* ``brief.py`` probes both ledgers, OMITS the section, and lists the reason in
  ``degraded[]`` — the right shape for a *composite* door whose other sections
  stay sound.
* ``cmd_events`` / ``cmd_uptime`` print to stdout and return 1 when the bots dir
  is unreachable — the right shape for a *single-source* command.
* ``report-back`` (#1216), ``workstreams``, and ``dispatch-overdue --orphans``
  (#1014) did neither, and returned a clean empty answer at rc 0.

Three independent expressions of one rule is how the fourth reader gets it
wrong. What is genuinely shared is the *classification* and the *wording* — the
parse is not, because these sources are JSONL, JSON, and plain text logs
respectively. So this module classifies and phrases; each caller parses its own
format and picks its own remedy from ``UNREACHABLE_REMEDIES`` below.

WHERE THE DISCLOSURE GOES IS THE CALLER'S CALL, AND IT VARIES BY CONSUMER
------------------------------------------------------------------------
Not a style preference — measured. ``dispatch-overdue.py``'s stdout is PARSED
(``fleet-pulse.sh:142`` reads it with ``read -r`` into an orphan cache;
``report-back.sh:117`` pipes ``--open`` through ``awk '{print $3}'``), so a
disclosure line printed there becomes a phantom row, and that module already
made this decision explicitly — see its own comment at ``:765``, "refusal is on
stderr behind that caller's ``2>/dev/null`` and its rc". ``claudlobby
report-back``'s stdout is a human table nothing parses, so a stderr-only
disclosure is exactly what hid #1216 for a day.

So: **rc always carries the refusal; the text goes wherever that command's
stdout is not machine-parsed.** Both halves are needed. rc alone is invisible to
a human reading a terminal, and a stdout line alone is invisible to a script.

NOT A VALIDATOR, AND IT NEVER RAISES
------------------------------------
It answers one question about one path and has no opinion about what the caller
does next. A read door that crashed on an unreadable ledger would have swapped a
false all-clear for an outage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The three states. String values, not an enum, because ``brief.py`` already
# emits them verbatim in its schema-1 JSON envelope (``provenance.*.state``) and
# tests assert on the literals — an enum here would be a wire-format change
# wearing a refactor's clothes.
SOURCE_OK = "ok"
SOURCE_ABSENT = "absent"
SOURCE_UNREADABLE = "unreadable"

#: States in which the source could not be reached. Both mean "no answer is
#: available", and they are kept distinct because the remedies differ: absent
#: means the instrument was never wired (or the wrong path was resolved),
#: unreadable means it exists and permissions or IO are broken.
UNREACHABLE = (SOURCE_ABSENT, SOURCE_UNREADABLE)


@dataclass(frozen=True)
class SourceProbe:
    """Whether one source can be read, and the path that was actually tried.

    ``path`` is carried because it is the single most useful thing to print: the
    #1216 incident was a *path* defect, not a missing file — the ledger existed
    all along at the fleet tier while the documented invocation resolved the root
    tier. A message that named only "no ledger found" would have sent the reader
    to create a file that was already there.
    """

    state: str
    path: Path

    @property
    def reachable(self) -> bool:
        return self.state == SOURCE_OK

    @property
    def unreachable(self) -> bool:
        return self.state in UNREACHABLE


def probe_source(path: Path) -> SourceProbe:
    """Classify one path as ok / absent / unreadable.

    A directory given where a file is expected classifies as ABSENT rather than
    unreadable: from the caller's point of view the file it needs is not there,
    and ``IsADirectoryError`` is a subclass of ``OSError`` that would otherwise
    be reported as a permissions problem and send someone to run ``chmod``.

    Openability is tested, not just ``is_file()``. A file that stats fine and
    then raises on read is the mode that takes out a read door, and a probe that
    only stats would have certified it as reachable.
    """
    try:
        with path.open("rb"):
            pass
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return SourceProbe(SOURCE_ABSENT, path)
    except OSError:
        return SourceProbe(SOURCE_UNREADABLE, path)
    return SourceProbe(SOURCE_OK, path)


def probe_dir(path: Path) -> SourceProbe:
    """``probe_source`` for a directory source (a bots dir, an events dir).

    Listability is tested rather than ``is_dir()``, for the same reason
    ``probe_source`` tests openability: a directory with no execute bit stats as
    a directory and then raises on iteration.
    """
    if not path.is_dir():
        return SourceProbe(SOURCE_ABSENT, path)
    try:
        next(iter(path.iterdir()), None)
    except OSError:
        return SourceProbe(SOURCE_UNREADABLE, path)
    return SourceProbe(SOURCE_OK, path)


def unreachable_line(what: str, probe: SourceProbe, *, remedy: str = "") -> str:
    """The one-line disclosure, worded the same way by every caller.

    Shape: what could not be read, which state, the path tried, and — when the
    caller can name one — what to do about it. Uniform wording is the point: an
    operator who has learned to recognise this line once recognises it on every
    command, and it is greppable in a log rather than five near-miss phrasings.

    It says "cannot" rather than "no rows", because the whole defect is that the
    two read alike.
    """
    detail = {
        SOURCE_ABSENT: "does not exist",
        SOURCE_UNREADABLE: "exists but could not be read",
    }.get(probe.state, probe.state)
    line = f"cannot read {what}: {detail} at {probe.path}"
    return f"{line} — {remedy}" if remedy else line


#: Remedies worth naming at the call site. Kept here so the phrasing that sent a
#: manager to the wrong conclusion cannot be re-invented per command.
UNREACHABLE_REMEDIES = {
    # #1216's actual cause: the documented invocation omitted --fleet, so the
    # root tier was resolved and the real ledger was never opened.
    "fleet_tier": (
        "pass --fleet <name> if this fleet lives under local/<fleet>/ "
        "(a root-mode path is resolved without it)"
    ),
}
