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
from urllib.parse import urlsplit

from . import review_rules
from .report_payload import decode_report_body

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


# ----------------------------------------------------------------------
# Verdict URLs (#1537)
# ----------------------------------------------------------------------

#: How an event was attributed, on every event. `url` is exact: a reviewed
#: report named the verdict's URL in --artifact. `window` is timed: no report
#: named it, so the -backward/+tolerance window decided. `window-after-failure`
#: is timed because the verdict's own URL could not be read (the REST review
#: listing failed), and the event's `fallback_reason` says why.
METHOD_URL, METHOD_WINDOW, METHOD_AFTER_FAILURE = "url", "window", "window-after-failure"

_VERDICT_PATH = re.compile(r"/([^/]+)/([^/]+)/(pull|issues)/([0-9]+)")
_VERDICT_FRAGMENT = re.compile(r"(issuecomment|pullrequestreview)-([0-9]+)")


def verdict_url_key(url, repo: str, number: int) -> tuple[str, str] | None:
    """``(kind, id)`` for a URL naming a comment or a review on this PR, else None.

    Comment and review ids are global on GitHub, so the id is what identifies
    the verdict. The repository and PR number must still be this PR's, so a URL
    can never carry a verdict between PRs. A comment is also reachable through
    its issue route (`/issues/N#issuecomment-…`); a review is not. Anything else
    names no verdict, and its report keeps the window fallback.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = _VERDICT_PATH.fullmatch(parts.path)
    fragment = _VERDICT_FRAGMENT.fullmatch(parts.fragment)
    if (parts.scheme != "https" or parts.netloc.lower() != "github.com"
            or path is None or fragment is None):
        return None
    owner, name, route, pr = path.groups()
    if f"{owner}/{name}".casefold() != repo.casefold() or int(pr) != number:
        return None
    kind, ident = fragment.groups()
    if kind == "pullrequestreview" and route != "pull":
        return None
    return kind, ident


def cited_reports(rows: list[dict], repo: str, number: int,
                  qualified: re.Pattern, bare: re.Pattern) -> list[tuple]:
    """This PR's reports as ``(row, hit, names)``: ``names`` maps each verdict
    URL the report's --artifact names on this PR to that URL."""
    cited = []
    for row in rows:
        hit = row_pr_match(row, qualified, bare)
        if hit is None:
            continue
        names = {}
        for url in row.get("artifacts") or ():
            key = verdict_url_key(url, repo, number)
            if key is not None:
                names.setdefault(key, url)
        cited.append((row, hit, names))
    return cited


def _candidate(row: dict, hit: tuple, event_ts: int | None, **extra) -> dict:
    row_ts = parse_ts(row.get("ts") or "")
    field, is_qualified = hit
    return {
        "actor": row.get("actor") or f"bot:{row.get('_fleet')}/{row.get('bot')}",
        "bot": row.get("bot") or "(unnamed)",
        "fleet": row.get("_fleet"),
        "report_ts": row.get("ts"),
        "delta_s": None if row_ts is None or event_ts is None else row_ts - event_ts,
        "field": field,
        "repo_qualified": is_qualified,
        "status": row.get("status") or "",
        "task_id": row.get("task_id") or "",
        "content": row.get("content"),
        **extra,
    }


def _decide(out: dict, candidates: list[dict], ambiguous: str) -> dict:
    """MATCH when every candidate is one bot, else AMBIGUOUS — never a guess."""
    distinct = {c["actor"] for c in candidates}
    if len(distinct) > 1:
        return {**out, "verdict": "AMBIGUOUS", "reason": f"{len(distinct)} {ambiguous}",
                "candidates": candidates}
    best = min(candidates, key=lambda c: abs(c["delta_s"]) if c["delta_s"] is not None
               else float("inf"))
    return {**out, "verdict": "MATCH", "reason": "", "candidates": candidates,
            "actor": best["actor"], "bot": best["bot"], "fleet": best["fleet"], "basis": best}


def attribute_event(
    event: dict,
    cited: list[tuple],
    tolerance: int,
    backward: int,
    link: dict | None = None,
    resolved: frozenset = frozenset(),
) -> dict:
    """Attribute one review/comment to a bot, or refuse to.

    The URL decides first (#1537): a reviewed report that names this verdict's
    URL in --artifact is its report, at any distance in time, so a careful
    write-up, a corrected verdict and a paired review all attribute exactly.
    The time window is only the fallback, and only over reports that name no
    verdict URL: a report that names one verdict is never another's by timing.
    When this verdict's URL could not be read, a report naming a review URL
    nothing resolved stays a candidate too, which is the window as it was.

    ``link`` is this event's ``{"key", "url", "failure"}`` and ``resolved``
    the keys of every verdict URL on this PR that was read.

    Verdict is one of MATCH / AMBIGUOUS / UNKNOWN. AMBIGUOUS lists every
    candidate rather than choosing among them; there is deliberately no
    nearest-wins tiebreak, because a tiebreak is a guess wearing arithmetic.
    """
    link = link or {}
    key, failure = link.get("key"), link.get("failure")
    event_ts = parse_ts(event.get("ts") or "")
    out = {**event, "url": link.get("url"), "fallback_reason": failure}

    if key is not None:
        named = [_candidate(row, hit, event_ts, url=names[key])
                 for row, hit, names in cited if key in names]
        if named:
            for candidate in named:
                candidate["field"] = "artifact"
            return _decide({**out, "method": METHOD_URL}, named,
                           "distinct bots name this verdict's URL")

    out["method"] = METHOD_AFTER_FAILURE if failure else METHOD_WINDOW
    if event_ts is None:
        return {**out, "verdict": "UNKNOWN", "reason": "event timestamp unparseable",
                "candidates": []}

    candidates = []
    for row, hit, names in cited:
        if names and not (failure and any(
                kind == "pullrequestreview" and (kind, ident) not in resolved
                for kind, ident in names)):
            continue
        row_ts = parse_ts(row.get("ts") or "")
        if row_ts is None:
            continue
        if -backward <= row_ts - event_ts <= tolerance:
            candidates.append(_candidate(row, hit, event_ts))

    if not candidates:
        window = f"cites pull/{event.get('_number')} within -{backward}s/+{tolerance}s"
        reason = (f"{failure}; in the fallback window no report {window}" if failure
                  else f"no report names this verdict's URL, and no report without one {window}"
                  if key is not None else f"no report without a verdict URL {window}")
        return {**out, "verdict": "UNKNOWN", "reason": reason, "candidates": []}
    return _decide(out, candidates, "distinct bots match the same window")


def attribute(
    events: list[dict],
    rows: list[dict],
    repo: str,
    number: int,
    tolerance: int = DEFAULT_TOLERANCE_S,
    backward: int = DEFAULT_BACKWARD_S,
    links: dict | None = None,
) -> list[dict]:
    """Attribute every event. Pure — no I/O, which is what makes it testable.

    ``links`` maps an event's ``_event_id`` to its verdict URL's key, or to why
    that URL could not be read (``verdict_links``). Without it every event is
    attributed by the window, as before #1537.
    """
    qualified, bare = pr_patterns(repo, number)
    cited = cited_reports(rows, repo, number, qualified, bare)
    links = links or {}
    resolved = frozenset(link["key"] for link in links.values() if link.get("key"))
    out = []
    for event in events:
        event = {**event, "_number": number}
        out.append(attribute_event(event, cited, tolerance, backward,
                                   links.get(event.get("_event_id")), resolved))
    return out


class ReviewUrlsUnavailable(RuntimeError):
    """The REST review listing could not be read. Its message is the reason
    every affected verdict carries; it names gh's exit code and HTTP status,
    never gh's message text."""


#: Seconds one REST review listing may take, every page included.
REVIEW_LISTING_TIMEOUT_S = 60


def fetch_review_urls(repo: str, number: int) -> dict[str, str]:
    """Every review's node id mapped to its ``html_url``, from the REST listing.

    ``gh pr view`` gives a review an opaque node id and no URL. The REST
    listing carries both (`node_id` equals that id), so it is the one read that
    can join a `#pullrequestreview-…` URL to its review. It is REST, while
    ``gh pr view`` is GraphQL, and the two throttle separately, so this read
    can fail on its own. It is paged 100 at a time with --paginate, so a PR with
    more reviews than one page loses none. The exit status is read directly,
    never through a pipe, and the whole body is read, because gh writes an
    error body to stdout too (#1066). Any doubt raises ReviewUrlsUnavailable;
    it never returns part of a listing.
    """
    command = ["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/reviews?per_page=100"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=REVIEW_LISTING_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ReviewUrlsUnavailable("the REST review listing timed out after "
                                    f"{REVIEW_LISTING_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise ReviewUrlsUnavailable(
            f"the REST review listing could not run ({type(exc).__name__})") from exc
    if proc.returncode:
        status = re.search(r"\(HTTP ([0-9]{3})\)", proc.stderr or "")
        raise ReviewUrlsUnavailable(
            f"the REST review listing failed (gh exit {proc.returncode}"
            + (f", HTTP {status.group(1)}" if status else "") + ")")
    urls = {}
    for review in _listing_pages(proc.stdout):
        if (not isinstance(review, dict) or not isinstance(review.get("node_id"), str)
                or not review["node_id"] or type(review.get("id")) is not int
                or verdict_url_key(review.get("html_url"), repo, number)
                != ("pullrequestreview", str(review["id"]))):
            raise ReviewUrlsUnavailable("the REST review listing has an unexpected shape")
        urls[review["node_id"]] = review["html_url"]
    return urls


def _listing_pages(text: str) -> list:
    """Every item on every page gh printed. gh 2.92 merges REST array pages into
    one array; older gh prints one array per page, back to back."""
    decoder, items, at, pages = json.JSONDecoder(), [], 0, 0
    try:
        while True:
            while at < len(text) and text[at].isspace():
                at += 1
            if at == len(text):
                break
            page, at = decoder.raw_decode(text, at)
            if not isinstance(page, list):
                raise ValueError("a page is not a JSON array")
            items.extend(page)
            pages += 1
    except (ValueError, TypeError) as exc:
        raise ReviewUrlsUnavailable(
            "the REST review listing returned an unreadable body") from exc
    if not pages:
        raise ReviewUrlsUnavailable("the REST review listing returned an empty body")
    return items


def verdict_links(payload: dict, rows: list[dict], repo: str) -> tuple[dict, dict]:
    """Each event's verdict URL key, plus the state of the REST review listing.

    A comment's URL comes with ``gh pr view``. A review's does not, so the REST
    listing is read, and only when a reviewed report on this PR names a review
    URL: with none named, no review could join by URL anyway. Returns
    ``(links, listing)``. ``links`` maps each ``_event_id`` to ``{"key", "url",
    "failure"}``, and ``listing`` is ``{"state", "reviews", "error"}`` with state
    `not-needed`, `read` or `unavailable`.
    """
    number = payload["number"]
    qualified, bare = pr_patterns(repo, number)
    links = {}
    for index, comment in enumerate(payload.get("comments") or []):
        url = comment.get("url")
        key = verdict_url_key(url, repo, number)
        links[("comments", index)] = {"key": key, "url": url if key else None, "failure": None}
    named_review = any(kind == "pullrequestreview"
                       for _row, _hit, names in cited_reports(rows, repo, number, qualified, bare)
                       for kind, _ident in names)
    urls, failure = {}, None
    if not named_review:
        listing = {"state": "not-needed", "reviews": None, "error": None}
    else:
        try:
            urls = fetch_review_urls(repo, number)
        except ReviewUrlsUnavailable as exc:
            failure = str(exc)
            listing = {"state": "unavailable", "reviews": None, "error": failure}
        else:
            listing = {"state": "read", "reviews": len(urls), "error": None}
    for index, review in enumerate(payload.get("reviews") or []):
        url = urls.get(review.get("id")) if named_review and not failure else None
        missing = (failure or ("this review is not in the REST review listing"
                               if named_review and url is None else None))
        links[("reviews", index)] = {"key": verdict_url_key(url, repo, number) if url else None,
                                     "url": url, "failure": missing}
    return links, listing


# The report producer places non-content PR metadata on exactly one companion
# event: a linked task event or an unlinked report_status marker. Those rows
# select the reviewed reports; a role absent from older rows is unknown, never
# reviewed. A report's --artifact URLs are content, so they live only in its own
# communication body, joined here by the companion's ref and sender (#1537).
_REPORT_BODY = (
    " LEFT JOIN communications c ON c.msg_id = substr(e.source_ref, instr(e.source_ref, ':') + 1)"
    " AND c.source_ref = e.source_ref AND c.sender_uid = i.uid AND c.message_class = 'report'"
)
REVIEW_ROWS_SQL = (
    "SELECT e.occurred_at, i.alias, e.event, e.detail, e.work_item_id, e.assignment_id,"
    " c.msg_id, c.body, c.privacy, c.truncated"
    " FROM events e JOIN identity_registry i ON i.uid = e.actor_uid" + _REPORT_BODY +
    " WHERE e.kind = 'task' AND e.detail_truncated = 0"
    " AND (e.source_ref LIKE 'report:%' OR e.source_ref LIKE 'report-back:%')"
    " AND json_extract(e.detail, '$.pr_role') = 'reviewed'"
    " AND json_extract(e.detail, '$.pr_url') IS NOT NULL"
    " UNION ALL"
    " SELECT e.occurred_at, i.alias, json_extract(e.detail, '$.status'),"
    " e.detail, NULL, NULL, c.msg_id, c.body, c.privacy, c.truncated"
    " FROM events e JOIN identity_registry i ON i.uid = e.subject_uid" + _REPORT_BODY +
    " WHERE e.kind = 'system' AND e.event = 'report_status'"
    " AND e.detail_truncated = 0"
    " AND (e.source_ref LIKE 'report:%' OR e.source_ref LIKE 'report-back:%')"
    " AND json_extract(e.detail, '$.pr_role') = 'reviewed'"
    " AND json_extract(e.detail, '$.pr_url') IS NOT NULL"
)


def report_artifacts(msg_id, body, privacy, truncated, pr_url) -> tuple[tuple[str, ...], str]:
    """A reviewed report's --artifact URLs, and the state of the body they came from.

    The shipped decoder reads the body. Metadata capture withholds it with the
    artifacts in it (`withheld`), and a missing, legacy, truncated or
    unreadable body proves no URL either. So does a body whose PR is not the
    companion's (`inconsistent`). In each of those cases the report names no
    verdict and stays a window candidate.
    """
    if msg_id is None:
        return (), "missing"
    decoded = decode_report_body(body, privacy=privacy, truncated=bool(truncated))
    payload = decoded.payload
    if payload is None:
        return (), decoded.state
    if payload.pr_url != pr_url or payload.pr_role != "reviewed":
        return (), "inconsistent"
    return payload.artifacts, decoded.state


def load_review_rows(conn: sqlite3.Connection) -> list[dict]:
    """Return host-wide, explicitly reviewed report metadata from one snapshot."""
    rows = []
    for (ts, actor, status, detail, work_id, assignment_id,
         msg_id, body, privacy, truncated) in conn.execute(REVIEW_ROWS_SQL):
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
        artifacts, content = report_artifacts(msg_id, body, privacy, truncated,
                                              data.get("pr_url"))
        rows.append({"ts": ts, "actor": actor, "_fleet": fleet, "bot": bot,
                     "status": status, "pr_url": data.get("pr_url") or "",
                     "summary": data.get("summary") or "",
                     "task_id": work_id or "", "assignment_id": assignment_id,
                     "artifacts": artifacts, "content": content})
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
        links, listing = verdict_links(payload, rows, repo)
        matches = attribute(events, rows, repo, payload["number"], links=links)
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
        methods = {}
        for event in matches:
            if event["_event_id"] in verdict_ids:
                methods[event["method"]] = methods.get(event["method"], 0) + 1
        assessed["observed_attribution"] = {
            "complete": not unresolved,
            "unresolved_events": [list(event["_event_id"]) for event in unresolved],
            # How the verdicts were attributed: an exact `url` join, or a timed
            # `window` (`window-after-failure` when the URL could not be read).
            "methods": methods,
        }
        results.append(assessed)
        attribution.append({"number": payload["number"], "review_urls": listing, "events": [
            {"event_id": list(event["_event_id"]), "ts": event["ts"],
             "verdict": event["verdict"], "actor": event.get("actor"),
             "method": event["method"], "url": event["url"],
             "fallback_reason": event["fallback_reason"],
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
