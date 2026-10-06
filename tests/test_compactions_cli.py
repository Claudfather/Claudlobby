"""#2206 through the public CLI on a selected root: `fleet compactions
record` parses, takes the selected fleet from the activation like every
scope door, and records a compaction once on that root's Plane. The row is
read back through the legacy renderer `event list` shares (this fixture seals
no event matcher)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.__main__ import main
from claudlobby.isolation import transcript_slug
from tests.conftest import read_fleet_events, write_jsonl
from tests.test_activation import cold, tmp_path  # noqa: F401 — selected activation and short paths
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency
from tests.test_task_write_cli import active  # noqa: F401 — real selected activation fixture


def _call(capsys, *argv):
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_the_public_command_records_once_and_event_list_serves_it(active, capsys):  # noqa: F811
    root, _release = active
    transcripts = Path.home() / ".claude/projects" / transcript_slug(root / "runtime/bots/worker")
    transcripts.mkdir(parents=True)
    # An hour ago, whenever the suite runs: the row's time rides the event.
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    write_jsonl(transcripts / "s1.jsonl", [{
        "parentUuid": None, "logicalParentUuid": "p0", "isSidechain": False, "type": "system",
        "subtype": "compact_boundary", "content": "Conversation compacted", "level": "info",
        "compactMetadata": {"trigger": "manual", "preTokens": 503386, "postTokens": 17666},
        "uuid": "u1", "timestamp": at, "sessionId": "s1"}])

    code, first = _call(capsys, "--root", str(root), "--json", "fleet", "compactions", "record")
    assert code == 0, first
    assert first["command"] == "fleet.compactions.record" and first["data"]["recorded"] == 1
    rows = {row["bot"]: row for row in first["data"]["bots"]}
    assert rows["worker"]["recorded"] == 1
    # The manager has no transcript: named, and nothing recorded for it.
    assert rows["manager"]["reason"] == "transcript_directory_missing_or_untrusted"

    code, again = _call(capsys, "--root", str(root), "--json", "fleet", "compactions", "record")
    assert (code, again["data"]["recorded"]) == (0, 0)

    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()
            if '"type":"compaction"' in line]
    assert [(row["bot"], row["data"]) for row in rows] == [
        ("worker", {"trigger": "manual", "pre_tokens": 503386, "post_tokens": 17666,
                    "session": "s1"})]
