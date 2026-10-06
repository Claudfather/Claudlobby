"""Unit tests for review_queries — attributing a PR review to the bot that
wrote it, when a shared GitHub PAT makes every review read `chrisrogers37`.

The two rules under test are the ones that came from the manual version failing:
a bare number must never match, and the report lands seconds after the review so
an exact-equality join finds nothing.

The Plane is the only attribution source. Pure matching tests retain the
historical report-row examples; the current query tests explicit review roles.
"""

from __future__ import annotations

import itertools
import json
import subprocess

import pytest

from tests.conftest import report_row as _report
from claudlobby import review_queries as who, review_rules
from claudlobby.plane.emit_api import emit_batch
from claudlobby.report_payload import ReportLink, ReportPayload, encode_report_facts
from tests.plane_fixtures import plane_root, ro

REPO = "Claudfather/Claudlobby"
URL = f"https://github.com/{REPO}/pull/1046"


def _rows(*rows):
    """Attach the fleet marker load_plane_rows adds, without touching a plane."""
    out = []
    for row, fleet in rows:
        out.append({**row, "_fleet": fleet})
    return out


def _event(ts, kind="review"):
    return {
        "kind": kind,
        "ts": ts,
        "state": "",
        "github_author": "chrisrogers37",
        "excerpt": "",
    }


class TestParseTs:
    def test_iso_z(self):
        # Cross-checked against datetime(...tzinfo=utc).timestamp() and `date -u`.
        assert who.parse_ts("2026-08-06T14:22:05Z") == 1786026125

    def test_fractional_seconds_survive(self):
        assert who.parse_ts("2026-08-06T14:22:05.123Z") == who.parse_ts(
            "2026-08-06T14:22:05Z"
        )

    def test_garbage_is_none_not_raise(self):
        assert who.parse_ts("not-a-time") is None
        assert who.parse_ts("") is None
        assert who.parse_ts(None) is None


class TestPrReferenceMatching:
    """Rule 1: `pull/<N>`, never a bare number."""

    def test_pr_url_matches(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report("vera", "2026-08-06T14:22:17Z", pr_url=URL)
        assert who.row_pr_match(row, q, b) == ("pr_url", True)

    def test_bare_number_never_matches(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report("vera", "2026-08-06T14:22:17Z", summary="finished 1046 finally")
        assert who.row_pr_match(row, q, b) is None

    def test_hash_reference_never_matches(self):
        """The real ledger row carries `#1046` in summary AND a URL in pr_url.
        The hash form alone must not be enough — it is the collision shape."""
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera", "2026-08-06T14:22:17Z", summary="Request Changes on #1046"
        )
        assert who.row_pr_match(row, q, b) is None

    def test_task_id_digits_never_match(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report("vera", "2026-08-06T14:22:17Z", task_id="t-1786321046-e1e9")
        assert who.row_pr_match(row, q, b) is None

    def test_longer_number_does_not_satisfy_shorter(self):
        """`pull/10461` must not answer a query for 1046.

        What this protects is the BEHAVIOUR — that some trailing guard exists.
        It fails if the guard is dropped entirely, which is the regression worth
        catching. It deliberately claims NOTHING about which guard is used: it
        passes under both `\\b` and `(?!\\d)`, because on this input the two are
        equivalent. An earlier docstring called this "the \\b trap", which
        asserted a discrimination the assertion cannot make.
        """
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url=f"https://github.com/{REPO}/pull/10461",
        )
        assert who.row_pr_match(row, q, b) is None

    def test_the_two_boundary_forms_are_equivalent_on_pr_shaped_input(self):
        """Pin the equivalence directly, so nobody re-derives it from prose.

        A mutation run that swaps `(?!\\d)` for `\\b` comes back GREEN, and that
        is not a test-adequacy failure — it is an INERT MUTANT. The two forms
        differ on exactly one shape, a trailing non-digit word character, which
        no GitHub PR URL produces. Asserting that here means the next reader
        gets the fact from an executable check rather than from a comment that
        was wrong once already.
        """
        import re

        digit_guard = re.compile(r"(?<!\d)pull/1046(?!\d)")
        word_boundary = re.compile(r"(?<!\d)pull/1046\b")
        pr_shaped = [
            "pull/1046",
            "pull/10461",
            "pull/1046/files",
            "pull/1046#issuecomment-1",
            "https://github.com/o/r/pull/1046",
            "see pull/1046 please",
        ]
        for text in pr_shaped:
            assert bool(digit_guard.search(text)) == bool(word_boundary.search(text)), (
                text
            )
        # The single divergence, stated rather than implied: the lookahead is the
        # MORE permissive of the two here, not the stricter one.
        assert digit_guard.search("pull/1046a")
        assert not word_boundary.search("pull/1046a")

    def test_longer_owner_ending_in_ours_is_not_qualified(self):
        """A longer owner cannot turn a structured foreign URL into a hit."""
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url="https://github.com/NotClaudfather/Claudlobby/pull/1046",
        )
        assert who.row_pr_match(row, q, b) is None

    def test_explicit_other_repo_does_not_become_an_unqualified_hit(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url="https://github.com/Other/Repo/pull/1046",
        )
        assert who.row_pr_match(row, q, b) is None

    def test_qualified_beats_prose(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera", "2026-08-06T14:22:17Z", summary=f"see {REPO}/pull/1046", pr_url=URL
        )
        field, qualified = who.row_pr_match(row, q, b)
        assert (field, qualified) == ("pr_url", True)


class TestTolerance:
    """Rule 2: the ledger row is written AFTER the review posts."""

    def test_real_pair_from_1046_matches(self):
        """The regression this module exists for: review 14:22:05Z, vera's
        ledger row 14:22:17Z, +12s. An exact join reports UNKNOWN here."""
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "MATCH"
        assert out[0]["bot"] == "vera"
        assert out[0]["fleet"] == "ai-platform"
        assert out[0]["basis"]["delta_s"] == 12

    def test_exact_equality_would_have_missed_it(self):
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute(
            [_event("2026-08-06T14:22:05Z")], rows, REPO, 1046, tolerance=0, backward=0
        )
        assert out[0]["verdict"] == "UNKNOWN"

    def test_beyond_tolerance_is_unknown(self):
        rows = _rows(
            (_report("vera", "2026-08-06T14:40:00Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "UNKNOWN"

    def test_small_backward_skew_still_matches(self):
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:00Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "MATCH"
        assert out[0]["basis"]["delta_s"] == -5

    def test_far_backward_is_unknown(self):
        """A row written well BEFORE the review did not report that review."""
        rows = _rows(
            (_report("vera", "2026-08-06T14:00:00Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "UNKNOWN"


class TestRefusals:
    """Unmatched is UNKNOWN and multi-matched is AMBIGUOUS — never a guess."""

    def test_no_rows_is_unknown(self):
        out = who.attribute([_event("2026-08-06T14:22:05Z")], [], REPO, 1046)
        assert out[0]["verdict"] == "UNKNOWN"
        assert out[0]["candidates"] == []

    def test_two_bots_in_window_is_ambiguous_not_nearest(self):
        """The nearest row is vera's, but ravi is also in the window. Picking
        the nearest would be the guess this module exists to prevent."""
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform"),
            (_report("ravi", "2026-08-06T14:22:40Z", pr_url=URL), "crog-eng-team"),
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "AMBIGUOUS"
        assert "bot" not in out[0]
        assert {c["bot"] for c in out[0]["candidates"]} == {"vera", "ravi"}

    def test_same_bot_twice_is_still_a_match(self):
        """Two rows from one bot are not an ambiguity — the answer is the same."""
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform"),
            (_report("vera", "2026-08-06T14:22:40Z", pr_url=URL), "ai-platform"),
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "MATCH"
        assert out[0]["bot"] == "vera"

    def test_same_bot_name_in_two_fleets_is_ambiguous(self):
        """Bot-name collision across fleets (#526) must not silently resolve."""
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform"),
            (_report("vera", "2026-08-06T14:22:20Z", pr_url=URL), "acme-fleet"),
        )
        out = who.attribute([_event("2026-08-06T14:22:05Z")], rows, REPO, 1046)
        assert out[0]["verdict"] == "AMBIGUOUS"

    def test_unparseable_event_ts_is_unknown(self):
        rows = _rows(
            (_report("vera", "2026-08-06T14:22:17Z", pr_url=URL), "ai-platform")
        )
        out = who.attribute([_event("garbage")], rows, REPO, 1046)
        assert out[0]["verdict"] == "UNKNOWN"


class TestPayloadNormalization:
    def test_reviews_and_comments_both_become_events(self):
        payload = {
            "reviews": [
                {
                    "submittedAt": "2026-08-06T14:22:05Z",
                    "state": "COMMENTED",
                    "author": {"login": "chrisrogers37"},
                    "body": "**Request Changes**\nbody",
                }
            ],
            "comments": [
                {
                    "createdAt": "2026-08-06T14:30:00Z",
                    "author": {"login": "chrisrogers37"},
                    "body": "a note",
                }
            ],
        }
        events = review_rules.events_from_payload(payload)
        assert [e["surface"] for e in events] == ["reviews", "comments"]
        assert events[0]["body"] == "**Request Changes**\nbody"

    def test_empty_payload_is_no_events(self):
        assert review_rules.events_from_payload({}) == []


def test_host_review_rows_keep_only_explicit_review_role_and_both_report_legs(tmp_path):
    """One host snapshot: linked + unlinked reviewed, authored excluded.

    Both reviewers matching one PR remain ambiguous even when one report lands
    after the other. A missing role cannot be retroactively called reviewed.
    """
    import sqlite3
    from claudlobby.plane.emit_api import emit_batch
    from tests.plane_fixtures import _scene, F

    root, _paths, _dispatch_rows, _reports = _scene(tmp_path)
    url = "https://github.com/org/repo/pull/1046"
    ts = "2026-09-02T14:00:00Z"
    emit_batch(root, [
        {"event_type": "task", "emitter": "report-back", "fleet": F,
         "source_ref": f"report-back:msg_{'5':0>32}", "occurred_at": ts,
         "payload": {"work_item_id": f"wi_{'2':0>32}", "assignment_id": f"asg_{'2':0>32}",
                     "event": "completed", "actor": f"bot:{F}/w1",
                     "pr_url": url, "pr_role": "reviewed"}},
        {"event_type": "system", "emitter": "report-back", "fleet": F,
         "source_ref": f"report-back:msg_{'6':0>32}", "occurred_at": ts,
         "payload": {"event": "report_status", "subject_kind": "actor",
                     "subject": f"bot:{F}/w2",
                     "data": {"status": "completed", "pr_url": url,
                              "pr_role": "reviewed"}}},
        {"event_type": "system", "emitter": "report-back", "fleet": F,
         "source_ref": f"report-back:msg_{'7':0>32}", "occurred_at": ts,
         "payload": {"event": "report_status", "subject_kind": "actor",
                     "subject": f"bot:{F}/w1",
                     "data": {"status": "completed", "pr_url": url,
                              "pr_role": "authored"}}},
    ])
    payload = {"number": 1046, "title": "review", "headRefOid": "b27ffc2c16e9dc3972332a550925b33f1b6143b1",
               "reviews": [{"submittedAt": "2026-09-02T13:59:52Z",
                            "body": "**[w1] [VERDICT] request-changes** reviewed against b27ffc2"}],
               "comments": [{"createdAt": "2026-09-02T13:59:52Z",
                             "body": "**[w1] [VERDICT] approve** reviewed against b27ffc2"}]}
    with sqlite3.connect(root / "state/plane/plane.db") as conn:
        result = who.assess_payloads(conn, [payload], "org/repo")
    assert result["rows"] == 2
    events = result["attribution_events"][0]["events"]
    assert [event["verdict"] for event in events] == ["AMBIGUOUS", "AMBIGUOUS"]
    assert {c["actor"] for c in events[0]["candidates"]} == {f"bot:{F}/w1", f"bot:{F}/w2"}
    assert result["prs"][0]["blocking"]  # copied header cannot clear an unresolved block
    assert result["prs"][0]["observed_attribution"]["complete"] is False
    assert result["prs"][0]["attribution"]["ambiguous"] == 2
    assert "identity is AMBIGUOUS" in review_rules.attribution_advice(
        result["prs"][0]["attribution"])


# ---------------------------------------------------------------------------
# #1537: a reviewed report that names its verdict's URL in --artifact joins that
# verdict exactly, with no time window. The window stays only as the fallback
# for a report that names no verdict URL, and every attribution says which of
# the two it used.
# ---------------------------------------------------------------------------

LREPO = "org/repo"
LPR = 77
LPR_URL = f"https://github.com/{LREPO}/pull/{LPR}"
HEAD = "c14e56506537c309c716c6553b0b1a1523f5eff5"
OLD = "c4d6fe8611111111111111111111111111111111"
_MSG = itertools.count(1)


def _comment_url(cid):
    return f"{LPR_URL}#issuecomment-{cid}"


def _review_url(rid):
    return f"{LPR_URL}#pullrequestreview-{rid}"


def _verdict(bot, word="approve", sha=HEAD):
    return f"**[{bot}] [VERDICT] {word}** — reviewed at {sha}"


def _payload(reviews=(), comments=()):
    return {"number": LPR, "title": "t", "headRefOid": HEAD,
            "reviews": list(reviews), "comments": list(comments)}


def _gh_comment(ts, body, cid):
    """A comment as `gh pr view --json comments` returns it: with its URL."""
    return {"createdAt": ts, "body": body, "url": _comment_url(cid)}


def _gh_review(ts, body, node):
    """A review as `gh pr view --json reviews` returns it: an opaque node id and
    no URL, so the REST listing is the only way to its #pullrequestreview link."""
    return {"submittedAt": ts, "body": body, "id": node, "state": "COMMENTED"}


def _reviewed(root, bot, ts, *artifacts, fleet="f", linked=False):
    """One reviewed report as the report door records it: the communication that
    carries the typed body (and its artifacts), plus the companion — a task
    event when linked, the report_status marker when not."""
    n = next(_MSG)
    link = ReportLink(f"wi_{n:032x}", f"asg_{n:032x}", "completed") if linked else None
    facts = encode_report_facts(
        ReportPayload("completed", summary=f"review {n}", pr_url=LPR_URL, pr_role="reviewed",
                      artifacts=tuple(artifacts)),
        fleet=fleet, sender=f"bot:{fleet}/{bot}", recipient=f"bot:{fleet}/mgr",
        msg_id=f"msg_{n:032x}", event_ids=(f"ev_{2 * n:032x}", f"ev_{2 * n + 1:032x}"),
        occurred_at=ts, link=link)
    emit_batch(root, list(facts), require_commit=True)


def _assess(root, payload, monkeypatch, review_urls=None):
    """assess_payloads over a real plane, with the REST review listing stubbed.
    A test that names no review URL must never make that read."""
    def listing(repo, number):
        assert (repo, number) == (LREPO, LPR)
        assert review_urls is not None, "the REST review listing ran with no review URL named"
        return dict(review_urls)
    monkeypatch.setattr(who, "fetch_review_urls", listing, raising=False)
    with ro(root) as conn:
        return who.assess_payloads(conn, [payload], LREPO)


def _events(result):
    return result["attribution_events"][0]["events"]


def _seen(event):
    return event["verdict"], event.get("actor"), event.get("method")


class TestTheFourMeasuredCases:
    """The four cases measured in #1537, each as its report would now be filed."""

    def test_case_1_a_careful_review_reported_332s_later_matches_by_url(self, tmp_path, monkeypatch):
        """#1535: the verdict comment at 22:17:01Z, the report at 22:22:33Z."""
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-09-10T22:22:33Z", _comment_url(101))
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-09-10T22:17:01Z", _verdict("w1"), 101)]), monkeypatch)
        [event] = _events(result)
        assert _seen(event) == ("MATCH", "bot:f/w1", "url")
        assert event.get("url") == _comment_url(101)
        assert result["prs"][0]["resolved"]["bot:f/w1"]["anchor"] == HEAD

    def test_case_2_a_late_reported_block_is_superseded_by_its_reviewers_approve(
            self, tmp_path, monkeypatch):
        """#1537's second case: a REQUEST-CHANGES review nobody could attribute
        outlived the same reviewer's later approve, as `UNKNOWN@reviews:0`."""
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-02T10:06:40Z", _review_url(501), linked=True)  # +400 s
        _reviewed(root, "w1", "2026-10-02T11:00:05Z", _review_url(502))
        payload = _payload(reviews=[
            _gh_review("2026-10-02T10:00:00Z", _verdict("w1", "mechanical fixes", OLD), "PRR_a"),
            _gh_review("2026-10-02T11:00:00Z", _verdict("w1"), "PRR_b")])
        result = _assess(root, payload, monkeypatch,
                         review_urls={"PRR_a": _review_url(501), "PRR_b": _review_url(502)})
        assert [_seen(e) for e in _events(result)] == [("MATCH", "bot:f/w1", "url")] * 2
        pr = result["prs"][0]
        assert pr["blocking"] == []
        assert pr["resolved"]["bot:f/w1"]["verdict"] == review_rules.APPROVE

    def test_case_3_a_paired_review_5s_apart_attributes_each_reviewer(self, tmp_path, monkeypatch):
        """#1537's third case: two reviewers re-anchor one head 5 s apart and
        each report falls in the other's window, so both read AMBIGUOUS."""
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-03T06:28:42Z", _comment_url(201))
        _reviewed(root, "w2", "2026-10-03T06:28:47Z", _comment_url(202), fleet="g")
        payload = _payload(comments=[
            _gh_comment("2026-10-03T06:28:39Z", _verdict("w1"), 201),
            _gh_comment("2026-10-03T06:28:44Z", _verdict("w2"), 202)])
        result = _assess(root, payload, monkeypatch)
        assert [_seen(e) for e in _events(result)] == [
            ("MATCH", "bot:f/w1", "url"), ("MATCH", "bot:g/w2", "url")]
        assert result["prs"][0]["observed_attribution"]["complete"] is True

    def test_case_4_a_corrected_verdict_matches_the_report_that_names_it(self, tmp_path, monkeypatch):
        """#2166: verdict A at 01:24:45Z had no anchor, and its report came 8 s
        later. The corrected verdict B at 01:26:48Z was 115 s after that report,
        past the -10 s bound, so B read UNKNOWN. A report that names B's URL
        attributes B however long the write-up takes (here 150 s)."""
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-06T01:24:53Z", _comment_url(301), linked=True)
        _reviewed(root, "w1", "2026-10-06T01:29:18Z", _comment_url(302))
        payload = _payload(comments=[
            _gh_comment("2026-10-06T01:24:45Z", f"**[w1] [VERDICT] approve** at `{HEAD[:8]}`", 301),
            _gh_comment("2026-10-06T01:26:48Z", _verdict("w1"), 302)])
        result = _assess(root, payload, monkeypatch)
        assert [_seen(e) for e in _events(result)] == [("MATCH", "bot:f/w1", "url")] * 2
        pr = result["prs"][0]
        assert pr["resolved"]["bot:f/w1"]["anchor"] == HEAD
        assert pr["unanchored"] == []


class TestUrlJoin:
    def test_a_comment_verdict_joins_the_report_that_names_its_url(self, tmp_path, monkeypatch):
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:10:00Z", _comment_url(701))  # +600 s
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-10-04T12:00:00Z", _verdict("w1"), 701)]), monkeypatch)
        [event] = _events(result)
        assert _seen(event) == ("MATCH", "bot:f/w1", "url")
        assert event["candidates"][0].get("url") == _comment_url(701)
        # No report named a review URL, so the REST listing was never needed.
        assert result["attribution_events"][0].get("review_urls", {}).get("state") == "not-needed"

    def test_a_review_verdict_joins_its_report_through_the_rest_listing(self, tmp_path, monkeypatch):
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:10:00Z", _review_url(601))  # +600 s
        result = _assess(root, _payload(reviews=[
            _gh_review("2026-10-04T12:00:00Z", _verdict("w1"), "PRR_x")]), monkeypatch,
            review_urls={"PRR_x": _review_url(601)})
        [event] = _events(result)
        assert _seen(event) == ("MATCH", "bot:f/w1", "url")
        assert event.get("url") == _review_url(601)
        assert result["attribution_events"][0].get("review_urls", {}).get("state") == "read"

    def test_two_bots_naming_one_verdict_url_is_ambiguous_not_a_guess(self, tmp_path, monkeypatch):
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:05:00Z", _comment_url(702))
        _reviewed(root, "w2", "2026-10-04T12:06:00Z", _comment_url(702))
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-10-04T12:00:00Z", _verdict("w1"), 702)]), monkeypatch)
        [event] = _events(result)
        assert (event["verdict"], event.get("method")) == ("AMBIGUOUS", "url")
        assert {c["actor"] for c in event["candidates"]} == {"bot:f/w1", "bot:f/w2"}

    def test_a_report_that_names_one_verdict_is_no_window_candidate_for_another(
            self, tmp_path, monkeypatch):
        """The window is only for a report that names no verdict URL. This report
        names A, and B landing 7 s after it does not make it B's report."""
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:00:08Z", _comment_url(703))
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-10-04T12:00:00Z", _verdict("w1", sha=HEAD[:8]), 703),
            _gh_comment("2026-10-04T12:00:15Z", _verdict("w1"), 704)]), monkeypatch)
        a, b = _events(result)
        assert _seen(a) == ("MATCH", "bot:f/w1", "url")
        assert (b["verdict"], b.get("method")) == ("UNKNOWN", "window")

    def test_a_report_naming_no_verdict_url_still_matches_in_the_window(self, tmp_path, monkeypatch):
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:00:12Z", "https://example.com/ci/run/9")
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-10-04T12:00:00Z", _verdict("w1"), 705)]), monkeypatch)
        [event] = _events(result)
        assert _seen(event) == ("MATCH", "bot:f/w1", "window")

    def test_a_withheld_report_body_falls_back_to_the_window_and_says_why(self, tmp_path, monkeypatch):
        """Under metadata capture the artifacts are withheld with the body, so
        there is no URL to join; the report stays a window candidate."""
        root = plane_root(tmp_path, capture='{"*": "metadata"}', initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:00:05Z", _comment_url(706))
        result = _assess(root, _payload(comments=[
            _gh_comment("2026-10-04T12:00:00Z", _verdict("w1"), 706)]), monkeypatch)
        [event] = _events(result)
        assert _seen(event) == ("MATCH", "bot:f/w1", "window")
        assert event["candidates"][0].get("content") == "withheld"


def _rest_gh(stdout="", returncode=0, stderr="", raises=None, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


class TestRestReviewListing:
    """The extra read is REST, which throttles apart from gh pr view's GraphQL.
    Its status is checked unpiped and its body is read (#1066)."""

    @pytest.mark.parametrize("failure", ["http-403", "timeout", "error-body", "not-json"])
    def test_a_failed_listing_falls_back_to_the_window_and_says_so_per_verdict(
            self, tmp_path, monkeypatch, failure):
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T12:00:05Z", _review_url(801))  # inside the window
        _reviewed(root, "w2", "2026-10-04T13:10:00Z", _review_url(802))  # 600 s late
        payload = _payload(reviews=[
            _gh_review("2026-10-04T12:00:00Z", _verdict("w1"), "PRR_1"),
            _gh_review("2026-10-04T13:00:00Z", _verdict("w2"), "PRR_2")])
        run = {
            "http-403": _rest_gh('{"message":"API rate limit exceeded"}', 1,
                                 "gh: API rate limit exceeded (HTTP 403)\n"),
            "timeout": _rest_gh(raises=subprocess.TimeoutExpired(["gh"], 60)),
            # Exit 0 and a JSON body that is not a listing: read the body (#1066).
            "error-body": _rest_gh('{"message":"Not Found"}'),
            "not-json": _rest_gh("<html>unavailable</html>"),
        }[failure]
        monkeypatch.setattr(who.subprocess, "run", run)
        with ro(root) as conn:
            result = who.assess_payloads(conn, [payload], LREPO)
        first, second = _events(result)
        assert _seen(first) == ("MATCH", "bot:f/w1", "window-after-failure")
        assert (second["verdict"], second.get("method")) == ("UNKNOWN", "window-after-failure")
        state = result["attribution_events"][0].get("review_urls", {})
        assert state.get("state") == "unavailable" and state.get("error")
        assert first.get("fallback_reason") == second.get("fallback_reason") == state["error"]
        if failure == "http-403":
            assert "403" in state["error"]

    def test_the_listing_is_paged_100_at_a_time(self, monkeypatch):
        calls = []
        monkeypatch.setattr(who.subprocess, "run", _rest_gh("[]", calls=calls))
        assert who.fetch_review_urls(LREPO, LPR) == {}
        assert calls == [["gh", "api", "--paginate", f"repos/{LREPO}/pulls/{LPR}/reviews?per_page=100"]]

    @pytest.mark.parametrize("pages", ["merged", "concatenated"])
    def test_a_review_past_the_first_page_is_not_lost(self, tmp_path, monkeypatch, pages):
        """A PR with more reviews than one page: the 101st still joins. gh 2.92
        merges REST array pages into one array; older gh prints them one after
        another. Both parse to every review."""
        listing = [{"id": 900 + i, "node_id": f"PRR_{i}", "html_url": _review_url(900 + i)}
                   for i in range(101)]
        stdout = (json.dumps(listing) if pages == "merged"
                  else json.dumps(listing[:100]) + json.dumps(listing[100:]))
        root = plane_root(tmp_path, initialize=True)
        _reviewed(root, "w1", "2026-10-04T14:00:00Z", _review_url(1000))
        monkeypatch.setattr(who.subprocess, "run", _rest_gh(stdout))  # after the emit
        reviews = [_gh_review("2026-10-04T12:00:00Z", "a note, not a verdict", f"PRR_{i}")
                   for i in range(100)]
        reviews.append(_gh_review("2026-10-04T13:00:00Z", _verdict("w1"), "PRR_100"))
        with ro(root) as conn:
            result = who.assess_payloads(conn, [_payload(reviews=reviews)], LREPO)
        assert _seen(_events(result)[-1]) == ("MATCH", "bot:f/w1", "url")
        assert result["attribution_events"][0]["review_urls"] == {
            "state": "read", "reviews": 101, "error": None}


class TestVerdictUrlKey:
    def test_comment_and_review_urls(self):
        assert who.verdict_url_key(_comment_url(5), LREPO, LPR) == ("issuecomment", "5")
        assert who.verdict_url_key(_review_url(6), LREPO, LPR) == ("pullrequestreview", "6")

    def test_the_issues_route_names_the_same_comment(self):
        url = f"https://github.com/{LREPO}/issues/{LPR}#issuecomment-5"
        assert who.verdict_url_key(url, LREPO, LPR) == ("issuecomment", "5")

    def test_owner_and_repo_case_do_not_matter(self):
        url = f"https://github.com/ORG/Repo/pull/{LPR}#issuecomment-5"
        assert who.verdict_url_key(url, LREPO, LPR) == ("issuecomment", "5")

    @pytest.mark.parametrize("url", [
        f"https://github.com/org/other/pull/{LPR}#issuecomment-5",       # another repository
        f"https://github.com/xorg/repo/pull/{LPR}#issuecomment-5",       # an owner ending in ours
        f"https://github.com/org/repo/pull/{LPR}1#issuecomment-5",       # a longer PR number
        f"https://github.com/org/repo/pull/{LPR}#discussion_r5",         # an inline comment
        f"https://github.com/org/repo/pull/{LPR}",                       # no verdict fragment
        f"https://github.com/org/repo/issues/{LPR}#pullrequestreview-5", # a review on an issue path
        f"https://gist.github.com/org/repo/pull/{LPR}#issuecomment-5",   # another host
        f"http://github.com/org/repo/pull/{LPR}#issuecomment-5",         # not https
        "not a url",
    ])
    def test_anything_else_names_no_verdict(self, url):
        assert who.verdict_url_key(url, LREPO, LPR) is None
