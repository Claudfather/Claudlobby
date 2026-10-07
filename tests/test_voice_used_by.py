"""A voice a bot declares reads as used by that bot in the plane inventory (#2204 registers the packaged voices as library items)."""

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.inventory import fleet_inventory
from tests.plane_setup import initialize_plane
from tests.test_plane_inventory import FLEET, _bot, _done, _lib, _root


def test_a_voice_a_bot_declares_reads_as_used_by_that_bot(tmp_path):
    root = _root(tmp_path)
    initialize_plane(root)
    declaring = _bot(f"bot:{FLEET}/erlich", skills=())
    declaring["payload"]["payload"]["equipment"]["voice"] = "voices/vito-corleone.md"
    emit_batch(
        root,
        [
            declaring,
            _bot(f"bot:{FLEET}/dinesh", skills=()),
            _lib("voices", "vito-corleone"),
            _lib("voices", "john-carmack"),
            _done(),
        ],
    )
    conn = connect(db_path(root))
    try:
        inv = fleet_inventory(conn, FLEET)
    finally:
        conn.close()
    rows = {r["name"]: r["used_by"] for r in inv["library"] if r["category"] == "voices"}
    assert rows == {"vito-corleone": ["erlich"], "john-carmack": []}
    assert inv["counts"]["library_in_use"] == 1
