"""Unit tests for lib/who-reviewed.py — attributing a PR review to the bot that
wrote it, when a shared GitHub PAT makes every review read `chrisrogers37`.

The two rules under test are the ones that came from the manual version failing:
a bare number must never match, and the report lands seconds after the review so
an exact-equality join finds nothing.

F18 closure (R2b): the plane is the module's ONLY source. The join is pure over
row dicts, so the rule pins below feed it rows directly; the CLI pins land a
report on a throwaway plane as the report door lands it. Deleted with the
ledgers: TestLedgerLoading (test_bad_lines_counted_not_silently_dropped,
test_missing_ledger_reports_unreadable, test_fleet_marker_attached),
TestDiscovery (test_finds_flat_nested_and_root_layouts,
test_no_ledgers_is_empty_not_error).
"""

from __future__ import annotations

import sys

import subprocess

import json

import pytest

from tests.conftest import load_lib_module, report_row as _report
from tests.plane_fixtures import _dispatch, _report as _land_report, plane_root

who = load_lib_module("who-reviewed")

REPO = "Claudfather/Claudlobby"
URL = f"https://github.com/{REPO}/pull/1046"


# One review at the instant of the real #1046 pair (14:22:05Z), as `gh` renders it.
_PAYLOAD = {
    "reviews": [
        {
            "submittedAt": "2026-08-06T14:22:05Z",
            "state": "COMMENTED",
            "author": {"login": "chrisrogers37"},
            "body": "verdict",
        }
    ],
    "comments": [],
}


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
        """The case the qualified lookbehind must still block. `(?<!\\w)` allows
        the `/` that always precedes the owner in a URL, but a longer owner ends
        in a word character and is correctly refused a qualified match."""
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url="https://github.com/NotClaudfather/Claudlobby/pull/1046",
        )
        assert who.row_pr_match(row, q, b) == ("pr_url", False)

    def test_other_repo_is_bare_not_qualified(self):
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url="https://github.com/Other/Repo/pull/1046",
        )
        assert who.row_pr_match(row, q, b) == ("pr_url", False)

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
        events = who.events_from_payload(payload)
        assert [e["kind"] for e in events] == ["review", "comment"]
        assert events[0]["excerpt"] == "**Request Changes**"

    def test_empty_payload_is_no_events(self):
        assert who.events_from_payload({}) == []


class TestCli:
    def test_refuses_without_a_root(self, capsys, monkeypatch, tmp_path):
        """Refusing beats reporting every review as UNKNOWN — a false all-clear
        is exactly the wrong-attribution class this module exists to stop."""
        monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)
        payload = tmp_path / "p.json"
        payload.write_text(json.dumps({"reviews": [], "comments": []}), encoding="utf-8")
        rc = who.main([REPO, "1046", "--reviews-json", str(payload), "--root", ""])
        assert rc == 4
        assert "refusing" in capsys.readouterr().err

    def test_unreachable_plane_refuses_not_unknown(self, capsys, tmp_path):
        """A root with no plane db is UNREACHABLE — not a fleet that never
        reported. rc 4, empty stdout, the reason on stderr."""
        root = tmp_path / "root"
        (root / "state" / "plane").mkdir(parents=True)
        payload = tmp_path / "p.json"
        payload.write_text(json.dumps(_PAYLOAD), encoding="utf-8")
        rc = who.main([REPO, "1046", "--reviews-json", str(payload), "--root", str(root), "--json"])
        captured = capsys.readouterr()
        assert rc == 4 and captured.out == "" and "unreachable" in captured.err

    def test_end_to_end_json(self, capsys, tmp_path):
        """The regression pair, landed as the report door lands it: the review
        posts at 14:22:05Z, vera's report lands at 14:22:17Z (+12s)."""
        root = plane_root(tmp_path)
        wi, asg = _dispatch(root, "1", "t-1-aaaa", "2026-08-06T14:00:00Z", bot="vera")
        _land_report(root, wi, asg, "2026-08-06T14:22:17Z", bot="vera",
                     extra={"pr_url": URL, "summary": "Request Changes on #1046"})
        payload = tmp_path / "p.json"
        payload.write_text(json.dumps(_PAYLOAD), encoding="utf-8")
        rc = who.main([REPO, "1046", "--reviews-json", str(payload), "--root", str(root), "--json"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        event = result["events"][0]
        assert event["verdict"] == "MATCH"
        assert (event["bot"], event["fleet"], event["basis"]["delta_s"]) == ("vera", "f", 12)
        assert event["basis"]["field"] == "pr_url" and event["basis"]["repo_qualified"] is True
        assert result["scope"]["rows"] == 1 and result["scope"]["fleets"] == ["f"]
        assert result["scope"]["source"] == "plane" and result["scope"]["plane"].endswith("plane.db")

    def test_text_scope_names_the_plane_and_the_fleets(self, capsys, tmp_path):
        root = plane_root(tmp_path)
        wi, asg = _dispatch(root, "1", "t-1-aaaa", "2026-08-06T14:00:00Z", bot="vera")
        _land_report(root, wi, asg, "2026-08-06T14:22:17Z", bot="vera",
                     extra={"pr_url": URL, "summary": "x"})
        payload = tmp_path / "p.json"
        payload.write_text(json.dumps(_PAYLOAD), encoding="utf-8")
        assert who.main([REPO, "1046", "--reviews-json", str(payload), "--root", str(root)]) == 0
        out = capsys.readouterr().out
        assert "plane: " in out and "plane.db" in out and "fleets: f" in out
        assert "→ vera (f)" in out and "report 2026-08-06T14:22:17Z (+12s)" in out

    def test_the_retired_source_seam_is_refused(self, tmp_path):
        """`--source` and `--ledger` went with the ledgers (F18 R2b); a stale
        caller must hear it rather than be silently served the plane."""
        for stale in (["--source", "plane"], ["--ledger", "/x.jsonl"]):
            with pytest.raises(SystemExit) as exc:
                who.main([REPO, "1046", *stale, "--root", str(tmp_path)])
            assert exc.value.code == 2

    def test_bad_repo_arg_rejected(self, capsys):
        assert who.main(["notarepo", "1046"]) == 2


# --- the plane join, moved from tests/test_plane_cutover_retire.py (dissolved in R3) ---
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import F as PLANE_FLEET, REPO as REPO_ROOT, _rrow, _scene


def test_who_reviewed_attributes_from_the_plane_like_the_ledger(tmp_path):
    wr = who
    root, paths, _, r = _scene(tmp_path)
    ts = "2026-09-02T14:00:00Z"
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": PLANE_FLEET,
                       "source_ref": f"report-back:msg_{'5':0>32}", "occurred_at": ts,
                       "payload": {"work_item_id": f"wi_{'2':0>32}", "assignment_id": f"asg_{'2':0>32}",
                                   "event": "completed", "actor": f"bot:{PLANE_FLEET}/w1",
                                   "pr_url": "https://github.com/org/repo/pull/1046", "summary": "Request Changes on #1046"}}])
    ledger_rows = [{**_rrow(ts, "t-2-bbbb", "completed", pr_url="https://github.com/org/repo/pull/1046",
                            summary="Request Changes on #1046"), "_fleet": PLANE_FLEET, "_ledger": "ledger"}]
    plane_rows, why = wr.load_plane_rows(str(root))
    assert why is None and len(plane_rows) == 1
    assert {k: plane_rows[0][k] for k in ("bot", "pr_url", "task_id", "status", "_fleet")} == \
        {"bot": "w1", "pr_url": "https://github.com/org/repo/pull/1046", "task_id": "t-2-bbbb",
         "status": "completed", "_fleet": PLANE_FLEET}
    events = [{"ts": "2026-09-02T13:59:52Z", "state": "CHANGES_REQUESTED", "kind": "review"}]
    from_ledger = wr.attribute(events, ledger_rows, "org/repo", 1046)
    from_plane = wr.attribute(events, plane_rows, "org/repo", 1046)
    assert from_ledger[0]["verdict"] == from_plane[0]["verdict"] == "MATCH"
    assert from_plane[0]["candidates"][0]["bot"] == "w1"
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps({"reviews": [], "comments": []}))
    ok = subprocess.run([sys.executable, str(REPO_ROOT / "lib" / "who-reviewed.py"), "org/repo", "1046",
                         "--root", str(root), "--reviews-json", str(reviews), "--json"],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["scope"]["source"] == "plane"
    (root / "state" / "plane" / "plane.db").unlink()
    rows, why = wr.load_plane_rows(str(root))
    assert rows == [] and "no plane db" in why                                     # unreachable ≠ empty
    gone = subprocess.run([sys.executable, str(REPO_ROOT / "lib" / "who-reviewed.py"), "org/repo", "1046",
                           "--root", str(root), "--reviews-json", str(reviews)],
                          capture_output=True, text=True, timeout=60)
    assert gone.returncode == 4 and gone.stdout == "" and "unreachable" in gone.stderr
