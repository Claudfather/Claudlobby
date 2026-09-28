"""Read-only host-wide Plane attribution for task reviews.

Review evidence is scoped to the selected host, not the caller's fleet: the
GitHub identity is shared across fleets. No missing role is inferred as reviewed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
import sqlite3

from . import review_rules

# A review report normally follows the GitHub event; bound both clock directions.
DEFAULT_TOLERANCE_S = 120

# Clocks are not perfectly aligned and this host has an RTC-less stale-clock
# window at boot (see selfstart-snapshot.sh). A small backward allowance keeps a
# genuine pair from being missed because the report was stamped a second early.
DEFAULT_BACKWARD_S = 10

# Structured pr_url is an explicit repository claim. A full URL to a different
# repository must not be demoted to an unqualified pull/N prose hit.


# ----------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------


def parse_ts(value: str) -> int | None:
    """Parse mixed-offset GitHub/Plane instants to epoch seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        instant = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return int(instant.timestamp())
    except (ValueError, OverflowError):
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

    Structured PR metadata must name this repo. Bare pull/N matching is only a
    fallback for historical prose without any structured PR URL.
    """
    structured = row.get("pr_url")
    if isinstance(structured, str) and structured:
        return ("pr_url", True) if qualified.search(structured) else None
    prose = row.get("summary")
    if not isinstance(prose, str) or not prose:
        return None
    if qualified.search(prose):
        return "summary", True
    return ("summary", False) if bare.search(prose) else None


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
        actor = row.get("actor") or f"bot:{row.get('_fleet')}/{row.get('bot')}"
        candidates.append(
            {
                "actor": actor,
                "bot": row.get("bot") or "(unnamed)",
                "fleet": row.get("_fleet"),
                "report_ts": row.get("ts"),
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
            "reason": f"no report cites pull/{event.get('_number')} within "
            f"-{backward}s/+{tolerance}s",
            "candidates": [],
        }

    distinct = {c["actor"] for c in candidates}
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
        "actor": best["actor"],
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


# The report producer places non-content PR metadata on exactly one companion
# event: a linked task event or an unlinked report_status marker. Reading those
# rows avoids interpreting a stripped communication body or re-decoding every
# report on the host. A role absent from older rows is unknown, never reviewed.
REVIEW_ROWS_SQL = (
    "SELECT e.occurred_at, i.alias, e.event, e.detail, e.work_item_id, e.assignment_id"
    " FROM events e JOIN identity_registry i ON i.uid = e.actor_uid"
    " WHERE e.kind = 'task' AND e.detail_truncated = 0"
    " AND (e.source_ref LIKE 'report:%' OR e.source_ref LIKE 'report-back:%')"
    " AND json_extract(e.detail, '$.pr_role') = 'reviewed'"
    " AND json_extract(e.detail, '$.pr_url') IS NOT NULL"
    " UNION ALL"
    " SELECT e.occurred_at, i.alias, json_extract(e.detail, '$.status'),"
    " e.detail, NULL, NULL"
    " FROM events e JOIN identity_registry i ON i.uid = e.subject_uid"
    " WHERE e.kind = 'system' AND e.event = 'report_status'"
    " AND e.detail_truncated = 0"
    " AND (e.source_ref LIKE 'report:%' OR e.source_ref LIKE 'report-back:%')"
    " AND json_extract(e.detail, '$.pr_role') = 'reviewed'"
    " AND json_extract(e.detail, '$.pr_url') IS NOT NULL"
)


def load_review_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return host-wide, explicitly reviewed report metadata from one snapshot."""
    rows = []
    for ts, actor, status, detail, work_id, assignment_id in conn.execute(REVIEW_ROWS_SQL):
        if not isinstance(actor, str) or not actor.startswith("bot:") or "/" not in actor:
            continue
        fleet, bot = actor[4:].split("/", 1)
        if not fleet or not bot:
            continue
        try:
            data = json.loads(detail)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append({"ts": ts, "actor": actor, "_fleet": fleet, "bot": bot,
                     "status": status, "pr_url": data.get("pr_url") or "",
                     "summary": data.get("summary") or "",
                     "task_id": work_id or "", "assignment_id": assignment_id})
    return rows


def plane_epoch(conn: sqlite3.Connection) -> str | None:
    """Earliest Plane fact, not earliest PR-citing fact."""
    row = conn.execute("SELECT MIN(occurred_at) FROM events").fetchone()
    return row[0] if row else None


def _gh(args: list[str]) -> dict | list:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise RuntimeError(f"gh failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def fetch_payloads(repo: str, *, pr: int | None, limit: int) -> list[dict]:
    """Fetch each PR payload once; attribution uses those same event objects."""
    if pr is None:
        numbers = _gh(["pr", "list", "--repo", repo, "--state", "open",
                       "--limit", str(limit), "--json", "number"])
        if not isinstance(numbers, list) or any(
                not isinstance(row, dict) or type(row.get("number")) is not int
                or row["number"] <= 0 for row in numbers):
            raise ValueError("GitHub PR listing has an invalid shape")
        prs = [row["number"] for row in numbers]
    else:
        prs = [pr]
    payloads = []
    for number in prs:
        payload = _gh(["pr", "view", str(number), "--repo", repo,
                       "--json", review_rules.PR_FIELDS])
        if not isinstance(payload, dict) or review_rules.missing_payload_fields(payload):
            raise ValueError("GitHub PR payload is incomplete")
        if (type(payload["number"]) is not int or payload["number"] != number
                or not isinstance(payload["headRefOid"], str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", payload["headRefOid"])):
            raise ValueError("GitHub PR payload identity or head is unavailable")
        if any(not isinstance(payload[field], list)
               or any(not isinstance(event, dict) for event in payload[field])
               for field in ("reviews", "comments")):
            raise ValueError("GitHub review events have an invalid shape")
        payloads.append(payload)
    return payloads


def assess_payloads(conn: sqlite3.Connection, payloads: list[dict], repo: str) -> dict:
    """Join one GitHub read to one host-wide Plane snapshot and assess verdicts."""
    rows = load_review_rows(conn)
    epoch = plane_epoch(conn)
    results = []
    attribution = []
    incomplete_attribution = False
    for payload in payloads:
        events = review_rules.events_from_payload(payload)
        matches = attribute(events, rows, repo, payload["number"])
        observed = {event["_event_id"]: event["actor"] for event in matches
                    if event["verdict"] == "MATCH"}
        verdict_ids = {event["_event_id"] for event in events
                       if review_rules.parse_verdict(event["body"]) is not None}
        state = {"state": review_rules.ATTR_ATTEMPTED, "epoch": epoch,
                 "error": None if epoch else "the Plane holds no events",
                 "ambiguous": sum(event["_event_id"] in verdict_ids
                                  and event["verdict"] == "AMBIGUOUS" for event in matches)}
        assessed = review_rules.assess_pr(payload, observed, canonical=True,
                                          attribution=state, require_observed=True)
        unresolved = [event for event in matches
                      if event["_event_id"] in verdict_ids and event["verdict"] != "MATCH"]
        incomplete_attribution |= bool(unresolved)
        assessed["observed_attribution"] = {
            "complete": not unresolved,
            "unresolved_events": [list(event["_event_id"]) for event in unresolved],
        }
        results.append(assessed)
        attribution.append({"number": payload["number"], "events": [
            {"event_id": list(event["_event_id"]), "ts": event["ts"],
             "verdict": event["verdict"], "actor": event.get("actor"),
             "reason": event["reason"], "candidates": event["candidates"]}
            for event in matches]})
    outcome = review_rules.exit_code_for(results)
    return {"repo": repo, "host_scope": "all fleets in the selected root",
            "source": "plane", "rows": len(rows),
            "fleets": sorted({row["_fleet"] for row in rows}),
            "prs": results, "attribution_events": attribution,
            "summary": review_rules.summary_line(results),
            "review_state": "actionable" if outcome == 1 else
                            "incomplete" if outcome == 3 or incomplete_attribution else "assessed"}
