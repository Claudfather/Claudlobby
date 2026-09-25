"""A report inside the overdue reader's own second closes its row (#1789): a
whole-second "now" reads as the end of that second, and the next second stays out."""

from __future__ import annotations

import pytest

from tests.plane_fixtures import F, NOW_EPOCH, _dispatch, _matcher, _report, plane_root

# NOW_EPOCH is 2026-09-02T20:00:00Z. The dispatch is due well before it.
DISPATCHED = "2026-09-02T10:00:00Z"


@pytest.mark.parametrize(
    "report_at,closed",
    [
        pytest.param("2026-09-02T20:00:00.171359+00:00", True, id="same-second"),
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
