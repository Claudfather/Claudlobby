"""Unit tests for lib/pr-review-state.py — is a PR's blocking verdict still live?

Every fixture that pins a REGEX is REAL text taken from a live verdict, never
synthetic. That is the whole lesson of `test_who_reviewed.py`'s inert-mutant
test: a matcher sampled from one phrasing passes every example its author
imagined and misses the one somebody actually typed. Synthetic fixtures are
written by the same person, at the same sitting, under the same idea of what a
verdict looks like — so they cannot surprise you about the shape of real input.

Provenance of the fixtures below:
  Claudlobby#1311  reviews[0].body  "**Request Changes**"   (no SHA anchor)
  Claudlobby#1311  reviews[1].body  "**Approve**" + "Re-reviewed at b27ffc2."
                                    + a decoy hex "(7a49f7c)" in ordinary prose
  head at the time: b27ffc2c16e9dc3972332a550925b33f1b6143b1
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_lib_module

prs = load_lib_module("pr-review-state")

# ---- REAL fixtures, copied verbatim from Claudlobby#1311 --------------------

REAL_BLOCK = "**Request Changes**"
REAL_APPROVE_HEADER = "**Approve**"
REAL_ANCHOR_LINE = (
    "Re-reviewed at b27ffc2. Both changes address the round-1 blocking finding "
    "(`fleet-update-lifecycle.md:100-102`) directly, with evidence, not just "
    "claim-deletion."
)
REAL_DECOY_LINE = (
    "- Mutation-tested the two new positive tests: swapped the pre-fix (7a49f7c) "
    "doc back in, re-ran the suite"
)
REAL_APPROVE_BODY = f"{REAL_APPROVE_HEADER}\n\n{REAL_ANCHOR_LINE}\n\n{REAL_DECOY_LINE}\n"
REAL_HEAD = "b27ffc2c16e9dc3972332a550925b33f1b6143b1"

# The older estate phrasing and the newer one that broke it in an afternoon.
REAL_OLD_FORMAT = "**Verdict: Ship it**"
REAL_NEW_FORMAT = "**[branden] [VERDICT] approve**"


def _payload(events, head=REAL_HEAD, number=1311, title="t"):
    """Build a `gh pr view` shaped payload. (surface, ts, body) tuples."""
    reviews, comments = [], []
    for surface, ts, body in events:
        (reviews if surface == "reviews" else comments).append(
            {"submittedAt": ts, "createdAt": ts, "body": body}
        )
    return {"number": number, "title": title, "headRefOid": head,
            "reviews": reviews, "comments": comments}


class TestVerdictHeaderRegex:
    """Bound (a): the header regex is SAMPLED, not specified. These pin the samples."""

    @pytest.mark.parametrize(
        "body,want",
        [
            (REAL_BLOCK, "REQUEST-CHANGES"),
            (REAL_APPROVE_HEADER, "APPROVE"),
            (REAL_OLD_FORMAT, "APPROVE"),
            (REAL_NEW_FORMAT, "APPROVE"),
            ("**[rajan] [VERDICT] request-changes**", "REQUEST-CHANGES"),
            ("**Approve.**", "APPROVE"),
        ],
    )
    def test_both_live_estate_formats_parse(self, body, want):
        assert prs.parse_verdict(body) == want

    def test_a_comment_discussing_a_verdict_is_not_a_verdict(self):
        """The `[^*]*?` defect, pinned — with fixtures chosen by MEASUREMENT.

        The permissive form allows newlines, so it matched from a CLOSING `**`
        through ordinary prose to a later OPENING `**`: the matched span and the
        rendered bold span diverged, and a note *about* a verdict parsed AS one.

        The obvious fixture — a verdict word in prose between two bold spans on
        SEPARATE lines — does NOT discriminate. Measured: both forms reject it,
        because the permissive one is stopped by the *trailing* `[^*\n]{0,40}?`
        rather than by the leading bound the test claims to pin. A mutation
        swapping the leading bound back survives it, so the test would have named
        one thing and checked another.

        What discriminates is a verdict word with a bold span later on the SAME
        line and the opening `**` on an earlier one — the shape that actually
        made a merge note parse as a verdict. Both cases below were confirmed to
        match under the permissive form before being used here.
        """
        # Neutral: both forms reject this. Kept to document what does NOT work,
        # so nobody re-adds it believing it covers the leading bound.
        assert prs.parse_verdict(
            "**Merge note**\n\nThe reviewer will approve once CI clears.\n\n**Status**\n"
        ) is None
        # Discriminating: the permissive form MATCHES both of these.
        for body in (
            "**Round 2**\n\nSuperseding my earlier request-changes **verdict** here.\n",
            "**Context**\n\nI would approve this **today** if CI were green.\n",
        ):
            assert prs.parse_verdict(body) is None, body

    def test_an_unparsed_header_is_reported_verbatim_not_dropped(self):
        """Drift must be VISIBLE. A vocabulary gap that prints nothing is
        indistinguishable from an estate with no verdicts."""
        body = "**[zed] [DECISION] looks-fine**\n\nsome prose\n"
        assert prs.parse_verdict(body) is None
        assert prs.first_bold(body) == "**[zed] [DECISION] looks-fine**"


class TestShaAnchorRegex:
    """Defect 2: one sampled phrasing missed a real anchor and under-claimed."""

    def test_both_real_anchor_phrasings_are_found(self):
        assert prs.parse_anchor("reviewed against `ee29406`") == "ee29406"
        assert prs.parse_anchor(REAL_ANCHOR_LINE) == "b27ffc2"

    def test_a_decoy_hex_in_prose_is_not_an_anchor(self):
        """The anchor matcher is verb-anchored, and this is why.

        Claudlobby#1311's real approve body carries a genuine anchor AND a hex in
        ordinary prose. A bare-hex matcher is not merely imprecise here — it takes
        whichever comes first and would report the verdict stale against a commit
        nobody reviewed.
        """
        assert prs.parse_anchor(REAL_DECOY_LINE) is None
        assert prs.parse_anchor(REAL_APPROVE_BODY) == "b27ffc2"

    def test_an_unanchored_verdict_is_unknowable_never_clean(self):
        """Bound (b), the one that decides what a clean run is worth."""
        result = prs.assess_pr(_payload([("reviews", "2026-08-21T03:57:14Z", REAL_BLOCK)]))
        assert result["unanchored"], "an anchorless verdict must be reported"
        assert prs.NO_SHA_ANCHOR in result["flags"]
        assert prs.exit_code_for([result]) != prs.RC_OK


class TestPerReviewerResolution:
    """Defect 1: latest-wins is correct on #1311 BY ACCIDENT."""

    def _two(self, first_who, second_who):
        return prs.assess_pr(
            _payload(
                [
                    ("reviews", "2026-08-21T03:57:14Z",
                     f"**[{first_who}] [VERDICT] request-changes** reviewed against `{REAL_HEAD[:7]}`"),
                    ("reviews", "2026-08-21T04:39:06Z",
                     f"**[{second_who}] [VERDICT] approve** reviewed against `{REAL_HEAD[:7]}`"),
                ]
            )
        )

    def test_same_reviewer_resolving_themselves_is_approved(self):
        """The real Claudlobby#1311 shape: vera blocks, vera approves later."""
        assert self._two("vera", "vera")["blocking"] == []

    def test_reversing_the_reviewers_flips_the_answer(self):
        """The case latest-wins passes without handling.

        Identical event ORDER, identical timestamps, identical verdicts — only the
        authors differ. Latest-wins returns APPROVE for both; per-reviewer keeps
        the block alive here. If this test ever agrees with the one above, the
        resolution has silently reverted to latest-wins.
        """
        assert self._two("alpha", "beta")["blocking"] == ["alpha"]

    def test_unattributed_disagreeing_verdicts_keep_the_block_and_say_why(self):
        """No identity: same-reviewer-resolved and two-reviewers-blocked are
        byte-identical. Keep the block (safe direction) but flag it as
        unresolvable rather than confirmed."""
        result = prs.assess_pr(
            _payload(
                [
                    ("reviews", "2026-08-21T03:57:14Z", f"{REAL_BLOCK}\n\nreviewed against `{REAL_HEAD[:7]}`"),
                    ("reviews", "2026-08-21T04:39:06Z", REAL_APPROVE_BODY),
                ]
            )
        )
        assert result["blocking"], "an unresolvable block must stay live"
        assert prs.UNATTRIBUTED in result["flags"]


class TestStaleness:
    def test_head_moved_since_the_verdict_is_stale(self):
        result = prs.assess_pr(
            _payload([("reviews", "2026-08-21T04:39:06Z",
                       "**[vera] [VERDICT] approve** reviewed against `ee29406`")],
                     head="81803be" + "0" * 33))
        assert result["stale"] and result["stale"][0]["anchor"] == "ee29406"
        assert prs.exit_code_for([result]) == prs.RC_ACTIONABLE

    def test_anchor_matching_head_is_not_stale(self):
        result = prs.assess_pr(
            _payload([("reviews", "2026-08-21T04:39:06Z",
                       "**[vera] [VERDICT] approve** " + REAL_ANCHOR_LINE)]))
        assert result["stale"] == []
        assert prs.exit_code_for([result]) == prs.RC_OK


class TestIdentity:
    """Requirement 5: header is self-reported, ledger is observed."""

    def test_header_identity_is_parsed_when_present(self):
        assert prs.parse_header_identity(REAL_NEW_FORMAT) == "branden"
        assert prs.parse_header_identity(REAL_BLOCK) is None

    def test_header_and_ledger_disagreement_is_reported_never_resolved(self):
        """A bot copying a verdict template writes whatever the template said, so
        the header can name someone who did not write it. Picking a winner is how
        a wrong attribution makes a reader ACT; DISAGREEMENT only makes them look."""
        payload = _payload([("reviews", "2026-08-21T04:39:06Z",
                             "**[branden] [VERDICT] approve** " + REAL_ANCHOR_LINE)])
        result = prs.assess_pr(payload, ledger_identity={"2026-08-21T04:39:06Z": "vera"})
        assert prs.DISAGREEMENT in result["flags"]
        assert "branden" not in result["resolved"] and "vera" not in result["resolved"]

    def test_agreement_uses_the_name(self):
        payload = _payload([("reviews", "2026-08-21T04:39:06Z",
                             "**[vera] [VERDICT] approve** " + REAL_ANCHOR_LINE)])
        result = prs.assess_pr(payload, ledger_identity={"2026-08-21T04:39:06Z": "vera"})
        assert list(result["resolved"]) == ["vera"]


class TestFailureDirection:
    """Requirement 4: a reader who greps STALE and finds nothing must not be able
    to conclude nothing is stale."""

    def test_exit_codes_rank_actionable_over_incomplete_over_ok(self):
        clean = prs.assess_pr(_payload([("reviews", "t1",
                                         "**[v] [VERDICT] approve** " + REAL_ANCHOR_LINE)]))
        incomplete = prs.assess_pr(_payload([("reviews", "t1", REAL_APPROVE_HEADER)]))
        actionable = prs.assess_pr(
            _payload([("reviews", "t1", "**[v] [VERDICT] approve** reviewed against `ee29406`")],
                     head="81803be" + "0" * 33))
        assert prs.exit_code_for([clean]) == prs.RC_OK
        assert prs.exit_code_for([incomplete]) == prs.RC_INCOMPLETE
        assert prs.exit_code_for([actionable]) == prs.RC_ACTIONABLE
        assert prs.exit_code_for([clean, incomplete, actionable]) == prs.RC_ACTIONABLE

    def test_summary_states_the_anchored_denominator_when_coverage_is_partial(self):
        incomplete = prs.assess_pr(_payload([("reviews", "t1", REAL_APPROVE_HEADER)]))
        line = prs.summary_line([incomplete])
        assert "0/1 anchored" in line
        assert "UNKNOWABLE" in line, "partial coverage must say so in the summary"

    def test_summary_counts_resolved_verdicts_not_superseded_ones(self):
        """A superseded verdict was never assessed, so counting it claimed coverage
        the run did not have. Real #1311 shape: 2 verdict events, 1 live."""
        result = prs.assess_pr(
            _payload([("reviews", "2026-08-21T03:57:14Z", f"**[vera] [VERDICT] request-changes**"),
                      ("reviews", "2026-08-21T04:39:06Z", "**[vera] [VERDICT] approve** " + REAL_ANCHOR_LINE)]))
        line = prs.summary_line([result])
        assert "1 live verdict(s) (1 superseded)" in line
        assert "1/1 anchored" in line


class TestOfflineSeam:
    """Requirement 1: every rule is reachable with no network."""

    def test_end_to_end_on_the_real_1311_payload(self, tmp_path):
        payload = _payload(
            [("reviews", "2026-08-21T03:57:14Z", REAL_BLOCK),
             ("reviews", "2026-08-21T04:39:06Z", REAL_APPROVE_BODY)])
        path = tmp_path / "p.json"
        path.write_text(json.dumps(payload))
        rc = prs.main(["Claudfather/Claudlobby", "--payload-json", str(path)])
        assert rc == prs.RC_ACTIONABLE  # unattributed block stays live

    def test_json_mode_carries_the_rc_and_summary(self, tmp_path, capsys):
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_payload([("reviews", "t1", REAL_APPROVE_HEADER)])))
        rc = prs.main(["o/r", "--payload-json", str(path), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert out["schema"] == 1 and out["rc"] == rc == prs.RC_INCOMPLETE
        assert "UNKNOWABLE" in out["summary"]

    def test_unreadable_payload_is_usage_error_not_a_clean_run(self, tmp_path):
        assert prs.main(["o/r", "--payload-json", str(tmp_path / "nope.json")]) == prs.RC_USAGE


class TestSelftest:
    """The on-invocation positive control, kept from the prototype.

    `tests/` runs in CI; `selftest()` runs on the operator's machine at the moment
    they read the output. A clean estate and a dead detector both print nothing,
    so the fixture has to fire independently of whether the estate is dirty.
    """

    def test_selftest_passes_on_the_shipped_regexes(self):
        prs.selftest()  # raises AssertionError if a sampled format stopped parsing

    def test_main_runs_the_selftest(self, tmp_path, monkeypatch):
        """Pin the WIRING, not just the function.

        Found by mutation: deleting the `selftest()` call from `main` broke no
        test, so the control could have been silently unwired while every unit
        test stayed green — a positive control that is not reached is not a
        control.
        """
        called = []
        monkeypatch.setattr(prs, "selftest", lambda: called.append(True))
        path = tmp_path / "p.json"
        path.write_text(json.dumps(_payload([("reviews", "t1", REAL_APPROVE_HEADER)])))
        prs.main(["o/r", "--payload-json", str(path)])
        assert called, "main() must run the self-test before reporting anything"

    def test_selftest_fails_loudly_if_a_sampled_format_stops_parsing(self, monkeypatch):
        """The control must be able to FAIL, or it certifies nothing."""
        monkeypatch.setattr(prs, "parse_verdict", lambda body: None)
        with pytest.raises(AssertionError):
            prs.selftest()


class TestDriftSignalIsNarrow:
    """A drift signal that cries wolf trains people to ignore its one real instance.

    All three headers below are REAL bold-led comments from Claudlobby#1160. The
    first version of the UNPARSED rule flagged any comment leading with `**`, so a
    live run reported three vocabulary drifts on a PR that had none.
    """

    REAL_NON_VERDICT_HEADERS = [
        "**Status: blocked on a policy decision, not awaiting a reviewer.**",
        "**The merge-policy question was ratified on 2026-07-02:**",
        "**Escalating rather than ruling.**",
    ]

    @pytest.mark.parametrize("header", REAL_NON_VERDICT_HEADERS)
    def test_ordinary_bold_comments_are_not_reported_as_drift(self, header):
        result = prs.assess_pr(_payload([("comments", "t1", header + "\n\nprose\n")]))
        assert result["unparsed_headers"] == []

    @pytest.mark.parametrize(
        "header",
        [
            "**[zed] [DECISION] looks-fine**",   # bracket-tagged: the shape that drifted
            "**Verdict: looks fine**",            # the older family, new vocabulary
        ],
    )
    def test_a_genuinely_new_verdict_vocabulary_still_surfaces(self, header):
        """Narrowing must not cost the signal. Both live families stay covered."""
        result = prs.assess_pr(_payload([("comments", "t1", header + "\n\nprose\n")]))
        assert result["unparsed_headers"] == [header]
        assert prs.UNPARSED in result["flags"]
        assert prs.exit_code_for([result]) == prs.RC_INCOMPLETE
