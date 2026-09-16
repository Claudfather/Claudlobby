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

    #: Real multi-hex verdict bodies from THIS repo — Claudlobby #1160 and #1311.
    #: Verbatim excerpts. Sourced from our own corpus deliberately: an earlier
    #: version quoted another fleet's repos, which are outside this fleet's
    #: declared scope. Ours are also the better fixtures — they show the decoy
    #: arising two different ways, and neither hex is a commit anyone reviewed.
    MULTI_HEX = [
        ("#1160 two base-refs",
         "of the claims I was asked to check, against `main` @ `560c3c9` (the "
         "plan's own pin — note my local `claudlobby` checkout's `main` ref was "
         "stale at `242dfcb`; had to `git fetch`)"),
        ("#1311 process IDs",
         "timestamps in the doc: otis's own `claude` process (PID 1028295) "
         "started `Wed Aug 19 23:08:07 2026`; its MCP child (PID 1029579) "
         "started `23:08:11`"),
    ]

    #: The opposite case, also real (#1306): multi-hex AND correctly anchored. A
    #: matcher that refused everything multi-hex would be safe and useless; this
    #: pins that the anchor is still found when a verdict names one.
    MULTI_HEX_ANCHORED = (
        "Verified at 8c3c0aa (tip; branch also carries 5719b6c, the main sweep)",
        "8c3c0aa",
    )

    def test_a_decoy_hex_in_prose_is_not_an_anchor(self):
        """LOAD-BEARING, not a defensive edge case — the house style manufactures it.

        This started as a careful guard found on one PR. Measured across this
        repo's own recent corpus (10 PRs, 14 verdict-shaped comments): **a
        hex-first matcher would anchor 10 of them, and 6 of those to the WRONG
        hex.** Not an edge case — the majority of what it would anchor.

        The decoys are not sloppiness and will not go away on their own — a
        rigorous verdict CITES things, and every citation is a hex: the base it was
        reviewed against (`against \`main\` @ \`560c3c9\``), the commit a fix
        landed on, and in one real case a **SHA-256 checksum** that is not a commit
        at all. A hex-first matcher takes the first of those and reports the verdict
        stale against something nobody reviewed: confident, wrong, and in the
        direction nobody re-checks.

        Note the `#1160` fixture contains the word "against" — the near-miss is not
        hypothetical, it is one word away from the real anchor phrasing.

        **A future reader who assumes multi-hex is rare will be tempted to simplify
        this matcher back to bare hex.** It is not rare, it is the convention, and
        the convention is the thing generating the decoys.

        The measured behaviour: verb-anchoring never takes the wrong hex. It either
        finds the anchored SHA or returns None, and None is UNKNOWABLE rather than
        clean — the safe direction.
        """
        # Claudlobby#1311's real body: a genuine anchor plus a decoy in prose.
        assert prs.parse_anchor(REAL_DECOY_LINE) is None
        assert prs.parse_anchor(REAL_APPROVE_BODY) == "b27ffc2"

        # A hex-first matcher takes the FIRST hex from each of these. Verb-anchoring
        # returns None — it refuses rather than guessing.
        import re as _re

        for label, body in self.MULTI_HEX:
            hexes = _re.findall(r"\b[0-9a-f]{7,40}\b", body)
            assert len(hexes) >= 2, f"{label}: fixture should be multi-hex"
            assert prs.parse_anchor(body) is None, (
                f"{label}: verb-anchoring must refuse, never take {hexes[0]}"
            )

    def test_multi_hex_notes_do_not_parse_as_verdicts_either(self):
        """The two bounds hold TOGETHER, which is what makes the refusal safe.

        A merge note containing the words "Request Changes" in prose would, if the
        header bound leaked, parse as a live blocking verdict AND carry several
        candidate hexes — a fabricated block anchored to an arbitrary commit.
        Measured: neither bound leaks, with the hex bolded into the lead span for
        good measure.
        """
        for label, body in self.MULTI_HEX:
            assert prs.parse_verdict(body) is None, label
        assert prs.parse_verdict(
            "**Merging at bcaf7a7**\n\nrajan Request Changes is superseded."
        ) is None

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


class TestPayloadContract:
    """The fixture must not be kinder than what a user actually produces.

    #1322 review, clog: `--payload-json` crashed with an uncaught `TypeError` on a
    payload built the obvious way — `gh pr view N --json reviews,comments,headRefOid`,
    which names every field the tool visibly reads and omits `number`, touched only
    by the renderer.

    33 green tests could not catch it, and the reason is the interesting part:
    `_payload()` supplied `number` BY DEFAULT, so every test fed a payload strictly
    MORE COMPLETE than a user's. A fixture kinder than production hides exactly the
    bugs a user hits first, and nothing in a green run says so. Patching the one
    field would have left the shape, so the fixture's key set is now pinned to the
    tool's own documented field list.
    """

    def test_fixture_is_not_kinder_than_the_documented_command(self):
        """The pin. If PR_FIELDS grows a field, this fails until the fixture has it."""
        assert set(_payload([]).keys()) == set(prs.PR_FIELD_LIST)

    def test_the_documented_command_names_every_required_field(self):
        """The help text a user follows must produce a payload that validates."""
        for field in prs.PR_FIELD_LIST:
            assert field in prs.PAYLOAD_COMMAND
        assert prs.missing_payload_fields(_payload([])) == []

    @pytest.mark.parametrize("dropped", list(prs.PR_FIELD_LIST))
    def test_every_single_missing_field_refuses_rather_than_crashes(
        self, dropped, tmp_path, capsys
    ):
        """Each field individually, not just `number`.

        `number` is the one that bit; testing only `number` would re-create the
        original defect one field over. `head` was already guarded (`r["head"] or ""`)
        — the defensive instinct was there and stopped one field short, which is
        precisely why the module read as finished.
        """
        payload = _payload([("reviews", "t1", REAL_APPROVE_HEADER)])
        payload.pop(dropped)
        path = tmp_path / "p.json"
        path.write_text(json.dumps(payload))
        rc = prs.main(["o/r", "--payload-json", str(path)])
        err = capsys.readouterr().err
        assert rc == prs.RC_USAGE, f"missing {dropped} must refuse, not crash"
        assert dropped in err and prs.PAYLOAD_COMMAND in err

    def test_the_exact_obvious_short_command_is_refused_by_name(self, tmp_path, capsys):
        """The literal shape from the review: reviews,comments,headRefOid."""
        obvious = {"reviews": [], "comments": [], "headRefOid": REAL_HEAD}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(obvious))
        rc = prs.main(["o/r", "--payload-json", str(path)])
        err = capsys.readouterr().err
        assert rc == prs.RC_USAGE
        assert "number" in err and "title" in err

    def test_a_non_object_payload_is_refused(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text(json.dumps(["not", "an", "object"]))
        assert prs.main(["o/r", "--payload-json", str(path)]) == prs.RC_USAGE


class TestAnchorVerbBoundary:
    """MINOR 1 from the #1322 review — an inert mutant hiding a real bug.

    Dropping `(?:re-?)?` passed all 33 tests, because "reviewed at" already matches
    inside "Re-reviewed at". clog verified that was an INERT MUTANT rather than a
    weak test — and then found what the missing LEADING boundary allowed.
    """

    @pytest.mark.parametrize(
        "text", ["unreviewed at 3a4f5b6", "Unreviewed at 3a4f5b6",
                 "prereviewed against 3a4f5b6", "notreviewed at 3a4f5b6"]
    )
    def test_the_verb_does_not_fire_mid_word(self, text):
        """A NEGATION READ AS AN AFFIRMATION.

        Without `\\b`, "unreviewed at 3a4f5b6" yielded an anchor — a verdict saying
        explicitly that it had NOT reviewed a commit would be scored as having
        reviewed it, and then compared against head as though it were evidence.
        """
        assert prs.parse_anchor(text) is None

    @pytest.mark.parametrize(
        "text,want",
        [("reviewed at 3a4f5b6", "3a4f5b6"), ("Re-reviewed at 3a4f5b6", "3a4f5b6"),
         ("Reviewed against 3a4f5b6", "3a4f5b6"), ("re-reviewed against 3a4f5b6", "3a4f5b6")],
    )
    def test_the_real_verbs_still_match(self, text, want):
        """The boundary must not cost the signal it guards."""
        assert prs.parse_anchor(text) == want

    def test_the_re_prefix_is_inert_on_observed_phrasings(self):
        """Pin the inertness directly — the `who-reviewed.py` precedent.

        Dropping `(?:re-?)?` comes back GREEN, and that is an INERT MUTANT rather
        than a weak test: the hyphen in "Re-reviewed" is itself a word boundary, so
        `\\breviewed` already matches inside it. The prefix differs on exactly one
        shape, unhyphenated "Rereviewed", which no observed verdict produces.

        Asserting it here means the next reader learns it from a check instead of
        re-running the mutation, seeing green, and concluding the tests are weak.
        The thing that actually fixed the live bug was the LEADING `\\b`, which
        `test_the_verb_does_not_fire_mid_word` covers and which does fail when
        removed.
        """
        import re

        with_prefix = re.compile(
            r"\b(?:re-?)?reviewed\s+(?:against|at)[^0-9a-f]{0,4}([0-9a-f]{7,40})\b", re.I)
        without = re.compile(
            r"\breviewed\s+(?:against|at)[^0-9a-f]{0,4}([0-9a-f]{7,40})\b", re.I)
        for text in ("Re-reviewed at abc1234", "reviewed at abc1234",
                     "Reviewed against abc1234", "unreviewed at abc1234"):
            assert bool(with_prefix.search(text)) == bool(without.search(text)), text
        # The single divergence, stated so it is not rediscovered as a surprise.
        assert with_prefix.search("Rereviewed at abc1234")
        assert not without.search("Rereviewed at abc1234")


class TestAnchorStemWidth:
    """#1322 review round 1, virgil (use-case lens) — a matcher gap wearing a bound.

    Bound (b) says staleness is detectable only where the reviewer WROTE the SHA.
    Verdicts that wrote it were still missed, so the bound did not cover them: the
    stems were too narrow, not the convention too loose. **A matcher gap wearing a
    bound is the worst place for one**, because the disclosure makes the miss look
    accounted for.

    Measured on THIS repo's own recent corpus (10 PRs, 14 verdict-shaped comments):
    the original `reviewed at|against` stem found **2 (14%)**; widened it finds
    **4 (29%)**. Both recovered anchors are our own house phrasing — `Verified at
    <sha>` — which the narrow stem could not see.

    The inversion is the part worth keeping: the misses were PHRASING, not
    sloppiness. A disciplined fleet phrases things consistently, so a narrow
    matcher misses all of them at once rather than a scattered few — and a
    systematic miss hides better than a random one, because the output stays
    plausible.
    """

    #: Verbatim from this repo. #1306 and #1314 are the two anchors the widening
    #: recovered on our own corpus.
    REAL_RECOVERED = [
        ("#1306", "every claim rather than trusting the report. Verified at 8c3c0aa (tip;", "8c3c0aa"),
        ("#1314", "**Approve**\n\nVerified at 7605097, again", "7605097"),
    ]

    #: Additional examination phrasings the widened stems cover. Observed on
    #: another fleet during review of this PR; that fleet's repos are outside this
    #: fleet's declared scope, so they are carried here as PHRASING FORMS and the
    #: source is deliberately not cited. The forms are what the matcher must
    #: handle; which repo they were seen in is not evidence this PR needs.
    OTHER_EXAMINATION_FORMS = [
        ("Verification review at `bc47cbcb`", "bc47cbcb"),
        ("Re-verification at **`bcaf7a71`**", "bcaf7a71"),
        ("Re-verified at **`adadf05`**", "adadf05"),
    ]

    @pytest.mark.parametrize("label,body,want", REAL_RECOVERED)
    def test_our_own_house_phrasing_that_the_narrow_stem_missed(self, label, body, want):
        assert prs.parse_anchor(body) == want, label

    @pytest.mark.parametrize("body,want", OTHER_EXAMINATION_FORMS)
    def test_other_observed_examination_phrasings(self, body, want):
        assert prs.parse_anchor(body) == want

    @pytest.mark.parametrize(
        "body",
        [
            "**Merging at `7721ab0`**",
            "Fix pushed — `92a92e6` -> `d80f927`. Fast-forward only.",
            "Rebased onto post-#972 main — `a7fe504`.",
            "Fixed at abc1234",
        ],
    )
    def test_producer_actions_are_deliberately_excluded(self, body):
        """A SEMANTIC line, not a coverage one — and the reason the rate is not higher.

        `Merging at`, `Fixed at` and `Rebased onto` name a commit somebody
        PRODUCED, not one a reviewer READ. Anchoring a verdict to one is the decoy
        failure with extra steps.

        Measured on this repo's corpus, admitting them would recover **zero**
        additional anchors — so the exclusion costs nothing here and is kept on
        the SEMANTIC line rather than a coverage one. That matters: a rule that
        happens to be free today would otherwise be the first thing dropped when
        someone wants the hit-rate up.
        """
        assert prs.parse_anchor(body) is None

    @pytest.mark.parametrize(
        "body", ["unreviewed at abc1234", "unverified at abc1234",
                 "prereviewed against abc1234"]
    )
    def test_widening_did_not_reopen_the_mid_word_hole(self, body):
        """Widening the stems must not cost the boundary that MINOR 1 added."""
        assert prs.parse_anchor(body) is None

    def test_widening_did_not_reopen_the_decoy_hole(self):
        """The load-bearing guarantee: more stems must not mean hex-first behaviour."""
        assert prs.parse_anchor(REAL_DECOY_LINE) is None
        assert prs.parse_anchor(REAL_APPROVE_BODY) == "b27ffc2"
        for label, body in TestShaAnchorRegex.MULTI_HEX:
            assert prs.parse_anchor(body) is None, label


def test_a_multi_hex_verdict_that_names_its_anchor_is_still_anchored():
    """The refusal must be targeted, not blanket.

    Real Claudlobby#1306 text: `Verified at 8c3c0aa (tip; branch also carries
    5719b6c ...)`. Two hexes, and the FIRST one is the genuine anchor. A matcher
    that refused anything multi-hex would be safe and useless — it would report
    NO-SHA-ANCHOR on a verdict that anchored perfectly, which is the exact
    under-claiming defect the `at` alternative was added to fix.
    """
    body, want = TestShaAnchorRegex.MULTI_HEX_ANCHORED
    assert prs.parse_anchor(body) == want


class TestAttributionReadsThePlane:
    """`--attribute` joins who-reviewed's PLANE rows (F18 closure R2b-1): the
    first plane-only who-reviewed left this caller reaching for its deleted
    ledger loaders, so attribution failed soft on every PR (the spec lens)."""

    def test_a_plane_backed_attribution_resolves(self):
        class _Stub:
            @staticmethod
            def load_plane_rows(root):
                return [{"ts": "2026-06-01T10:00:00Z", "bot": "vera", "status": "completed",
                         "task_id": "t-1", "pr_url": "https://github.com/o/r/pull/1", "summary": "",
                         "_fleet": "f"}], None

            @staticmethod
            def fetch_events(repo, number):
                return [{"ts": "2026-06-01T10:00:12Z"}]

            @staticmethod
            def attribute(events, rows, repo, number):
                return [{"ts": e["ts"], "bot": "vera"} for e in events]

        mapping, err = prs.ledger_identity_for("o/r", 1, "/nonexistent", module=_Stub())
        assert err is None and mapping == {"2026-06-01T10:00:12Z": "vera"}

    def test_an_unreachable_plane_is_soft_but_never_silent(self, tmp_path):
        """The real module against a root with no plane: a reason, not {} read
        as 'no attribution available' — `source_state`'s rule."""
        mapping, err = prs.ledger_identity_for("o/r", 1, str(tmp_path / "nowhere"))
        assert mapping == {} and err and "unreachable" in err
