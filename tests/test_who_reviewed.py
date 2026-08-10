"""Unit tests for lib/who-reviewed.py — attributing a PR review to the bot that
wrote it, when a shared GitHub PAT makes every review read `chrisrogers37`.

The two rules under test are the ones that came from the manual version failing:
a bare number must never match, and the ledger row lands seconds after the
review so an exact-equality join finds nothing.
"""

from __future__ import annotations

import json

from tests.conftest import (
    load_lib_module,
    report_row as _report,
    write_jsonl as _write_jsonl,
)

who = load_lib_module("who-reviewed")

REPO = "Claudfather/Claudlobby"
URL = f"https://github.com/{REPO}/pull/1046"


def _rows(*rows):
    """Attach the fleet marker load_ledger would add, without touching disk."""
    out = []
    for row, fleet in rows:
        out.append({**row, "_fleet": fleet, "_ledger": f"/fake/{fleet}"})
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
        """`pull/10461` must not answer a query for 1046 — the \\b trap."""
        q, b = who.pr_patterns(REPO, 1046)
        row = _report(
            "vera",
            "2026-08-06T14:22:17Z",
            pr_url=f"https://github.com/{REPO}/pull/10461",
        )
        assert who.row_pr_match(row, q, b) is None

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


class TestLedgerLoading:
    def test_bad_lines_counted_not_silently_dropped(self, tmp_path):
        path = tmp_path / "report-back.jsonl"
        path.write_text(
            json.dumps(_report("vera", "2026-08-06T14:22:17Z", pr_url=URL))
            + "\n"
            + "{not json\n"
            + "[]\n",
            encoding="utf-8",
        )
        rows, bad = who.load_ledger(str(path), "ai-platform")
        assert len(rows) == 1
        assert bad == 2

    def test_missing_ledger_reports_unreadable(self, tmp_path):
        rows, bad = who.load_ledger(str(tmp_path / "nope.jsonl"), "ghost")
        assert rows == []
        assert bad == -1

    def test_fleet_marker_attached(self, tmp_path):
        path = tmp_path / "report-back.jsonl"
        _write_jsonl(path, [_report("vera", "2026-08-06T14:22:17Z", pr_url=URL)])
        rows, _ = who.load_ledger(str(path), "ai-platform")
        assert rows[0]["_fleet"] == "ai-platform"


class TestDiscovery:
    def test_finds_flat_nested_and_root_layouts(self, tmp_path):
        flat = tmp_path / "local" / "alpha" / "runtime"
        nested = tmp_path / "local" / "home" / "ai-platform" / "runtime"
        rootm = tmp_path / "runtime" / "fleet"
        for d in (flat, nested, rootm):
            d.mkdir(parents=True)
            (d / "report-back.jsonl").write_text("", encoding="utf-8")
        found = dict((f, p) for f, p in who.discover_ledgers(str(tmp_path)))
        assert "alpha" in found
        assert "ai-platform" in found
        assert "(root)" in found

    def test_no_ledgers_is_empty_not_error(self, tmp_path):
        assert who.discover_ledgers(str(tmp_path)) == []


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
    def test_refuses_without_any_ledger_source(self, capsys, monkeypatch, tmp_path):
        """Refusing beats reporting every review as UNKNOWN — a false all-clear
        is exactly the wrong-attribution class this module exists to stop."""
        monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)
        payload = tmp_path / "p.json"
        payload.write_text(
            json.dumps({"reviews": [], "comments": []}), encoding="utf-8"
        )
        rc = who.main([REPO, "1046", "--reviews-json", str(payload), "--root", ""])
        assert rc == 4
        assert "refusing" in capsys.readouterr().err

    def test_end_to_end_json(self, capsys, tmp_path):
        ledger = tmp_path / "local" / "home" / "ai-platform" / "runtime"
        ledger.mkdir(parents=True)
        _write_jsonl(
            ledger / "report-back.jsonl",
            [_report("vera", "2026-08-06T14:22:17Z", pr_url=URL)],
        )
        payload = tmp_path / "p.json"
        payload.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        rc = who.main(
            [
                REPO,
                "1046",
                "--reviews-json",
                str(payload),
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["events"][0]["bot"] == "vera"
        assert result["events"][0]["fleet"] == "ai-platform"
        assert result["scope"]["rows"] == 1

    def test_bad_repo_arg_rejected(self, capsys):
        assert who.main(["notarepo", "1046"]) == 2
