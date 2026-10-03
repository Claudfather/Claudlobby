"""The self-restart check reads the session capture, not the refresh envelope (#2119).

Activation tops every old handoff with its reference refresh envelope (#2094),
whose time is the refresh's and never the session's. A checkpoint written later
keeps the envelope on top and refreshes the capture below it, so the check
judges that capture. It finds the envelope with the activation's own reader and
refuses, by name, one that reader refuses. The cases are vera's five probe
shapes from her review of #2110, item 3.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby import activation_handoffs, bot_operations
from claudlobby.activation_state import ActivationRefusal


def _stamp(moment: datetime) -> str:
    return f"{moment:%Y-%m-%dT%H:%M:%SZ}"


def _checkpointed(bot_dir: Path, *, refreshed: datetime, captured: datetime,
                  field: str | None = "references_refreshed") -> tuple[Path, bytes]:
    """A handoff as an activation leaves it, after a checkpoint below its envelope.

    The envelope is the activation's own rendering (``field=None`` writes none,
    and ``last_updated`` is its shape before #2094), the capture keeps its own
    frontmatter, and the canonical section closes the file."""
    handoff = bot_dir / ".claude" / "session.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    envelope = (b"" if field is None
                else activation_handoffs._refresh_envelope(bot_dir, _stamp(refreshed), field=field))
    capture = (f"---\ncwd: {bot_dir}\nlast_updated: {_stamp(captured)}\nschema_version: 2\n---\n\n"
               "## Activity\n- a checkpoint below the envelope\n").encode()
    section = (activation_handoffs._BEGIN + b'\n```json\n{"previous_handoff": '
               b'"existing file present (fresh capture unverified)"}\n```\n'
               + activation_handoffs._END + b"\n")
    handoff.write_bytes(envelope + capture + b"\n\n" + section)
    return handoff, capture


# vera's first four shapes: the envelope's field (None: no envelope) and its age in hours.
SHAPES = [
    pytest.param(None, 0, id="capture-only"),
    pytest.param("references_refreshed", 25, id="new-envelope-25h-old"),
    pytest.param("last_updated", 25, id="old-envelope-25h-old"),
    pytest.param("last_updated", 0, id="old-envelope-time-edited-to-now"),
]


@pytest.mark.parametrize("field, hours", SHAPES)
def test_a_fresh_capture_passes_under_any_recognised_envelope(tmp_path, field, hours):
    """The activation's reader strips the same envelope, so the two agree on what
    the session wrote. At main the new envelope refused (it carries no
    last_updated), and the old one refused as stale when it was 25 h old."""
    bot_dir = tmp_path / "bot"
    now = datetime.now(timezone.utc)
    _, capture = _checkpointed(bot_dir, refreshed=now - timedelta(hours=hours), captured=now, field=field)
    bot_operations._fresh_self_handoff(bot_dir)
    bot_operations._fresh_self_handoff(bot_dir, observed_capture=True)
    assert activation_handoffs._handoff_file(bot_dir, tmp_path)[1] == capture


@pytest.mark.parametrize("field, hours", SHAPES)
def test_a_stale_capture_still_refuses_whatever_the_envelope_says(tmp_path, field, hours):
    """No envelope time vouches for the capture below it: an old envelope edited
    to now, the repair before #2094, no longer passes a capture 25 h old."""
    bot_dir = tmp_path / "bot"
    now = datetime.now(timezone.utc)
    _checkpointed(bot_dir, refreshed=now - timedelta(hours=hours), captured=now - timedelta(hours=25),
                  field=field)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned") as refused:
        bot_operations._fresh_self_handoff(bot_dir)
    assert "envelope" not in str(refused.value)
    assert str(refused.value.__cause__) == "handoff is stale"


def test_an_envelope_over_a_capture_with_no_frontmatter_of_its_own_refuses(tmp_path):
    """A session that kept the old repair (editing the old envelope's time) and
    wrote no frontmatter of its own passed at main. No envelope time vouches for
    a capture, so it refuses until the session writes its own last_updated."""
    bot_dir = tmp_path / "bot"
    handoff = bot_dir / ".claude" / "session.md"
    handoff.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    handoff.write_bytes(activation_handoffs._refresh_envelope(bot_dir, _stamp(now), field="last_updated")
                        + b"Handoff notes with no frontmatter of their own\n")
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned") as refused:
        bot_operations._fresh_self_handoff(bot_dir)
    assert str(refused.value.__cause__) == "missing handoff frontmatter"


def test_a_malformed_envelope_refuses_and_the_refusal_names_it(tmp_path):
    """vera's fifth shape: last_updated added to the new envelope's own frontmatter.
    At main it passed this check, and the next activation then refused the file.
    Now both refuse it, and the refusal says which block to remove."""
    bot_dir = tmp_path / "bot"
    now = datetime.now(timezone.utc)
    handoff, _ = _checkpointed(bot_dir, refreshed=now - timedelta(hours=25), captured=now)
    edited = handoff.read_bytes().replace(
        b"\nschema_version: 2\n---\n" + activation_handoffs._REFRESH_BEGIN,
        f"\nlast_updated: {_stamp(now)}\nschema_version: 2\n---\n".encode()
        + activation_handoffs._REFRESH_BEGIN, 1)
    assert edited.count(b"\nlast_updated: ") == 2
    handoff.write_bytes(edited)
    with pytest.raises(bot_operations.BotLifecycleError,
                       match="reference refresh envelope at its top is malformed"):
        bot_operations._fresh_self_handoff(bot_dir)
    with pytest.raises(ActivationRefusal, match="existing canonical reference refresh is malformed"):
        activation_handoffs._handoff_file(bot_dir, tmp_path)


def test_a_character_cut_by_the_read_limit_does_not_refuse(tmp_path):
    """The check reads the first 8 KiB and decodes only the frontmatter it parses.
    At main it decoded the whole read, so a fresh handoff whose 8,192nd byte fell
    inside a multi-byte character refused."""
    handoff_dir = tmp_path / "bot" / ".claude"
    handoff_dir.mkdir(parents=True)
    head = f"---\nlast_updated: {_stamp(datetime.now(timezone.utc))}\n---\n".encode()
    content = head + b"x" * (8191 - len(head)) + "→ the next note\n".encode()
    assert content[8191:8194] == "→".encode()
    (handoff_dir / "session.md").write_bytes(content)
    bot_operations._fresh_self_handoff(tmp_path / "bot")
