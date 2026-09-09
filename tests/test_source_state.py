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
    # test_brief_reexports_these_literals_rather_than_redefining_them was deleted
    # with brief's LEDGER_* aliases (F18 R2b): brief reads no ledger, so it has
    # no source state to re-export.

    def test_the_envelope_strings_are_unchanged_by_the_extraction(self):
        """The literals are load-bearing for consumers, so pin the VALUES too —
        `is` identity would still hold if all three were renamed together."""
        assert (SOURCE_OK, SOURCE_ABSENT, SOURCE_UNREADABLE) == (
            "ok",
            "absent",
            "unreadable",
        )


class TestProbeDirDoesNotCrashOnAnUnreadableAncestor:
    """vera's round-3 blocking finding (#1227), one level below where three
    review passes were looking.

    ``probe_dir``'s ``path.is_dir()`` sat OUTSIDE its ``try``. ``Path.is_dir``
    swallows only ``pathlib._IGNORED_ERRNOS`` — measured as exactly ENOENT,
    ENOTDIR, EBADF, ELOOP, with **EACCES absent** — so the stat propagates and
    the guard written to stop readers crashing on an unreachable source crashed
    on an unreadable one.

    The trigger is an unreadable ANCESTOR, not the directory itself: a mode-000
    dir whose parent is traversable stats fine and was always classified
    correctly. That distinction is why it survived the earlier passes, and it is
    the shape the real callers hit — collect_events probes
    ``<bots>/<bot>/data/events`` while ``<bots>`` is the locked one.
    """

    def test_a_dir_inside_an_unreadable_parent_is_unreadable_not_an_exception(
        self, tmp_path
    ):
        import os

        import pytest

        from claudlobby.source_state import SOURCE_UNREADABLE, probe_dir

        if os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        parent = tmp_path / "locked"
        (parent / "child").mkdir(parents=True)
        os.chmod(parent, 0o000)
        try:
            # Premise check, version-split: <=3.12 pathlib propagated EACCES
            # from is_dir(); 3.13+ SWALLOWS every OSError (the change that
            # silently killed this pin into the red baseline and let the
            # defect resurface — found live by external review). Either way
            # the lock is really applied, and probe_dir must classify
            # UNREADABLE on every version because it reads errnos from
            # os.scandir at call time, not from pathlib's swallow list.
            try:
                stat_result = (parent / "child").is_dir()
            except OSError:
                pass                        # <=3.12: propagated — lock real
            else:
                assert stat_result is False  # 3.13+: swallowed — lock real
            assert probe_dir(parent / "child").state == SOURCE_UNREADABLE
        finally:
            os.chmod(parent, 0o755)

    def test_the_directory_itself_being_unreadable_still_classifies(self, tmp_path):
        """The case that always worked — kept as the control, so a fix that
        traded one for the other cannot pass."""
        import os

        import pytest

        from claudlobby.source_state import SOURCE_UNREADABLE, probe_dir

        if os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        d = tmp_path / "selfzero"
        d.mkdir()
        os.chmod(d, 0o000)
        try:
            assert probe_dir(d).state == SOURCE_UNREADABLE
        finally:
            os.chmod(d, 0o755)

    def test_absent_and_not_a_directory_still_classify_absent(self, tmp_path):
        """Moving is_dir() inside the try must not turn ABSENT into UNREADABLE."""
        from claudlobby.source_state import SOURCE_ABSENT, probe_dir

        missing = tmp_path / "nope"
        a_file = tmp_path / "afile"
        a_file.write_text("x")
        assert probe_dir(missing).state == SOURCE_ABSENT
        assert probe_dir(a_file).state == SOURCE_ABSENT


class TestProbeDirObservesFirstReadErrors:
    """External round 2's blocker on the scandir rewrite: opendir can
    succeed while the FIRST readdir raises (EIO/ESTALE — failing storage,
    FUSE, NFS; the estate's SD-stall class). An un-advanced iterator never
    observes it and certified unreadable-as-OK. probe_dir must advance once
    inside the exception boundary."""

    def test_error_on_first_read_is_unreadable_not_ok(self, tmp_path, monkeypatch):
        import errno

        from claudlobby import source_state

        class _OpensThenDies:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __next__(self):
                raise OSError(errno.EIO, "I/O error on first readdir")

        monkeypatch.setattr(source_state.os, "scandir",
                            lambda p: _OpensThenDies())
        probe = source_state.probe_dir(tmp_path)
        assert probe.state == source_state.SOURCE_UNREADABLE


class TestScanDirMaterializesUnderOneBoundary:
    """External round 3's blocker: probe-then-reopen let Path.glob swallow a
    LATER readdir error, returning a partial listing as a clean empty.
    scan_dir performs classification and the ENTIRE enumeration inside one
    exception boundary; callers consume its list."""

    def _one_then_eio(self, benign_path):
        import errno
        import types

        class _It:
            def __init__(self):
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if not self._sent:
                    self._sent = True
                    return types.SimpleNamespace(path=str(benign_path))
                raise OSError(errno.EIO, "later readdir failed")

        return _It()

    def test_error_after_one_benign_entry_is_unreadable_with_no_partial(
        self, tmp_path, monkeypatch
    ):
        from claudlobby import source_state

        monkeypatch.setattr(
            source_state.os, "scandir",
            lambda p: self._one_then_eio(tmp_path / "benign"))
        probe, entries = source_state.scan_dir(tmp_path)
        assert probe.state == source_state.SOURCE_UNREADABLE
        assert entries == []          # a partial listing is never returned

    def test_clean_dir_returns_the_full_listing(self, tmp_path):
        from claudlobby import source_state

        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "sub").mkdir()
        probe, entries = source_state.scan_dir(tmp_path)
        assert probe.state == source_state.SOURCE_OK
        assert sorted(e.name for e in entries) == ["a.json", "sub"]
