"""The overdue reader reads AT an instant as precise as the rows it compares (#1789).

`dispatch-overdue.py` takes "now" as a whole-second epoch: the truncated
default, or a caller's `$(date +%s)`. `plane-readers.py` rendered it as
`…:56+00:00`, with no fraction, while a stored `occurred_at` carries microseconds
(`…:56.171359+00:00`). The two are compared as strings, and `.` sorts after `+`,
so every terminal event inside the reader's own second read as AFTER "now". The
row stayed open, and the #835 harness check flaked whenever a report and the read
shared a second.

A whole-second "now" means the END of that second: every event stored in it was
written before the read finished. The next second stays out.
"""

from __future__ import annotations

import pytest

from tests.plane_fixtures import (
    F,
    NOW_EPOCH,
    _dispatch,
    _matcher,
    _report,
    _stdlib_readers,
    plane_root,
)

# NOW_EPOCH is 2026-09-02T20:00:00Z. The dispatch is due well before it.
DISPATCHED = "2026-09-02T10:00:00Z"


@pytest.mark.parametrize(
    "report_at,closed",
    [
        pytest.param("2026-09-02T20:00:00.171359+00:00", True, id="same-second"),
        pytest.param(
            "2026-09-02T20:00:00.000001+00:00", True, id="same-second-first-microsecond"
        ),
        pytest.param("2026-09-02T19:59:59.999999+00:00", True, id="second-before"),
        pytest.param("2026-09-02T20:00:01.000001+00:00", False, id="next-second"),
    ],
)
def test_a_report_inside_the_readers_own_second_closes_its_row(
    tmp_path, report_at, closed
):
    root = plane_root(tmp_path)
    wi, asg = _dispatch(root, "1", "t-1-aaaa", DISPATCHED)
    _report(root, wi, asg, report_at)
    r = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    assert r.returncode == 0, r.stderr
    listed = [l.split()[0] for l in r.stdout.splitlines()]
    # A closed row is gone from the overdue set; a report in the next second is
    # not yet seen, so the row is still overdue as of NOW_EPOCH.
    assert (listed == []) is closed, (report_at, r.stdout)


def test_a_whole_second_reads_as_the_end_of_that_second():
    pr = _stdlib_readers()
    assert pr.read_instant(NOW_EPOCH) == "2026-09-02T20:00:00.999999+00:00"
    # A precise instant keeps its own fraction.
    assert pr.read_instant(NOW_EPOCH + 0.25) == "2026-09-02T20:00:00.250000+00:00"
