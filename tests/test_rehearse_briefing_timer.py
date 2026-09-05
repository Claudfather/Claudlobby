"""The briefing-timer rehearsal's fire assertion reads the PLANE (F18 closure
R2b-2): briefing-trigger.sh rides emit_fleet_event, so the throwaway fleet's
`briefing_dispatched` / `briefing_deferred` lands on the checkout's plane,
never in a per-bot event file. The predicate is unit-tested here; the
rehearsal itself needs a user systemd."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REHEARSAL = REPO / "lib" / "rehearse-briefing-timer.sh"


def _predicate() -> str:
    text = REHEARSAL.read_text()
    start = text.index("briefing_event_landed() {")
    return text[start:text.index("\n}\n", start) + 3]


def _landed(root: Path, fleet: str, bot: str) -> bool:
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    r = subprocess.run(["bash", "-c", _predicate() + f'\nbriefing_event_landed "$1" "$2" "$3"', "_",
                        str(root), fleet, bot], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def _land(root: Path, fleet: str, bot: str, etype: str, source: str) -> None:
    from claudlobby.plane.emit_api import emit_batch

    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    out = emit_batch(root, [{"event_type": "system", "emitter": source, "fleet": fleet,
                             "occurred_at": "2026-08-06T12:40:00Z", "source_ref": f"fleet-events:sha:{etype:>032}",
                             "payload": {"event": etype, "subject_kind": "actor", "subject": f"bot:{fleet}/{bot}",
                                         "data": {"source": source, "legacy_ts": "2026-08-06T12:40:00Z",
                                                  "data": {}}}}])
    assert all(o.status == "committed" for o in out), out


def test_the_rehearsal_asks_the_plane_not_a_file():
    text = REHEARSAL.read_text()
    assert 'ls "$BOT_DIR"/data/events' not in text and "fleet-*.jsonl" not in text
    assert 'plane-lookup.py" --root "$1" --events --fleet "$2" --bot "$3"' in text


def test_a_briefing_event_on_the_plane_satisfies_the_fire_assertion(tmp_path):
    _land(tmp_path, "briefing-rehearsal", "rbot", "briefing_deferred", "briefing")
    assert _landed(tmp_path, "briefing-rehearsal", "rbot")


def test_no_briefing_event_and_no_plane_both_fail_the_assertion(tmp_path):
    assert not _landed(tmp_path, "briefing-rehearsal", "rbot")                 # no plane at all
    _land(tmp_path, "briefing-rehearsal", "rbot", "keepalive_skip", "keepalive")   # the fleet is known, no briefing
    assert not _landed(tmp_path, "briefing-rehearsal", "rbot")
