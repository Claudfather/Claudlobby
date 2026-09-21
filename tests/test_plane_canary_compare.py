"""The #1693 canary's comparator, proven to FIRE (#1712 review).

The control run cannot establish this. Under per-batch-close every
acknowledgment is already checkpointed before the reply is sent, so `lost == 0`
is guaranteed before the harness runs: a comparator that always answers "not
lost" and one that correctly answers "not lost because nothing was lost" emit
byte-identical output there.

So the control validates NO FALSE POSITIVES. These tests are the other half —
false negatives — and the harness's own bar is what demands them: *a canary
that has not demonstrated it can detect the failure is not evidence.*

Injected at the COMPARATOR, never at the daemon: no second SIGKILL, and the
property under test is the detector rather than the system.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "plane_canary_compare", REPO / "lib" / "plane-canary-compare.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


C = _mod()
IDS = [f"ev_{i:032x}" for i in range(5)]


class TestTheDetectorFires:
    def test_an_acknowledged_id_absent_from_the_ledger_is_named(self):
        """THE test the control cannot be."""
        have = set(IDS) - {IDS[2]}
        assert C.compare(IDS, have) == [IDS[2]]

    def test_a_clean_run_names_nothing(self):
        """The other direction: it must not cry wolf when nothing is missing —
        which is the only property the control run establishes."""
        assert C.compare(IDS, set(IDS)) == []

    def test_self_test_detects_its_own_injection(self):
        fired, detail = C.self_test(IDS, set(IDS))
        assert fired, detail
        assert IDS[0] in detail

    def test_self_test_REFUSES_a_comparator_that_never_fires(self, monkeypatch):
        """The self-test must fail a broken detector, or it is decoration.

        A comparator that always answers "nothing lost" passes every assertion
        the control run makes. It must not pass this one."""
        monkeypatch.setattr(C, "compare", lambda acked, have: [])
        fired, detail = C.self_test(IDS, set(IDS))
        assert not fired
        assert "NOT detected" in detail

    def test_self_test_REFUSES_a_comparator_that_fires_on_everything(self):
        """The second assertion, and it is not redundant: a comparator
        returning its whole input NAMES the withheld id, so a self-test
        checking only 'was the victim named' would pass it."""
        m = _mod()
        m.compare = lambda acked, have: list(acked)      # fires on everything
        fired, detail = m.self_test(IDS, set(IDS))
        assert not fired
        assert "also named ids that are present" in detail

    def test_self_test_refuses_with_no_acknowledgments_to_inject_into(self):
        """An empty witness cannot demonstrate anything, and must say so rather
        than vacuously passing."""
        fired, detail = C.self_test([], set())
        assert not fired
        assert "no acknowledged ids" in detail


class TestTheWitnessIsTheClientsOwnLog:
    def test_only_acknowledged_lines_count(self, tmp_path):
        w = tmp_path / "witness.log"
        w.write_text(
            "1 ev_a ok 100\n"
            "2 ev_b refused 100\n"      # the daemon said no: never acknowledged
            "3 ev_c error 100\n"        # no reply at all
            "4 ev_d ok 100\n"
        )
        assert C.acked_ids(str(w)) == ["ev_a", "ev_d"]

    def test_a_refused_id_absent_from_the_ledger_is_NOT_loss(self, tmp_path):
        """Refusals and transport errors were never promised anything. Counting
        them would manufacture loss out of the daemon working correctly."""
        w = tmp_path / "witness.log"
        w.write_text("1 ev_a ok 1\n2 ev_b refused 1\n")
        assert C.compare(C.acked_ids(str(w)), {"ev_a"}) == []
