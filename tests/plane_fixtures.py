"""Shared plane test scaffolding — one definition of the throwaway plane root
(three test files had the same four lines) and a read-only connection that
owns its close."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from claudlobby.plane.db import connect_ro, db_file


def plane_root(tmp_path: Path, *, capture: str = '{"*": "full"}') -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text(capture)
    return root


def open_assignment_ids(root: Path) -> list[str]:
    """The plane's open assignments by production's own definition of open."""
    from claudlobby.plane.queries import NON_TERMINAL_CLAUSE
    with ro(root) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT a.assignment_id FROM assignments a WHERE" + NON_TERMINAL_CLAUSE))


@contextmanager
def ro(root: Path):
    conn = connect_ro(db_file(root))
    try:
        yield conn
    finally:
        conn.close()
