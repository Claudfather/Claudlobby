"""Issue-linked quarantine policy shared by CI collection and its probes."""

import pytest


def quarantine_problem(mark) -> str | None:
    """Why a ``quarantine`` marker is refused, or None when it is fine.

    A quarantined test leaves CI's required lanes; it still runs, visibly, in
    the quarantine workflow that nothing requires. So the marker must name the
    issue that tracks bringing it back, ``quarantine(issue=<number>)``: one
    that names none has been removed from the gate with no way back."""
    issue = mark.kwargs.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        return "must name its tracking issue: @pytest.mark.quarantine(issue=<number>)"
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Refuse a quarantine that names no tracking issue, in every lane. It runs
    before -m deselects anything, so the required lane refuses it too."""
    refused = [
        f"{item.nodeid}: {problem}"
        for item in items
        for mark in item.iter_markers("quarantine")
        if (problem := quarantine_problem(mark))
    ]
    if refused:
        raise pytest.UsageError(
            "quarantined test(s) naming no tracking issue:\n  " + "\n  ".join(refused)
        )
