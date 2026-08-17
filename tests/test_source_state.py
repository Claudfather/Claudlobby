"""The shared unreachable-vs-empty rule (#1216, #1014).

These pin the CLASSIFIER. The per-command remedies are pinned next to each
command, because the rule and what a caller does about it are separate decisions
and were deliberately kept that way.
"""

from __future__ import annotations

import os

import pytest

from claudlobby.source_state import (
    SOURCE_ABSENT,
    SOURCE_OK,
    SOURCE_UNREADABLE,
    UNREACHABLE,
    probe_dir,
    probe_source,
    unreachable_line,
)


class TestProbeSource:
    def test_a_readable_file_is_ok_even_when_empty(self, tmp_path):
        """THE central case, and the one the whole issue turns on: an existing
        file with no content is REACHABLE. If this ever flips, every consumer
        starts refusing to answer for fleets that simply have not reported yet —
        the opposite failure, and just as wrong."""
        p = tmp_path / "ledger.jsonl"
        p.write_text("")
        probe = probe_source(p)
        assert probe.state == SOURCE_OK
        assert probe.reachable
        assert not probe.unreachable

    def test_a_missing_file_is_absent(self, tmp_path):
        probe = probe_source(tmp_path / "nope.jsonl")
        assert probe.state == SOURCE_ABSENT
        assert probe.unreachable

    def test_a_directory_where_a_file_belongs_is_absent_not_unreadable(self, tmp_path):
        """The remedies differ: absent sends you to create or re-resolve the
        path, unreadable sends you to fix permissions. IsADirectoryError is an
        OSError, so the naive ordering reports 'fix your permissions' for a path
        that simply is not a file."""
        d = tmp_path / "as_dir"
        d.mkdir()
        assert probe_source(d).state == SOURCE_ABSENT

    def test_a_path_under_a_non_directory_is_absent(self, tmp_path):
        """NotADirectoryError: a resolver that appends to a FILE path produces
        this, and it means the same thing to the caller as a missing file."""
        f = tmp_path / "afile"
        f.write_text("x")
        assert probe_source(f / "child.jsonl").state == SOURCE_ABSENT

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unopenable_file_is_unreadable_not_absent(self, tmp_path):
        """The mode a stat-only probe certifies as fine. It exists, so 'absent'
        would be a lie, and the file is real so the reader must not silently
        report zero rows for it."""
        p = tmp_path / "locked.jsonl"
        p.write_text('{"a":1}\n')
        p.chmod(0o000)
        try:
            assert probe_source(p).state == SOURCE_UNREADABLE
        finally:
            p.chmod(0o644)

    def test_the_probe_never_raises_on_a_hostile_path(self, tmp_path):
        """A read door that crashed would trade a false all-clear for an outage,
        which is not an improvement. Exercised with a path long enough to draw
        ENAMETOOLONG rather than a plain ENOENT."""
        probe = probe_source(tmp_path / ("x" * 4096))
        assert probe.state in UNREACHABLE

    def test_the_path_tried_is_carried_back(self, tmp_path):
        """#1216 was a PATH defect — the ledger existed all along at the fleet
        tier. A message naming only 'not found' sends the reader to create a file
        that is already there, so the probe has to report where it looked."""
        p = tmp_path / "sub" / "ledger.jsonl"
        assert probe_source(p).path == p


class TestProbeDir:
    def test_a_listable_directory_is_ok(self, tmp_path):
        assert probe_dir(tmp_path).state == SOURCE_OK

    def test_an_empty_directory_is_still_ok(self, tmp_path):
        """Same rule as the file case: empty is a state, not a failure."""
        d = tmp_path / "empty"
        d.mkdir()
        assert probe_dir(d).state == SOURCE_OK

    def test_a_missing_directory_is_absent(self, tmp_path):
        assert probe_dir(tmp_path / "nope").state == SOURCE_ABSENT

    def test_a_file_is_absent_when_a_directory_was_expected(self, tmp_path):
        f = tmp_path / "afile"
        f.write_text("x")
        assert probe_dir(f).state == SOURCE_ABSENT

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unlistable_directory_is_unreadable(self, tmp_path):
        """is_dir() returns True without the execute bit, so a predicate-only
        check certifies a directory that raises the moment it is iterated."""
        d = tmp_path / "nox"
        d.mkdir()
        (d / "f.jsonl").write_text("{}\n")
        d.chmod(0o000)
        try:
            assert probe_dir(d).state == SOURCE_UNREADABLE
        finally:
            d.chmod(0o755)


class TestUnreachableLine:
    def test_it_says_cannot_rather_than_none(self, tmp_path):
        """The wording is the fix. 'no rows' and 'cannot read' were the same
        output; a line that hedges re-creates that."""
        line = unreachable_line("the report-back ledger", probe_source(tmp_path / "x"))
        assert line.startswith("cannot read the report-back ledger")
        assert "does not exist" in line

    def test_it_names_the_path(self, tmp_path):
        p = tmp_path / "deep" / "ledger.jsonl"
        assert str(p) in unreachable_line("the ledger", probe_source(p))

    def test_the_remedy_is_appended_when_given_and_absent_when_not(self, tmp_path):
        probe = probe_source(tmp_path / "x")
        assert "try --fleet" in unreachable_line("l", probe, remedy="try --fleet")
        assert "—" not in unreachable_line("l", probe)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_the_two_unreachable_states_do_not_render_alike(self, tmp_path):
        """Absent and unreadable have different remedies, so a single phrase for
        both would re-collapse a distinction one layer down."""
        missing = tmp_path / "gone.jsonl"
        locked = tmp_path / "locked.jsonl"
        locked.write_text("{}\n")
        locked.chmod(0o000)
        try:
            a = unreachable_line("l", probe_source(missing))
            b = unreachable_line("l", probe_source(locked))
            assert a != b
            assert "does not exist" in a
            assert "could not be read" in b
        finally:
            locked.chmod(0o644)


class TestTheRuleIsSharedNotCopied:
    def test_brief_reexports_these_literals_rather_than_redefining_them(self):
        """brief.py emits these strings verbatim in its schema-1 envelope. Two
        definitions is a wire-format fork waiting to happen, so the aliases are
        asserted identical rather than merely equal-looking."""
        from claudlobby import brief

        assert brief.LEDGER_OK is SOURCE_OK
        assert brief.LEDGER_ABSENT is SOURCE_ABSENT
        assert brief.LEDGER_UNREADABLE is SOURCE_UNREADABLE

    def test_the_envelope_strings_are_unchanged_by_the_extraction(self):
        """The literals are load-bearing for consumers, so pin the VALUES too —
        `is` identity would still hold if all three were renamed together."""
        assert (SOURCE_OK, SOURCE_ABSENT, SOURCE_UNREADABLE) == (
            "ok",
            "absent",
            "unreadable",
        )
