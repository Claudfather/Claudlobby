"""A refused report says why its fields were refused (#2933, #2935).

assignment progress, block, return, complete and fail build one typed report,
and every refusal of its fields answered the same message, `invalid linked
report fields`, with no hint, so nothing said which field was wrong or why.
fleet reports submit builds the same report with no assignment, and answered
`invalid report fields; inspect command help`. Each refusal carries the report's
own reason now. A refused report records nothing and sends nothing, as before.
The fixtures are tests/test_task_write_cli.py's.
"""

from uuid import uuid4

from claudlobby import message_operations
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_write_cli import _call, _counts, active  # noqa: F401 — the selected activation

_PR = "https://example.org/pull/1"

# (verb, the flags after its text, the report's own reason)
_REFUSED = (
    *((verb, ("--pr", _PR), "PR URL and role must occur together")
      for verb in ("progress", "block", "return", "complete", "fail")),
    ("complete", ("--pr-role", "authored"), "PR URL and role must occur together"),
    ("complete", ("--percent", "50"), "percent must be an integer 0..100 on progress only"),
    ("complete", ("--artifact", "pull/1"), "evidence must use absolute URLs without whitespace"),
)


def _no_send(*args, **kwargs):
    raise AssertionError("a refused report sends nothing")


def test_a_refused_linked_report_names_its_reason(active, monkeypatch, capsys):  # noqa: F811
    root, release = active
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root), "FLEET_NAME": "example",
                       "CLAUDLOBBY_INSTALLATION": str(release.directory)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(message_operations, "send_committed_native_attempt", _no_send)

    def generated(bot):
        monkeypatch.setenv("BOT_ID", bot)
        monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots" / bot))

    generated("manager")
    task = _call(capsys, root, "task", "admit", "--title", "Report on a PR",
                 "--request-id", str(uuid4()))["data"]["task_id"]
    assignment = _call(capsys, root, "task", "assign", task, "--bot", "worker",
                       "--request-id", str(uuid4()))["data"]["assignment_id"]
    generated("worker")
    before = _counts(root)
    for verb, flags, reason in _REFUSED:
        text = "--summary" if verb in ("progress", "complete") else "--reason"
        refused = _call(capsys, root, "assignment", verb, assignment, text, "Opened the PR", *flags,
                        "--request-id", str(uuid4()), expected=2)
        assert refused["error"]["code"] == "invalid_argument", refused
        assert refused["error"]["message"] == f"invalid linked report fields: {reason}", (verb, flags, refused)
        assert _counts(root) == before, (verb, flags)


def test_a_refused_unlinked_report_names_its_reason(active, monkeypatch, capsys):  # noqa: F811
    """Item 4 of #2935: the refusals `complete` makes above, on a completed report with no
    assignment, from a bot that may submit one."""
    root, release = active
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root), "FLEET_NAME": "example",
                       "CLAUDLOBBY_INSTALLATION": str(release.directory), "BOT_ID": "worker",
                       "BOT_DIR": str(root / "runtime/bots/worker")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(message_operations, "send_unlinked_report", _no_send)
    before = _counts(root)
    for flags, reason in ((flags, reason) for verb, flags, reason in _REFUSED if verb == "complete"):
        refused = _call(capsys, root, "fleet", "reports", "submit", "--status", "completed",
                        "--summary", "Opened the PR", *flags, "--request-id", str(uuid4()), expected=2)
        assert (refused["command"], refused["error"]["code"]) == ("fleet.reports.submit", "invalid_argument"), refused
        assert refused["error"]["message"] == f"invalid report fields: {reason}", (flags, refused)
        assert _counts(root) == before, flags
