"""Unit tests for review_queries — attributing a PR review to the bot that
wrote it, when a shared GitHub PAT makes every review read `chrisrogers37`.

The two rules under test are the ones that came from the manual version failing:
a bare number must never match, and the report lands seconds after the review so
an exact-equality join finds nothing.

The Plane is the only attribution source. Pure matching tests retain the
historical report-row examples; the current query tests explicit review roles.
"""

from __future__ import annotations

from tests.conftest import report_row as _report
from claudlobby import review_queries as who, review_rules

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
            (_report("vera", "2026-08-06T14:22:20Z", pr_url=URL), "tl-enterprises"),
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
