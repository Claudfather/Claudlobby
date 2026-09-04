#!/usr/bin/env python3
"""Answer WHO on this estate authored a review, when GitHub structurally cannot.

The fleet shares one GitHub identity, so every review, comment and verdict on
every PR reads as that one account — the shared PAT's user, or (under fleet-scope
App-auth #1270) the single `<slug>[bot]`. Either way the per-bot author is not
recorded on GitHub's side, so no amount of querying GitHub harder will ever
recover it: fleet-scope App narrows the identity from a human to a bot but does
not make it per-bot (that is #252). This join stays load-bearing until then. The
information exists in exactly one place: the per-fleet report-back ledgers, where
the bot that posted the verdict wrote a row naming itself.

Recovering it is a JOIN, and until now every reader hand-rolled that join. Two
people did it independently on the same PR the same evening and one of them got
it wrong in the other direction first — concluding from the review PROSE that
the reviewer belonged to another fleet, and telling that fleet's manager so, when
the reviewer was a bot they had dispatched themselves. This module exists so the
correct path stops costing extra effort.

WHAT THIS DOES NOT DO, stated up front because overselling it is the likely
failure: it does not make attribution arrive unbidden. A reader who never thinks
to ask "who actually wrote this?" is not helped by a door they do not open. This
only makes the answer cheap once asked. The open problem — that the second axis
has to arrive on its own — is untouched.

Two rules, both learned from how the manual version actually worked:

1. MATCH ON `pull/<N>`, NEVER A BARE NUMBER. A bare `1046` collides with task
   ids (`t-1786320833-e1e9` contains digit runs), epoch timestamps, progress
   percentages and issue numbers, and has misattributed a PR on this estate
   before. Note the shape of the real data: the SAME ledger row carries
   `pr_url: ".../pull/1046"` and `summary: "Request Changes on #1046"`. The
   first is a match; the second is exactly the ambiguous form that must never
   count. `#N` is not accepted either — only `pull/N`, bounded so `pull/1046`
   cannot be satisfied by `pull/10461`.

2. TIMESTAMP-MATCH WITH A TOLERANCE. The ledger row is written seconds AFTER the
   review posts — the bot posts, then reports. The two real pairs on #1046 were
   +12s and +8s. An exact-equality join finds nothing; an unbounded one finds
   everything.

UNMATCHED IS `UNKNOWN`, AND MULTI-MATCHED IS `AMBIGUOUS`. Neither is ever
resolved by picking the nearest candidate, because picking the nearest IS the
guess this module exists to stop. A wrong attribution is worse than no
attribution: no attribution makes a reader go and look, a wrong one makes them
act — which is precisely the failure that motivated this.

Usage:
  who-reviewed.py <owner/repo> <pr-number> [options]

  --json                  machine-readable envelope instead of the text table
  --reviews-json <file>   read the GitHub side from a file instead of calling
                          `gh`; this is the seam that keeps the join unit-testable
                          and lets the module run with no network at all
  --ledger <path>         explicit ledger (repeatable). Without it, every fleet
                          on the host is discovered under $CLAUDLOBBY_ROOT
  --tolerance <seconds>   forward window, ledger row after review (default 120)
  --backward <seconds>    backward allowance for clock skew (default 10)

Standalone stdlib module — `dispatch-overdue.py` precedent — so the join is unit
testable and any bot can call it without importing the compositor package.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import re
import subprocess
import sys
import time

# The ledger row lands after the review posts. Measured on the real pair that
# motivated this: review 14:22:05Z, ledger 14:22:17Z (+12s); the second pair on
# the same PR was +8s. 120s is generous against those, and deliberately not
# generous enough to sweep in an unrelated report on a busy PR — a wider window
# does not produce a better answer, it produces AMBIGUOUS more often, which is
# the honest outcome but a less useful one.
DEFAULT_TOLERANCE_S = 120

# Clocks are not perfectly aligned and this host has an RTC-less stale-clock
# window at boot (see selfstart-snapshot.sh). A small backward allowance keeps a
# genuine pair from being missed because the ledger stamped a second early.
DEFAULT_BACKWARD_S = 10

# Fields scanned for the PR reference. These are the URL-bearing fields written
# by report-back.sh. `summary` is included because a bot may legitimately cite a
# `pull/` URL there, but it can only ever match the SAME bounded `pull/<N>` form
# — the bare `#1046` that also lives in summary text is never a match. The field
# that matched is reported, so a reader can weigh a `pr_url` hit differently from
# a prose hit rather than being handed an undifferentiated "matched".
LEDGER_TEXT_FIELDS = ("pr_url", "issues", "artifact", "summary")


# ----------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------


def parse_ts(value: str) -> int | None:
    """ISO-8601 `...Z` to epoch seconds. None when unparseable.

    Returns None rather than raising: one malformed row must not take out the
    whole join. The row is dropped and counted, never silently treated as
    matching or as not matching.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    # Tolerate fractional seconds; GitHub does not emit them here but a future
    # writer might, and losing the whole row over sub-second precision is a bad
    # trade.
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return calendar.timegm(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------
# PR reference matching
# ----------------------------------------------------------------------


def pr_patterns(repo: str, number: int) -> tuple[re.Pattern, re.Pattern]:
    """(qualified, bare) matchers for a PR reference.

    `qualified` requires the owner/repo path, so a row about another repo's
    #1046 cannot match. `bare` accepts any `pull/<N>` — still never a bare
    number, still bounded against `pull/10461`.

    The trailing guard is a negative lookahead. `\\b` would ALSO be correct here,
    and an earlier version of this comment claimed otherwise — it is worth
    stating plainly because the claim was backwards and survived into a PR body
    and two docs before review caught it. Measured, not reasoned:

        pattern             pull/10461   pull/1046a
        pull/1046\\b          False        False
        pull/1046(?!\\d)      False        True

    `\\b` between the `6` and the `1` of `10461` is indeed not a boundary — and
    that is exactly why it REJECTS. A missing boundary makes `\\b` fail to match,
    which is the outcome we want; it does not silently match the wrong PR.

    The two forms differ on exactly one shape, a trailing non-digit word
    character (`pull/1046a`), where the lookahead is the MORE permissive of the
    two. That shape does not occur in a GitHub PR URL, so the choice is a wash;
    the lookahead stays because it states the actual intent — the thing that
    must not follow is another DIGIT.

    The qualified lookbehind excludes word characters ONLY, deliberately not `/`.
    Excluding `/` looks tighter and is wrong: the owner in a real URL is always
    preceded by one (`https://github.com/Claudfather/...`), so the stricter form
    never matched a single genuine `pr_url` — every real row scored as merely
    `pull/N`, which both understates the basis shown to a reader and erases the
    distinction between this repo and another repo's PR of the same number.
    `(?<!\\w)` still blocks the case that matters, a longer owner ending in the
    target name (`NotClaudfather/Claudlobby/pull/1046`).
    """
    n = re.escape(str(number))
    r = re.escape(repo)
    return (
        re.compile(rf"(?<!\w){r}/pull/{n}(?!\d)"),
        re.compile(rf"(?<!\d)pull/{n}(?!\d)"),
    )


def row_pr_match(row: dict, qualified: re.Pattern, bare: re.Pattern):
    """(field, qualified?) for the strongest PR reference in a row, else None.

    Prefers a repo-qualified hit and prefers structured fields over prose, so
    the reported basis names the best evidence rather than the first found.
    """
    best = None
    for field in LEDGER_TEXT_FIELDS:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        if qualified.search(value):
            return (field, True)  # strongest available; stop looking
        if best is None and bare.search(value):
            best = (field, False)
    return best


# ----------------------------------------------------------------------
# Ledger discovery and loading
# ----------------------------------------------------------------------


def discover_ledgers(root: str) -> list[tuple[str, str]]:
    """[(fleet, path)] for every report-back ledger under a claudlobby root.

    Covers all three layouts the estate actually uses: flat `local/<fleet>/`,
    the nested system container `local/<system>/<fleet>/` that
    migrate-fleet-to-system.sh produces (and which every fleet on this host
    currently uses), and root mode `runtime/fleet/`. Missing a layout would
    silently shrink the search space and turn a real attribution into UNKNOWN,
    so all three are globbed rather than assuming one.
    """
    seen: dict[str, str] = {}
    patterns = [
        os.path.join(root, "local", "*", "runtime", "report-back.jsonl"),
        os.path.join(root, "local", "*", "*", "runtime", "report-back.jsonl"),
        os.path.join(root, "runtime", "fleet", "report-back.jsonl"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            real = os.path.realpath(path)
            if real in seen:
                continue
            # <fleet>/runtime/report-back.jsonl -> the fleet is two levels up.
            fleet = os.path.basename(os.path.dirname(os.path.dirname(path)))
            if fleet == "runtime":  # root mode: runtime/fleet/report-back.jsonl
                fleet = "(root)"
            seen[real] = fleet
    return sorted(
        ((fleet, path) for path, fleet in seen.items()), key=lambda p: (p[0], p[1])
    )


def load_ledger(path: str, fleet: str) -> tuple[list[dict], int]:
    """(rows, unreadable_line_count) for one ledger.

    A poisoned line is counted and skipped, never dropped silently — #911 is the
    standing defect that ledger rows can be unparseable, and a join that quietly
    ignored them would under-report attribution while looking complete.
    """
    rows: list[dict] = []
    bad = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    bad += 1
                    continue
                if not isinstance(row, dict):
                    bad += 1
                    continue
                row["_fleet"] = fleet
                row["_ledger"] = path
                rows.append(row)
    except OSError:
        return ([], -1)  # -1 distinguishes "unreadable file" from "no bad lines"
    return (rows, bad)


# ----------------------------------------------------------------------
# The join
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# The plane side (cutover chunk 6b): the same join off the plane's task events
# instead of the report ledgers, so attribution survives the report ledger's
# retirement. A report's `pr_url` rides the task event's detail (not the
# communication body, which the capture policy may strip), the actor alias
# names the bot AND its fleet, and the assignment's source_ref carries the
# legacy task id. Rows are shaped like ledger rows so `attribute` is untouched.
# ----------------------------------------------------------------------

PLANE_ROWS_SQL = (
    "SELECT e.occurred_at, i.alias, e.event, e.detail, a.source_ref"
    " FROM events e JOIN identity_registry i ON i.uid = e.actor_uid"
    " LEFT JOIN assignments a ON a.assignment_id = e.assignment_id"
    " WHERE e.kind = 'task' AND e.detail_truncated = 0"
    " AND json_extract(e.detail, '$.pr_url') IS NOT NULL"
)


def _readers():
    """The stdlib plane readers beside this file — ONE read-only open (schema
    probe + transient retry) for every stdlib door."""
    import importlib.util
    src = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plane-readers.py")
    spec = importlib.util.spec_from_file_location("plane_readers", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_plane_rows(root: str) -> tuple[list[dict], str | None]:
    """(rows, reason) — rows shaped like ledger rows from the plane's task
    events that carry a pr_url; reason is set (and rows empty) when the plane
    is unreachable, which is NOT an empty answer. The `detail_truncated = 0`
    filter is defensive only: a task event's summary is capped at 4096 bytes
    by the contract, so its detail never reaches the diagnostic cap and no
    row is ever dropped for truncation."""
    import sqlite3
    pr = _readers()
    try:
        conn = pr.connect(root)
    except pr.PlaneUnreachable as exc:
        return [], str(exc)
    try:
        raw = conn.execute(PLANE_ROWS_SQL).fetchall()
    except sqlite3.Error as exc:
        return [], f"plane db unreadable: {exc}"
    finally:
        conn.close()
    rows: list[dict] = []
    for occurred_at, alias, event, detail, source_ref in raw:
        try:
            data = json.loads(detail) if detail else {}
        except (ValueError, TypeError):
            continue
        fleet, _, bot = (alias or "").partition("/")
        fleet = fleet[len("bot:"):] if fleet.startswith("bot:") else fleet
        ts = (occurred_at or "").replace("+00:00", "Z")
        tid = ""
        if source_ref and source_ref.startswith("dispatch-log:") and not source_ref.startswith("dispatch-log:sha:"):
            tid = source_ref[len("dispatch-log:"):]
        rows.append({"ts": ts, "bot": bot or "(unnamed)", "status": event, "task_id": tid,
                     "pr_url": data.get("pr_url") or "", "summary": data.get("summary") or "",
                     "_fleet": fleet, "_ledger": "plane"})
    return rows, None


def report_write_retired(root: str, fleet: str) -> bool:
    """Is the fleet's report write retired on the plane (the ledger frozen)?
    False when the plane cannot say — then the ledger is read and the scope
    discloses the plane's state."""
    pr = _readers()
    try:
        conn = pr.connect(root)
    except pr.PlaneUnreachable:
        return False
    try:
        return pr.retired(conn, fleet, "report") is not None
    except Exception:
        return False
    finally:
        conn.close()


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """One row per (fleet, bot, second): a report the plane and a ledger both
    hold is one candidate, never two — two identical candidates would read as
    AMBIGUOUS under the join's own rule. The plane's row wins the tie."""
    seen: set = set()
    out: list[dict] = []
    ordered = sorted(rows, key=lambda r: 0 if r.get("_ledger") == "plane" else 1)
    for r in ordered:
        key = (r.get("_fleet"), r.get("bot"), (r.get("ts") or "")[:19])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def attribute_event(
    event: dict,
    rows: list[dict],
    qualified: re.Pattern,
    bare: re.Pattern,
    tolerance: int,
    backward: int,
) -> dict:
    """Attribute one review/comment to a bot, or refuse to.

    Verdict is one of MATCH / AMBIGUOUS / UNKNOWN. AMBIGUOUS lists every
    candidate rather than choosing among them; there is deliberately no
    nearest-wins tiebreak, because a tiebreak is a guess wearing arithmetic.
    """
    event_ts = parse_ts(event.get("ts") or "")
    if event_ts is None:
        return {
            **event,
            "verdict": "UNKNOWN",
            "reason": "event timestamp unparseable",
            "candidates": [],
        }

    candidates = []
    for row in rows:
        row_ts = parse_ts(row.get("ts") or "")
        if row_ts is None:
            continue
        delta = row_ts - event_ts
        if not (-backward <= delta <= tolerance):
            continue
        hit = row_pr_match(row, qualified, bare)
        if hit is None:
            continue
        field, is_qualified = hit
        candidates.append(
            {
                "bot": row.get("bot") or "(unnamed)",
                "fleet": row.get("_fleet"),
                "ledger_ts": row.get("ts"),
                "delta_s": delta,
                "field": field,
                "repo_qualified": is_qualified,
                "status": row.get("status") or "",
                "task_id": row.get("task_id") or "",
            }
        )

    if not candidates:
        return {
            **event,
            "verdict": "UNKNOWN",
            "reason": f"no ledger row cites pull/{event.get('_number')} within "
            f"-{backward}s/+{tolerance}s",
            "candidates": [],
        }

    distinct = {(c["bot"], c["fleet"]) for c in candidates}
    if len(distinct) > 1:
        return {
            **event,
            "verdict": "AMBIGUOUS",
            "reason": f"{len(distinct)} distinct bots match the same window",
            "candidates": candidates,
        }

    best = min(candidates, key=lambda c: abs(c["delta_s"]))
    return {
        **event,
        "verdict": "MATCH",
        "reason": "",
        "candidates": candidates,
        "bot": best["bot"],
        "fleet": best["fleet"],
        "basis": best,
    }


def attribute(
    events: list[dict],
    rows: list[dict],
    repo: str,
    number: int,
    tolerance: int = DEFAULT_TOLERANCE_S,
    backward: int = DEFAULT_BACKWARD_S,
) -> list[dict]:
    """Attribute every event. Pure — no I/O, which is what makes it testable."""
    qualified, bare = pr_patterns(repo, number)
    out = []
    for event in events:
        event = {**event, "_number": number}
        out.append(attribute_event(event, rows, qualified, bare, tolerance, backward))
    return out


# ----------------------------------------------------------------------
# GitHub side
# ----------------------------------------------------------------------


def fetch_events(repo: str, number: int) -> list[dict]:
    """Reviews and issue comments for a PR, via `gh`.

    Isolated in one function so every other part of this module is pure and
    offline-testable; `--reviews-json` bypasses it entirely. `gh` is already a
    hard dependency of the fleet, and subprocess is stdlib, so the "no deps"
    property holds.
    """
    proc = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", "reviews,comments"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return events_from_payload(json.loads(proc.stdout or "{}"))


def events_from_payload(payload: dict) -> list[dict]:
    """Normalize `gh pr view --json reviews,comments` into flat events.

    Reviews carry `submittedAt`, comments carry `createdAt` — the join needs one
    field name, and a reader needs to know which kind they are looking at.
    """
    events = []
    for review in payload.get("reviews") or []:
        events.append(
            {
                "kind": "review",
                "ts": review.get("submittedAt") or "",
                "state": review.get("state") or "",
                "github_author": (review.get("author") or {}).get("login") or "",
                "excerpt": _excerpt(review.get("body") or ""),
            }
        )
    for comment in payload.get("comments") or []:
        events.append(
            {
                "kind": "comment",
                "ts": comment.get("createdAt") or "",
                "state": "",
                "github_author": (comment.get("author") or {}).get("login") or "",
                "excerpt": _excerpt(comment.get("body") or ""),
            }
        )
    return sorted(events, key=lambda e: e.get("ts") or "")


def _excerpt(body: str, limit: int = 72) -> str:
    first = (body or "").strip().splitlines()
    text = first[0] if first else ""
    return text[:limit]


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _render_text(result: dict) -> str:
    scope = result["scope"]
    lines = [
        f"who-reviewed {scope['repo']}#{scope['number']}",
        f"  window: -{scope['backward']}s / +{scope['tolerance']}s around each event",
        f"  ledgers: {scope['ledgers_read']} read, {scope['rows']} rows"
        + (f", {scope['unreadable']} unreadable" if scope["unreadable"] else "")
        + (f", {scope['bad_lines']} unparseable rows" if scope["bad_lines"] else ""),
        f"  fleets: {', '.join(scope['fleets']) or '(none found)'}",
        "",
    ]
    if not result["events"]:
        lines.append("  no reviews or comments on this PR")
        return "\n".join(lines)

    for event in result["events"]:
        head = f"  {event['ts']}  {event['kind']:<7}"
        if event["verdict"] == "MATCH":
            basis = event["basis"]
            qual = "repo-qualified" if basis["repo_qualified"] else "pull/N only"
            lines.append(f"{head} → {event['bot']} ({event['fleet']})")
            lines.append(
                f"      basis: {basis['field']} {qual}, ledger {basis['ledger_ts']} "
                f"({basis['delta_s']:+d}s)"
            )
        elif event["verdict"] == "AMBIGUOUS":
            lines.append(f"{head} → AMBIGUOUS — {event['reason']}")
            for cand in event["candidates"]:
                lines.append(
                    f"      candidate: {cand['bot']} ({cand['fleet']}) "
                    f"{cand['ledger_ts']} ({cand['delta_s']:+d}s, {cand['field']})"
                )
        else:
            lines.append(f"{head} → UNKNOWN — {event['reason']}")
        if event.get("excerpt"):
            lines.append(
                f"      github says: {event['github_author']} | {event['excerpt']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="who-reviewed.py",
        description="Attribute a PR's reviews to the bot that wrote them, "
        "which GitHub cannot answer under a shared PAT.",
    )
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("number", type=int, help="pull request number")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--reviews-json", help="read the GitHub side from a file")
    parser.add_argument(
        "--ledger",
        action="append",
        default=[],
        help="explicit ledger path (repeatable)",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("CLAUDLOBBY_ROOT", ""),
        help="claudlobby root for ledger discovery",
    )
    parser.add_argument("--source", choices=("auto", "ledger", "plane"), default="auto",
                        help="auto (default, cutover C3): the plane's rows plus the ledgers of fleets"
                        " whose report write is NOT retired, deduplicated; ledger: the report"
                        " ledgers only; plane: the plane's task events only (chunk 6b)")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE_S)
    parser.add_argument("--backward", type=int, default=DEFAULT_BACKWARD_S)
    args = parser.parse_args(argv)

    if "/" not in args.repo:
        print("repo must be owner/repo", file=sys.stderr)
        return 2

    # GitHub side
    try:
        if args.reviews_json:
            with open(args.reviews_json, "r", encoding="utf-8") as handle:
                events = events_from_payload(json.load(handle))
        else:
            events = fetch_events(args.repo, args.number)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"could not read the review side: {exc}", file=sys.stderr)
        return 3

    # Ledger side
    rows: list[dict] = []
    unreadable = 0
    bad_lines = 0
    fleets: list = []
    ledgers: list = []
    plane_note = None
    if args.source == "plane":
        if not args.root:
            print("--source plane needs --root (or CLAUDLOBBY_ROOT); refusing rather than"
                  " reporting every review as UNKNOWN", file=sys.stderr)
            return 4
        rows, why = load_plane_rows(args.root)
        if why is not None:
            print(f"the plane is unreachable ({why}) — not an empty answer; refusing", file=sys.stderr)
            return 4
        fleets = sorted({r["_fleet"] for r in rows})
    elif args.source == "auto":
        # Cutover C3: the plane holds every report an ARMED fleet made and is the
        # only record once a fleet's report write is retired; an unarmed fleet's
        # reports live in its ledger alone. So: the plane's rows, plus the ledgers
        # of fleets whose write is NOT retired, deduplicated by (fleet, bot,
        # second) before the AMBIGUOUS rule — the same report seen twice must not
        # read as two candidates. An unreachable plane is DISCLOSED in the scope
        # and the ledgers still serve (right for an unflipped fleet, stale for a
        # flipped one — said so, never silent).
        if args.root:
            rows, plane_note = load_plane_rows(args.root)
            ledgers = [(fleet, path) for fleet, path in
                       ([(os.path.basename(os.path.dirname(os.path.dirname(p))) or "(explicit)", p) for p in args.ledger]
                        if args.ledger else discover_ledgers(args.root))
                       if plane_note is not None or not report_write_retired(args.root, fleet)]
        elif args.ledger:
            ledgers = [(os.path.basename(os.path.dirname(os.path.dirname(p))) or "(explicit)", p) for p in args.ledger]
        else:
            print("no --ledger given and CLAUDLOBBY_ROOT is unset, so neither the plane nor a"
                  " ledger could be located; refusing rather than reporting every review as UNKNOWN",
                  file=sys.stderr)
            return 4
    elif args.ledger:
        ledgers = [
            (os.path.basename(os.path.dirname(os.path.dirname(p))) or "(explicit)", p)
            for p in args.ledger
        ]
    elif args.root:
        ledgers = discover_ledgers(args.root)
    else:
        print(
            "no --ledger given and CLAUDLOBBY_ROOT is unset, so no ledger could "
            "be located; refusing rather than reporting every review as UNKNOWN",
            file=sys.stderr,
        )
        return 4

    for fleet, path in ledgers:
        loaded, bad = load_ledger(path, fleet)
        if bad < 0:
            unreadable += 1
            continue
        bad_lines += bad
        rows.extend(loaded)
        fleets.append(fleet)
    if args.source == "auto":
        fleets = sorted(set(fleets) | {r["_fleet"] for r in rows if r.get("_ledger") == "plane"})
        rows = dedupe_rows(rows)

    result = {
        "scope": {
            **({"plane": f"unreachable: {plane_note}"} if plane_note else {}),
            "repo": args.repo,
            "number": args.number,
            "tolerance": args.tolerance,
            "backward": args.backward,
            "ledgers_read": len(fleets),
            "ledgers_found": len(ledgers),
            "unreadable": unreadable,
            "bad_lines": bad_lines,
            "source": args.source,
            "rows": len(rows),
            "fleets": fleets,
        },
        "events": attribute(
            events, rows, args.repo, args.number, args.tolerance, args.backward
        ),
    }

    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
