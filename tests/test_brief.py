"""Tests for `claudlobby brief` — the one read door (#904 PR 1, epic #1102 R1).

Two properties carry most of the weight here and are worth naming, because a
test that only checked "the section rendered" would pass while either was
broken:

  1. **The dispatch sections are the shared doors' output, not a re-join.**
     Asserted by calling ``lib/dispatch-overdue.py`` directly and comparing, so
     a re-implementation that drifted from the watchdog would fail even if it
     looked right on its own.
  2. **A field this door cannot serve truthfully is never served silently.**
     Every degradation test checks the disclosure AND that the section did not
     quietly become an innocent-looking empty list.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from claudlobby.brief import (
    BOOT_CHAR_BUDGET,
    SCHEMA_VERSION,
    boot_provenance,
    build_brief,
    format_boot_brief,
    cursor_path,
    format_brief,
    load_dispatch_doors,
    read_cursor,
    write_cursor,
)
from claudlobby.config import BotConfig, FleetConfig, ProjectConfig, ScopeConfig
from claudlobby.paths import Paths

from tests.conftest import (
    dispatch_row as _dispatch,
    report_row as _report,
    write_jsonl as _write_jsonl,
)

NOW = 2_000_000
REPO_ROOT = Path(__file__).resolve().parent.parent


# --- fixtures -----------------------------------------------------------------


def _fleet(**kw) -> FleetConfig:
    bot = BotConfig(
        bot_id="alex",
        name="Alex",
        expertise=["software-engineering"],
        scope=ScopeConfig(org="acme", repos=["acme/widget"]),
    )
    base = dict(
        name="test-fleet",
        service_prefix="com.test",
        bots={"alex": bot, "ari": BotConfig(bot_id="ari", name="Ari", expertise=[])},
        mission="Ship things that earn their keep.",
    )
    base.update(kw)
    return FleetConfig(**base)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A claudlobby root with the REAL dispatch matcher in lib/.

    Copied rather than stubbed: the point of the dispatch assertions is that
    the brief and the watchdog share one implementation, which a stub would
    quietly sever.
    """
    (tmp_path / "lib").mkdir()
    shutil.copy(
        REPO_ROOT / "lib" / "dispatch-overdue.py",
        tmp_path / "lib" / "dispatch-overdue.py",
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "runtime" / "fleet").mkdir(parents=True)
    (tmp_path / "runtime" / "bots" / "alex" / "data").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=None)


def _dlog(paths: Paths) -> Path:
    return paths.root / "state" / "dispatch-log.jsonl"


def _rlog(paths: Paths) -> Path:
    return paths.fleet_state / "report-back.jsonl"


def _registry(paths: Paths) -> Path:
    return paths.fleet_state / "workstreams.json"


def _ws(**kw) -> dict:
    base = {
        "id": "ws-x",
        "title": "Ship the widget",
        "status": "active",
        "owner_bot": "alex",
        "next": "build the door",
        "opened_ts": "2026-07-01T00:00:00Z",
        "last_progress_ts": "2026-07-01T00:00:00Z",
        "lease_expires_ts": "2026-07-15T00:00:00Z",
    }
    base.update(kw)
    return base


def _write_registry(paths: Paths, entries: dict) -> None:
    _registry(paths).write_text(json.dumps({"updated": "x", "workstreams": entries}))


def _find(brief: dict, field: str, issue: str | None = None) -> list[dict]:
    return [
        d
        for d in brief["degraded"]
        if d["field"] == field and (issue is None or d["issue"] == issue)
    ]


# --- envelope -----------------------------------------------------------------


def test_brief_json_schema_v1(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    brief = build_brief(_fleet(), paths, "alex", NOW)

    assert brief["schema"] == SCHEMA_VERSION
    assert brief["bot"] == "alex"
    assert brief["fleet"] == "test-fleet"
    for key in (
        "generated_at",
        "mission",
        "dispatches",
        "workstreams",
        "reports",
        "alerts",
        "degraded",
    ):
        assert key in brief, f"envelope is missing {key}"
    assert set(brief["dispatches"]) == {"open", "overdue", "orphaned"}
    assert set(brief["reports"]) == {"cursor", "unacked"}
    # Round-trips as JSON — R4 consumes this envelope, not the text form.
    json.dumps(brief)


def test_mission_carries_pointers_not_inlined_charters(paths: Paths, tmp_path: Path):
    fleet = _fleet(
        mission_file="missions/fleet.md",
        projects={
            "widget": ProjectConfig(
                key="widget",
                title="Widget",
                repos=["acme/widget"],
                mission_file="missions/widget.md",
            ),
            "other": ProjectConfig(
                key="other",
                title="Other",
                repos=["acme/unrelated"],
                mission_file="missions/other.md",
            ),
        },
    )
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    m = build_brief(fleet, paths, "alex", NOW)["mission"]

    assert m["anchor"] == "Ship things that earn their keep."
    assert m["charter"].endswith("missions/fleet.md")
    # Joined on scope repos: the bot's project is pointed at, the other is not.
    assert [p["project"] for p in m["projects"]] == ["widget"]
    assert m["projects"][0]["mission_file"].endswith("missions/widget.md")


# --- dispatches ---------------------------------------------------------------


def test_brief_dispatch_sections_match_overdue_doors(paths: Paths):
    """The three sections must BE the shared doors' output, not a second join."""
    _write_jsonl(
        _dlog(paths),
        [
            _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-late"),
            _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-early"),
            _dispatch("ari", NOW - 9000, NOW - 3000, task_id="t-other-bot"),
        ],
    )
    _write_jsonl(_rlog(paths), [])

    doors = load_dispatch_doors(paths)
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]

    expected_overdue = doors.overdue_all(
        str(_dlog(paths)), str(_rlog(paths)), NOW, bots_dir=str(paths.runtime_bots)
    ).get("alex", [])
    assert [r["task_id"] for r in d["overdue"]] == [t[3] for t in expected_overdue]
    assert [r["task_id"] for r in d["overdue"]] == ["t-late"]

    expected_open = doors.open_dispatches("alex", str(_dlog(paths)), str(_rlog(paths)))
    assert [r["task_id"] for r in d["open"]] == [t[2] for t in expected_open]

    # Another bot's rows never leak into this bot's brief.
    assert "t-other-bot" not in json.dumps(d)


def test_open_is_deadline_blind_superset_of_overdue(paths: Paths):
    """The readable distinction the door was built for: open-but-not-yet-due."""
    _write_jsonl(
        _dlog(paths),
        [
            _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-late"),
            _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-early"),
        ],
    )
    _write_jsonl(_rlog(paths), [])
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]

    assert [r["task_id"] for r in d["open"]] == ["t-late", "t-early"]  # oldest first
    assert {r["task_id"] for r in d["overdue"]} == {"t-late"}
    assert {r["task_id"] for r in d["overdue"]} <= {r["task_id"] for r in d["open"]}

    by_id = {r["task_id"]: r for r in d["open"]}
    assert by_id["t-late"]["past_due"] is True
    assert by_id["t-early"]["past_due"] is False


def test_terminal_report_closes_an_open_dispatch(paths: Paths):
    _write_jsonl(
        _dlog(paths), [_dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-1")]
    )
    _write_jsonl(_rlog(paths), [_report("alex", "2026-08-08T00:00:00Z", task_id="t-1")])
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]
    assert d["open"] == []
    assert d["overdue"] == []


def test_missing_matcher_omits_dispatches_rather_than_reporting_zero(paths: Paths):
    """An unloadable door must not render as 'nothing open'."""
    (paths.lib / "dispatch-overdue.py").unlink()
    _write_jsonl(
        _dlog(paths), [_dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-1")]
    )
    _write_jsonl(_rlog(paths), [])

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}
    entry = _find(brief, "dispatches", "#835")
    assert entry and entry[0]["mode"] == "omitted"
    assert "unavailable" in format_brief(brief)


# --- unacked reports + the ack cursor -----------------------------------------


def test_brief_ack_cursor_roundtrip(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(
        _rlog(paths),
        [
            _report("vera", "2026-08-08T10:00:00Z", status="completed"),
            _report("mason", "2026-08-08T11:00:00Z", status="blocked"),
            _report("vera", "2026-08-08T11:30:00Z", status="progress"),
        ],
    )
    fleet = _fleet()

    brief = build_brief(fleet, paths, "alex", NOW)
    unacked = brief["reports"]["unacked"]
    # progress is not terminal — it closes nothing and acks nothing.
    assert [r["status"] for r in unacked] == ["completed", "blocked"]
    assert brief["reports"]["cursor"] is None

    write_cursor(paths, "alex", unacked[-1]["ts"])
    again = build_brief(fleet, paths, "alex", NOW)
    assert again["reports"]["unacked"] == []
    assert again["reports"]["cursor"] == "2026-08-08T11:00:00Z"
    assert read_cursor(paths, "alex") == "2026-08-08T11:00:00Z"


def test_cursor_is_per_bot(paths: Paths):
    """Two managers acking the same ledger must not clobber each other."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [_report("vera", "2026-08-08T10:00:00Z")])
    write_cursor(paths, "alex", "2026-08-08T23:00:00Z")

    assert build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"] == []
    assert len(build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"]) == 1
    assert cursor_path(paths, "alex") != cursor_path(paths, "ari")


def test_corrupt_cursor_fails_toward_showing_too_much(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [_report("vera", "2026-08-08T10:00:00Z")])
    cursor_path(paths, "alex").write_text("{not json")

    assert len(build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"]) == 1


def test_reports_are_fleet_wide_not_self_scoped(paths: Paths):
    """'What did my workers finish that I have not acted on' — not 'my own'."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(
        _rlog(paths),
        [
            _report("vera", "2026-08-08T10:00:00Z"),
            _report("mason", "2026-08-08T10:05:00Z"),
        ],
    )
    bots = {
        r["bot"]
        for r in build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"]
    }
    assert bots == {"vera", "mason"}


# --- workstreams --------------------------------------------------------------


def test_brief_stall_flags_readonly(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    # now = 2_000_000 epoch ≈ 1970-01-24; use epochs so the arithmetic is explicit.
    fresh = "1970-01-23T00:00:00Z"  # ~1 day before NOW
    old = "1970-01-01T00:00:00Z"  # ~23 days before NOW → past the 14d lease
    _write_registry(
        paths,
        {
            "ws-fresh": _ws(
                id="ws-fresh",
                last_progress_ts=fresh,
                lease_expires_ts="1999-01-01T00:00:00Z",
            ),
            "ws-stale": _ws(
                id="ws-stale",
                last_progress_ts=old,
                lease_expires_ts="1999-01-01T00:00:00Z",
            ),
            "ws-expired": _ws(
                id="ws-expired", last_progress_ts=fresh, lease_expires_ts=old
            ),
            "ws-done": _ws(id="ws-done", status="done"),
        },
    )
    before = hashlib.sha256(_registry(paths).read_bytes()).hexdigest()

    w = build_brief(_fleet(), paths, "alex", NOW)["workstreams"]

    assert {e["id"] for e in w["active"]} == {"ws-fresh", "ws-stale", "ws-expired"}
    flags = {e["id"]: (e["stalled"], e["lease_expired"]) for e in w["active"]}
    assert flags["ws-fresh"] == (False, False)
    assert flags["ws-stale"] == (True, False)
    assert flags["ws-expired"] == (False, True)
    assert {e["id"] for e in w["stalled"]} == {"ws-stale", "ws-expired"}

    # THE read-only assertion: the registry is byte-identical after a brief run.
    assert hashlib.sha256(_registry(paths).read_bytes()).hexdigest() == before


def test_corrupt_registry_is_omitted_not_reported_as_empty(paths: Paths):
    """'No workstreams' and 'the registry failed to load' are different answers."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    _registry(paths).write_text("{ this is not json")

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["workstreams"] == {}
    entry = _find(brief, "workstreams", "#911")
    assert entry and entry[0]["mode"] == "omitted"


def test_genuinely_empty_registry_is_not_degraded(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    _write_registry(paths, {})

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["workstreams"] == {"active": [], "stalled": []}
    assert _find(brief, "workstreams") == []


def test_lease_window_follows_the_writer(paths: Paths, monkeypatch):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    _write_registry(
        paths,
        {
            "ws-x": _ws(
                last_progress_ts="1970-01-18T00:00:00Z",  # ~6 days before NOW
                lease_expires_ts="1999-01-01T00:00:00Z",
            )
        },
    )
    assert (
        build_brief(_fleet(), paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
        is False
    )

    monkeypatch.setenv("WORKSTREAM_LEASE_DAYS", "3")
    assert (
        build_brief(_fleet(), paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
        is True
    )


# --- the R0 trust gate --------------------------------------------------------


def test_poisoned_report_row_is_counted_not_silently_dropped(paths: Paths):
    """#911: readers drop invalid rows silently. The door states how many."""
    _write_jsonl(_dlog(paths), [])
    _rlog(paths).write_text(
        json.dumps(_report("vera", "2026-08-08T10:00:00Z"))
        + "\n"
        + '{"ts":"2026-08-08T10:01:00Z","bot":"vera","summary":"broke "it""}\n'
    )
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "reports", "#911")
    assert entry and entry[0]["mode"] == "labeled"
    assert "1 row(s)" in entry[0]["reason"]
    # Labeled, not omitted — the rows that DID parse are still served.
    assert len(brief["reports"]["unacked"]) == 1
    assert "[degraded: #911]" in format_brief(brief)


def test_poisoned_dispatch_row_is_counted(paths: Paths):
    _dlog(paths).write_text(
        json.dumps(_dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-1"))
        + "\n"
        + "{ broken\n"
    )
    _write_jsonl(_rlog(paths), [])
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "dispatches", "#911")
    assert entry and entry[0]["mode"] == "labeled"
    assert "1 row(s)" in entry[0]["reason"]
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-1"]


def test_clean_ledgers_raise_no_911_disclosure(paths: Paths):
    """The #911 label is MEASURED, so it must vanish when the data is clean."""
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [_report("vera", "2026-08-08T10:00:00Z")])
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert [d for d in brief["degraded"] if d["issue"] == "#911"] == []


def test_alerts_are_labeled_until_the_event_type_ssot_lands(paths: Paths):
    """#903: absence of an alert is not evidence of health, and says so."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "alerts", "#903")
    assert entry and entry[0]["mode"] == "labeled"
    assert "absence of an alert is not evidence of health" in entry[0]["reason"]


def test_alert_label_clears_when_the_ssot_symbol_appears(paths: Paths, monkeypatch):
    """Keyed on #903's actual deliverable, so it retires itself."""
    from claudlobby import known_values

    monkeypatch.setattr(known_values, "FLEET_EVENT_TYPES", {"disk_high"}, raising=False)
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    assert _find(build_brief(_fleet(), paths, "alex", NOW), "alerts", "#903") == []


def test_utilization_is_recorded_as_omitted(paths: Paths):
    """The cut section is an answer, not a gap to be inferred from absence."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "utilization", "#891")
    assert entry and entry[0]["mode"] == "omitted"
    assert "utilization" not in set(brief) - {"degraded"}


def test_residence_mismatch_bound_disclosed_only_in_overlay_mode(root: Path):
    """#526: the dispatch log is host-global; report ledgers are per-fleet, so
    another fleet's bot appears in this log with its reports in a file this
    brief never opens. Labeled whenever the section is served at all."""
    fleet_dir = root / "local" / "f1"
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)
    overlay = Paths(root=root, fleet_dir=fleet_dir)
    rootmode = Paths(root=root, fleet_dir=None)
    for p in (overlay, rootmode):
        _write_jsonl(_dlog(p), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
        _write_jsonl(_rlog(p), [])

    entry = _find(build_brief(_fleet(), overlay, "alex", NOW), "dispatches", "#526")
    assert entry and entry[0]["mode"] == "labeled"
    assert (
        _find(build_brief(_fleet(), rootmode, "alex", NOW), "dispatches", "#526") == []
    )


# --- rendering ----------------------------------------------------------------


def test_format_marks_degraded_sections_inline_and_lists_them(paths: Paths):
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    text = format_brief(build_brief(_fleet(), paths, "alex", NOW))

    assert "ALERTS" in text and "[degraded: #903]" in text
    assert "DEGRADED — fields this door will not serve as plain truth" in text
    assert "degraded field(s)" in text  # the top-of-output banner
    for section in ("MISSION", "DISPATCHES", "WORKSTREAMS", "REPORTS"):
        assert section in text


def test_subfield_degradation_marks_its_parent_section_header(
    paths: Paths, monkeypatch
):
    """A degraded `dispatches.open` must not render a clean DISPATCHES header
    above an `open (0)` that is not a measurement."""
    import claudlobby.brief as brief_mod

    real = brief_mod.load_dispatch_doors

    class _Old:
        """A matcher from before the open-list door existed."""

        def __init__(self, mod):
            self._mod = mod

        def __getattr__(self, name):
            if name == "open_dispatches":
                raise AttributeError(name)
            return getattr(self._mod, name)

    monkeypatch.setattr(brief_mod, "load_dispatch_doors", lambda p: _Old(real(p)))
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [])

    brief = build_brief(_fleet(), paths, "alex", NOW)
    entry = _find(brief, "dispatches.open", "#904")
    assert entry and entry[0]["mode"] == "omitted"
    assert brief["dispatches"]["open"] == []
    # Overdue/orphaned are unaffected — only the open list degrades.
    assert "overdue" in brief["dispatches"]

    header = next(
        ln for ln in format_brief(brief).splitlines() if ln.startswith("DISPATCHES")
    )
    assert "#904" in header


def test_text_output_caps_long_sections_and_discloses_the_cap(paths: Paths):
    """Silent truncation reads as exhaustive coverage."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(
        _rlog(paths),
        [_report("vera", f"2026-08-08T10:{n:02d}:00Z") for n in range(25)],
    )
    brief = build_brief(_fleet(), paths, "alex", NOW)
    text = format_brief(brief)

    # JSON is never capped — R4 consumes that.
    assert len(brief["reports"]["unacked"]) == 25
    assert "REPORTS — unacked (25)" in text
    assert "showing the oldest 10 of 25" in text
    # The oldest is kept (it is the one rotting), the newest is dropped.
    assert "10:00:00" in text and "10:24:00" not in text


def test_cli_registers_brief_subcommand():
    """Guards the wiring itself: the door is useless if argparse cannot reach
    it, and no test that calls build_brief() directly would notice."""
    import argparse

    from claudlobby.commands._parsers import register_subparsers

    parser = argparse.ArgumentParser()
    register_subparsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["brief", "--bot", "alex", "--json"])

    assert callable(args.func)
    assert args.bot == "alex"
    assert args.json is True
    assert args.ack is False


def test_overdue_honours_the_env_expiry_cap_like_the_cli(paths: Paths, monkeypatch):
    """The matcher's Python API defaults max_age; only its main() reads the env
    var. A brief that ignored it would disagree with the very watchdog it
    mirrors, and 'byte-consistent with --all' is the contract."""
    # ~2.8h old: past its deadline, but inside the 24h default expiry cap.
    _write_jsonl(
        _dlog(paths), [_dispatch("alex", NOW - 10_000, NOW - 5_000, task_id="t-old")]
    )
    _write_jsonl(_rlog(paths), [])

    overdue = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]["overdue"]
    assert [r["task_id"] for r in overdue] == ["t-old"]

    # A fleet that tightens the cap ages the row out; the brief must follow.
    monkeypatch.setenv("DISPATCH_OVERDUE_MAX_AGE_S", "1000")
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"]["overdue"] == []
    # Still OPEN, though — expiry silences the watchdog, it does not close work.
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-old"]


# --- consuming the shared doors defensively (#526 / #1014) ---------------------


def test_missing_report_ledger_omits_dispatches_rather_than_alarming(paths: Paths):
    """The matcher fails OPEN here: with nothing to join against, every closed
    dispatch in history comes back overdue at rc 0. The precondition below
    asserts that fail-open directly, so this test still means something if the
    door is ever fixed underneath us."""
    _write_jsonl(
        _dlog(paths),
        [
            _dispatch("alex", NOW - 10_000, NOW - 5_000, task_id=f"t-{i}")
            for i in range(5)
        ],
    )
    # The ledger that would close all five is ABSENT — not empty, absent.
    assert not _rlog(paths).exists()

    doors = load_dispatch_doors(paths)
    raw = doors.overdue_all(str(_dlog(paths)), str(_rlog(paths)), NOW)
    assert len(raw.get("alex", [])) == 5, "precondition: the door fails open"

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}, "a wall of false overdue work was served"
    entry = _find(brief, "dispatches", "#526")
    assert entry and entry[0]["mode"] == "omitted"
    assert "absent" in entry[0]["reason"]

    text = format_brief(brief)
    assert "#526" in next(ln for ln in text.splitlines() if ln.startswith("DISPATCHES"))
    assert "unavailable" in text


def test_unreadable_report_ledger_omits_rather_than_raising(paths: Paths, monkeypatch):
    """The other half: an unreadable ledger raises out of the matcher entirely,
    which would take out a read-only command."""
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [])

    real_read_text = Path.read_text
    target = _rlog(paths)

    def _boom(self, *a, **kw):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    brief = build_brief(_fleet(), paths, "alex", NOW)

    assert brief["dispatches"] == {}
    assert brief["reports"] == {}
    assert _find(brief, "dispatches", "#526")[0]["mode"] == "omitted"
    assert "unreadable" in _find(brief, "dispatches", "#526")[0]["reason"]


def test_an_empty_but_present_ledger_is_answered_not_omitted(paths: Paths):
    """Existence, not emptiness, is the line: a fleet that has not reported yet
    genuinely has every dispatch open, and that answer is TRUE."""
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [])  # exists, zero rows

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-1"]
    assert brief["reports"]["unacked"] == []
    assert [d for d in _find(brief, "dispatches") if d["mode"] == "omitted"] == []


def test_missing_dispatch_log_omits_rather_than_manufacturing_an_all_clear(
    paths: Paths,
):
    _write_jsonl(_rlog(paths), [_report("vera", "2026-08-08T10:00:00Z")])
    assert not _dlog(paths).exists()

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}
    entry = _find(brief, "dispatches", "#1014")
    assert entry and entry[0]["mode"] == "omitted"
    # Reports are independent and still served.
    assert len(brief["reports"]["unacked"]) == 1


def test_missing_report_ledger_omits_reports_rather_than_zero(paths: Paths):
    """'unacked (0)' from an unreadable ledger asserts nobody is waiting on a
    decision — #949 and #1024 exactly, re-created by the fix."""
    _write_jsonl(_dlog(paths), [])
    assert not _rlog(paths).exists()

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["reports"] == {}
    entry = _find(brief, "reports", "#526")
    assert entry and entry[0]["mode"] == "omitted"

    text = format_brief(brief)
    assert "unacked (0)" not in text
    assert "REPORTS" in text and "unavailable" in text
    # The omission must not swallow later sections.
    assert "ALERTS" in text


def test_orphan_list_is_labeled_when_respawn_cannot_be_detected(paths: Paths):
    """#1014's family: no bots dir means the empty orphan list is a construction,
    not a measurement."""
    shutil.rmtree(paths.runtime_bots)
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [])

    brief = build_brief(_fleet(), paths, "alex", NOW)
    entry = _find(brief, "dispatches.orphaned", "#1014")
    assert entry and entry[0]["mode"] == "labeled"
    # Open/overdue are unaffected and still served.
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-1"]


def test_orphan_label_absent_when_the_bots_dir_exists(paths: Paths):
    _write_jsonl(_dlog(paths), [_dispatch("alex", NOW - 100, NOW + 100, task_id="t-1")])
    _write_jsonl(_rlog(paths), [])
    assert _find(build_brief(_fleet(), paths, "alex", NOW), "dispatches.orphaned") == []


def _write_fleet_yaml(fleet_dir: Path, name: str, bots: list[str]) -> None:
    """A REAL fleet.yaml — ``bots:`` nests under ``fleet:``.

    Spelled out because getting it wrong is silent: a top-level ``bots:`` key
    parses fine and yields ZERO declared bots, so `cmd_brief` returns 1 for
    "bot not found" and any test asserting only on the exit code passes for
    entirely the wrong reason.
    """
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(
        f"fleet:\n  name: {name}\n  service_prefix: com.test\n  bots:\n"
        + "".join(f"    {b}:\n      expertise: [software-engineering]\n" for b in bots)
    )


def test_ack_refuses_when_the_report_section_was_not_served(paths: Paths, caplog):
    """Advancing a cursor past reports nobody could read marks unread work as
    handled, permanently — the one irreversible thing this command can do.

    Asserts the REASON, not just the exit code: `cmd_brief` returns 1 for
    "bot not found" as well, so a bare `== 1` would pass on a fixture whose
    fleet.yaml declares no bots at all.
    """
    import argparse
    import logging

    from claudlobby.commands.core import cmd_brief

    _write_jsonl(_dlog(paths), [])
    assert not _rlog(paths).exists()  # ledger absent -> section omitted

    fleet_dir = paths.root / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    _write_fleet_yaml(fleet_dir, "f1", ["alex"])

    # The fixture is load-bearing: prove the bot really resolves, so the exit
    # code below can only come from the refusal path.
    from claudlobby.config import load_fleet

    assert "alex" in load_fleet(fleet_dir / "fleet.yaml")[0].bots

    args = argparse.Namespace(
        fleet="f1",
        root=str(paths.root),
        seed=False,
        bot="alex",
        json=False,
        ack=True,
        boot=False,
    )
    with caplog.at_level(logging.ERROR, logger="claudlobby"):
        assert cmd_brief(args) == 1
    assert "refusing to ack" in caplog.text
    assert "not found" not in caplog.text
    assert not cursor_path(Paths(root=paths.root, fleet_dir=fleet_dir), "alex").exists()


def test_ack_succeeds_when_the_ledger_is_readable(paths: Paths):
    """The positive control for the test above — same fixture, readable ledger,
    so a refusal here would mean the guard fires on the wrong condition."""
    import argparse

    from claudlobby.commands.core import cmd_brief

    _write_jsonl(_dlog(paths), [])
    fleet_dir = paths.root / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    _write_fleet_yaml(fleet_dir, "f1", ["alex"])
    (fleet_dir / "runtime" / "report-back.jsonl").write_text(
        json.dumps(_report("vera", "2026-08-08T10:00:00Z")) + "\n"
    )

    args = argparse.Namespace(
        fleet="f1", root=str(paths.root), seed=False, bot="alex", json=False, ack=True
    )
    assert cmd_brief(args) == 0
    assert cursor_path(Paths(root=paths.root, fleet_dir=fleet_dir), "alex").exists()


# --- the omit suppresses true positives too, and must say how many ------------


def test_omitted_dispatches_counts_the_rows_it_could_not_adjudicate(paths: Paths):
    """The omit is honest but lossy: with no ledger, a genuinely overdue
    dispatch and a finished one are the same bytes. What must not happen is the
    real one going unmentioned — under-reporting is the worse failure, because a
    noisy watchdog gets audited and a silent one does not."""
    _write_jsonl(
        _dlog(paths),
        [
            _dispatch("alex", NOW - 10_000, NOW - 5_000, task_id="t-past-1"),
            _dispatch("alex", NOW - 9_000, NOW - 4_000, task_id="t-past-2"),
            _dispatch("alex", NOW - 100, NOW + 5_000, task_id="t-not-yet-due"),
            _dispatch("ari", NOW - 10_000, NOW - 5_000, task_id="t-other-bot"),
        ],
    )
    assert not _rlog(paths).exists()

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}

    entry = _find(brief, "dispatches", "#526")[0]
    # This bot's past-deadline rows only: not the future one, not the peer's.
    assert entry["count"] == 2
    assert "2 dispatch row(s)" in entry["reason"]
    assert "could not be adjudicated" in entry["reason"]

    # And it reaches the section header, not just the degraded block — where a
    # bare "unavailable" would read the same as hiding nothing.
    text = format_brief(brief)
    assert "2 row(s) past deadline, status undeterminable" in text


def test_unadjudicated_count_is_none_when_even_the_denominator_is_unknown(
    paths: Paths,
):
    """The dispatch log is the unreadable side, so the count itself cannot be
    taken. Stated as None, never rendered as a reassuring 0."""
    _write_jsonl(_rlog(paths), [])
    assert not _dlog(paths).exists()

    entry = _find(build_brief(_fleet(), paths, "alex", NOW), "dispatches", "#1014")[0]
    assert entry["count"] is None
    assert "could not be adjudicated" not in entry["reason"]


def test_no_past_deadline_rows_reports_no_count_rather_than_zero_noise(paths: Paths):
    _write_jsonl(
        _dlog(paths), [_dispatch("alex", NOW - 100, NOW + 5_000, task_id="t-1")]
    )
    assert not _rlog(paths).exists()

    entry = _find(build_brief(_fleet(), paths, "alex", NOW), "dispatches", "#526")[0]
    assert entry["count"] == 0
    assert "(unavailable — see DEGRADED)" in format_brief(
        build_brief(_fleet(), paths, "alex", NOW)
    )


def test_every_degradation_carries_the_count_key(paths: Paths):
    """R4 reads this envelope; an absent key and a null one are different bugs."""
    _write_jsonl(_dlog(paths), [])
    _write_jsonl(_rlog(paths), [])
    for d in build_brief(_fleet(), paths, "alex", NOW)["degraded"]:
        assert "count" in d, d
# --- #1102 R3 / M1: the boot payload (locked fork R3-F1, O-B+r) ---------------


class TestBootProvenance:
    """boot_provenance() — the door-side facts rule 2 renders. Interim for
    #1122: computed from the same reads the door already performs; the helper
    is deleted when the envelope carries these facts."""

    def test_ledger_counts_ever_and_24h(self, paths: Paths):
        rows = [
            _dispatch("alex", NOW - 90_000, NOW - 89_000, task_id="t-old"),
            _dispatch("alex", NOW - 100, NOW + 500, task_id="t-new"),
        ]
        _write_jsonl(_dlog(paths), rows)
        prov = boot_provenance(paths, NOW)
        assert prov["dispatch_ledger"]["state"] == "ok"
        assert prov["dispatch_ledger"]["rows_ever"] == 2
        assert prov["dispatch_ledger"]["rows_24h"] == 1

    def test_ledger_absent_is_state_not_zero(self, paths: Paths):
        prov = boot_provenance(paths, NOW)
        assert prov["dispatch_ledger"]["state"] == "absent"
        assert "rows_ever" not in prov["dispatch_ledger"]

    def test_registry_absent_vs_present(self, paths: Paths):
        from claudlobby.workstreams import registry_path

        assert boot_provenance(paths, NOW)["registry"]["present"] is False
        rp = registry_path(paths)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text('{"workstreams": []}')
        prov = boot_provenance(paths, NOW)
        assert prov["registry"]["present"] is True
        assert prov["registry"]["entries"] == 0

    def test_corrupt_registry_is_unreadable_never_zero_entries(self, paths: Paths):
        # load_workstreams flattens corrupt to {} — a false-quiet. The raw
        # read here must keep corrupt distinguishable (#1122 owns the
        # envelope-level fix).
        from claudlobby.workstreams import registry_path

        rp = registry_path(paths)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text("{not json")
        prov = boot_provenance(paths, NOW)
        assert prov["registry"]["present"] is True
        assert prov["registry"]["entries"] is None


class TestBootRender:
    """format_boot_brief() — the locked O-B+r payload. The empty-state line is
    the point (fork R3-F1); mission never renders; caps are token-enforced
    with disclosed overflow."""

    def _brief(self, paths_: Paths, **ledgers):
        _write_jsonl(_dlog(paths_), ledgers.get("dispatches", []))
        if "reports" in ledgers:
            _write_jsonl(_rlog(paths_), ledgers["reports"])
        return build_brief(_fleet(), paths_, "alex", NOW)

    def test_all_quiet_renders_provenance_never_bare_zero(self, paths: Paths):
        brief = self._brief(paths, dispatches=[], reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "all quiet" in out
        assert "0 open" in out
        assert "rows ever" in out  # ledger provenance present
        assert "registry: absent" in out
        assert "claudlobby brief --bot alex" in out  # the door line
        # never a bare zero: the quiet line must carry its provenance clause
        for line in out.splitlines():
            if "0 open" in line:
                assert "ledger" in line

    def test_busy_case_prioritizes_orphaned_then_overdue_then_open(
        self, paths: Paths
    ):
        rows = [
            _dispatch("alex", NOW - 5_000, NOW + 5_000, task_id="t-open-a"),
            _dispatch("alex", NOW - 4_000, NOW + 5_000, task_id="t-open-b"),
            _dispatch("alex", NOW - 3_000, NOW - 1_000, task_id="t-late"),
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "t-late" in out
        # the door's open list is deadline-blind (a SUPERSET, #904), so the
        # overdue row counts there too; the boot payload keeps door semantics
        assert "3 open" in out and "1 overdue" in out
        assert out.count("t-late") == 1  # but each task renders once
        assert "full state: claudlobby brief --bot alex" in out

    def test_detail_cap_three_with_disclosed_overflow(self, paths: Paths):
        rows = [
            _dispatch("alex", NOW - (i * 100), NOW + 9_000, task_id=f"t-{i:02d}")
            for i in range(7)
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        detail = [ln for ln in out.splitlines() if " — sent " in ln]
        assert len(detail) == 3
        assert "+4 more" in out and "door" in out

    def test_token_cap_enforced_with_disclosure_kept(self, paths: Paths):
        rows = [
            _dispatch(
                "alex",
                NOW - (i * 10),
                NOW + 9_000,
                task_id=f"t-{'x' * 60}-{i:03d}",
            )
            for i in range(40)
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert len(out) <= BOOT_CHAR_BUDGET
        assert "more" in out and "door" in out  # overflow disclosure survived
        assert "full state: claudlobby brief" in out  # door line survived

    def test_omitted_dispatches_render_unavailable_not_zero(self, paths: Paths):
        # No report ledger -> the door omits the dispatch section (#526 defence).
        brief = self._brief(paths, dispatches=[])
        assert not brief["dispatches"]
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "UNAVAILABLE" in out
        assert "0 open" not in out
        assert "all quiet" not in out

    def test_mission_never_renders_in_boot_payload(self, paths: Paths):
        brief = self._brief(paths, dispatches=[], reports=[])
        assert brief["mission"]  # the envelope HAS it
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "MISSION" not in out and "mission" not in out

    def test_labeled_degradation_marks_the_dispatch_line(self, paths: Paths):
        _write_jsonl(_dlog(paths), [])
        (_dlog(paths)).write_text(_dlog(paths).read_text() + "not json at all\n")
        _write_jsonl(_rlog(paths), [])
        brief = build_brief(_fleet(), paths, "alex", NOW)
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "#911" in out


class TestBootCLI:
    def _args(self, root, **kw):
        import argparse

        base = dict(
            fleet="f1",
            root=str(root),
            seed=False,
            bot="alex",
            json=False,
            ack=False,
            boot=True,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _fleet_dir(self, paths_: Paths):
        fleet_dir = paths_.root / "local" / "f1"
        (fleet_dir / "runtime").mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(
            "fleet:\n  name: f1\n  service_prefix: com.test\n"
            "  bots:\n    alex:\n      expertise: [software-engineering]\n"
        )
        return fleet_dir

    def test_boot_flag_renders_quiet_payload_end_to_end(self, paths: Paths, capsys):
        from claudlobby.commands.core import cmd_brief

        _write_jsonl(_dlog(paths), [])
        fleet_dir = self._fleet_dir(paths)
        # per-fleet report ledger present-and-empty -> dispatches served empty
        _write_jsonl(fleet_dir / "runtime" / "report-back.jsonl", [])
        assert cmd_brief(self._args(paths.root)) == 0
        out = capsys.readouterr().out
        assert "all quiet" in out
        assert "full state: claudlobby brief --bot alex" in out

    def test_boot_is_mutually_exclusive_with_json_and_ack(self, paths: Paths):
        from claudlobby.commands.core import cmd_brief

        _write_jsonl(_dlog(paths), [])
        self._fleet_dir(paths)
        assert cmd_brief(self._args(paths.root, json=True)) == 1
        assert cmd_brief(self._args(paths.root, ack=True)) == 1


class TestUnlistableBotsDir:
    """brief's own contract is that it never serves a number it knows is wrong.

    An unlistable runtime/bots is the dir-source twin of the unreadable ledger
    this PR already handles: ``is_dir()`` passes, then iteration fails (#1227
    review follow-on — these two sites are inside the swept set).
    """

    def test_the_alerts_section_degrades_instead_of_raising(self, paths):
        import os as _os

        from claudlobby.brief import _alerts_section

        if _os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        bots = paths.runtime_bots
        (bots / "alex" / "data" / "events").mkdir(parents=True, exist_ok=True)
        bots.chmod(0o000)
        try:
            degraded: list = []
            out = _alerts_section(paths, "alex", 1787000000, degraded)
            assert out == []
            assert degraded, "an unreachable alert source must be disclosed"
        finally:
            bots.chmod(0o755)

    def test_orphans_are_omitted_and_disclosed_not_reported_as_none(self, paths):
        """'no orphans' and 'could not look' have opposite remedies."""
        import os as _os

        from claudlobby.brief import _dispatch_section, load_dispatch_doors

        if _os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        _dlog(paths).write_text("")
        _rlog(paths).write_text("")
        doors = load_dispatch_doors(paths)
        bots = paths.runtime_bots
        bots.chmod(0o000)
        try:
            degraded: list = []
            _dispatch_section(doors, paths, "alex", 1787000000, degraded)
            assert degraded, "an unlistable bots dir must be disclosed, not silent"
        finally:
            bots.chmod(0o755)
