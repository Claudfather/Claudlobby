"""F18 closure R2b-1 — door rules the plane-only readers made visible, each
driven through the REAL doors (the e2e harness: real shim, cold-CLI rung, a
real plane db).

1. A report that resolved no task carries its pr_url on the `report_status`
   marker, and who-reviewed's plane rows join that leg — an ad-hoc review
   (work never dispatched with an id) was unattributable by construction
   once the ledger was gone (the R2b-1 adversarial lens).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests.test_plane_door_e2e import _bash, _plane_lib, _rows

REPO = Path(__file__).resolve().parent.parent
F = "e2e-fleet"


def _who():
    spec = importlib.util.spec_from_file_location("who_reviewed_r2b", REPO / "lib" / "who-reviewed.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["who_reviewed_r2b"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_an_idless_report_keeps_its_pr_url_on_the_marker_and_who_reviewed_reads_it(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    url = "https://github.com/o/r/pull/77"
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "reviewed it" --pr {url}', env)   # no --task, nothing open
    assert r.returncode == 0, r.stderr
    marker = _rows(tmp_path, "SELECT json_extract(detail, '$.status'), json_extract(detail, '$.pr_url')"
                             " FROM events WHERE kind='system' AND event='report_status'")
    assert [tuple(m) for m in marker] == [("completed", url)], marker
    rows, why = _who().load_plane_rows(str(tmp_path))
    assert why is None
    assert [(x["bot"], x["status"], x["pr_url"], x["task_id"]) for x in rows] == [("w1", "completed", url, "")]
