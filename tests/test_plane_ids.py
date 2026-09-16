from __future__ import annotations

import re
import stat
from pathlib import Path

from claudlobby.plane.ids import (
    ID_PATTERNS,
    ensure_host_uid,
    mint_event_id,
    mint_uid,
)


def test_event_id_shape():
    eid = mint_event_id()
    assert re.fullmatch(r"ev_[0-9a-f]{32}", eid)
    assert re.fullmatch(ID_PATTERNS["event"], eid)


def test_uid_kinds():
    for kind, prefix in [("host", "host_"), ("fleet", "fleet_"),
                         ("actor", "actor_"), ("bot_instance", "boti_"),
                         ("session", "sess_"), ("vault", "vault_"), ("project", "proj_"), ("library_item", "lib_")]:
        uid = mint_uid(kind)
        assert uid.startswith(prefix) and len(uid) == len(prefix) + 32


def test_mint_is_unique():
    assert len({mint_event_id() for _ in range(1000)}) == 1000


def test_session_uid_is_derived_and_stable():
    from claudlobby.plane.ids import derive_session_uid

    import pytest
    with pytest.raises(ValueError):
        derive_session_uid("")
    a = derive_session_uid("8ad2aa7e-bade-4c55-b3c3-000000000000")
    b = derive_session_uid("8ad2aa7e-bade-4c55-b3c3-000000000000")
    c = derive_session_uid("different-session")
    assert a == b and a != c
    assert re.fullmatch(r"sess_[0-9a-f]{32}", a)


def test_ensure_host_uid_mints_once(tmp_path: Path):
    first = ensure_host_uid(tmp_path)
    second = ensure_host_uid(tmp_path)
    assert first == second
    assert first.startswith("host_")
    f = tmp_path / "host-uid"
    assert f.read_text().strip() == first
    # 0600: the uid is joined against every ledger row; owner-only like .env
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_ensure_host_uid_survives_trailing_newline(tmp_path: Path):
    (tmp_path / "host-uid").write_text("host_" + "a" * 32 + "\n")
    assert ensure_host_uid(tmp_path) == "host_" + "a" * 32


def test_fast_path_fsyncs_directory_before_return(tmp_path: Path, monkeypatch):
    """Round-3 F2: the read path must make the dirent durable itself — the
    winner's own fsync may not have happened yet when a loser returns."""
    import os as _os

    (tmp_path / "host-uid").write_text("host_" + "a" * 32 + "\n")
    synced_dirs = []
    real_fsync = _os.fsync

    def spy(fd):
        try:
            import stat as _stat
            if _stat.S_ISDIR(_os.fstat(fd).st_mode):
                synced_dirs.append(fd)
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", spy)
    assert ensure_host_uid(tmp_path) == "host_" + "a" * 32
    assert synced_dirs, "fast path returned without fsyncing the directory"


def test_publish_is_create_if_absent(tmp_path: Path):
    """Round-3 F2: a pre-existing final file always wins; minting never
    overwrites it (the link-publish loser path). Round-6 note: the fast path
    also REPAIRS a lax pre-existing mode to 0600."""
    import os as _os, stat as _stat

    f = tmp_path / "host-uid"
    f.write_text("host_" + "b" * 32 + "\n")     # write_text => typically 0644
    _os.chmod(f, 0o644)                          # deterministic, not umask-luck
    assert ensure_host_uid(tmp_path) == "host_" + "b" * 32
    assert _stat.S_IMODE(_os.stat(f).st_mode) == 0o600


def test_crash_litter_does_not_break_minting(tmp_path: Path):
    """A crash between tmp-write and publish leaves only tmp litter — the
    next call mints normally and the final file appears complete."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / ".host-uid.999.deadbeef.tmp").write_text("host_" + "c" * 32 + "\n")
    value = ensure_host_uid(tmp_path)
    assert value.startswith("host_")
    assert (tmp_path / "host-uid").read_text().strip() == value


def test_ensure_host_uid_rejects_garbage(tmp_path: Path):
    (tmp_path / "host-uid").write_text("not-a-uid\n")
    import pytest
    with pytest.raises(ValueError):
        ensure_host_uid(tmp_path)
