"""Reachability of a read door's source: absent vs unreadable vs present.

THE RULE, DECIDED ONCE (#1216, #1014)
-------------------------------------
**A reader that cannot reach its source must not return the same thing as a
reader that found nothing.** Those two answers have opposite remedies — "wire the
instrument" versus "there is genuinely no work" — and collapsing them fails
toward *everything is fine*, which is the direction nobody audits.

A FOURTH STATE WAS CONSIDERED AND DEFERRED, NOT MISSED (#1256)
--------------------------------------------------------------
``probe_source`` has exactly three outcomes and no time comparison, so a source
that is present, readable and **fossilised** rates ``SOURCE_OK``. That is a
fail-open state inside the module written to close the fail-open class, and it
is live rather than theoretical: one fleet's ``report-back.jsonl`` holds 63 rows
last written ``2026-07-07T01:36:31Z`` and is still read today.

Deferred rather than fixed here because "stale" is not a property of the source.
It is a per-consumer policy — the age at which a dispatch ledger stops being
evidence is not the age at which a workstream registry does — so it belongs
above this module, and #1256 owns that general case. Reachability is what this
answers.

**Retirement condition, which is a measurement and not a merge.** This note is
dead the moment ``probe_source`` returns a state other than the three above for
a present-but-old source. Check it with one grep::

    grep -n 'SOURCE_STALE' claudlobby/source_state.py

A hit means the bound no longer holds and this block should go. Deliberately NOT
keyed on "#1256 lands": that issue may land without touching this predicate, and
a caveat that expires on somebody else's merge is an expired conditional nobody
re-reads — the same defect this module exists to prevent, one level up.

Who checks: whoever next edits this module. The note sits against the rule it
qualifies precisely so that edit cannot miss it.

Three people reached this gap independently on 2026-08-18 — one widening a
coverage check, one filing #1256, one reproducing it inside ``probe_source`` by
running it. That is why it is disclosed here rather than shrugged off.

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

import os

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
    # os.scandir, never pathlib predicates: the classification must come from
    # ERRNOS AT CALL TIME, not from what pathlib elects to swallow. The prior
    # form leaned on Path.is_dir() propagating EACCES (pathlib swallowed only
    # ENOENT/ENOTDIR/EBADF/ELOOP — measured, and pinned) — a premise Python
    # 3.13+ KILLED: is_dir() now swallows every OSError, so an unreadable
    # ANCESTOR read as plain False and fell through to ABSENT. The pin guarding
    # this went red into the known-failing baseline with the interpreter
    # upgrade, and the defect resurfaced live on the trust surface as
    # "quarantined: 0" from a spool tree it could not even reach — the exact
    # false all-clear this module exists to kill (external review, probed).
    # scandir raises the real errno on every supported version: ENOENT/ENOTDIR
    # (incl. from an absent ancestor) -> ABSENT; EACCES and the rest -> the
    # honest UNREADABLE.
    # …and the iterator is ADVANCED ONCE inside the same boundary: opendir
    # can succeed while the first readdir raises (EIO/ESTALE from failing
    # storage, FUSE, or a network filesystem — the estate's SD-stall class),
    # and an un-advanced scandir never observes it. The first rewrite
    # returned OK from the open alone and re-certified exactly that
    # unreadable-as-healthy state (external round 2, probed with an
    # iterator raising on its first entry). This is the property the
    # original iterdir form had and the scandir rewrite briefly lost.
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except (FileNotFoundError, NotADirectoryError):
        return SourceProbe(SOURCE_ABSENT, path)
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
