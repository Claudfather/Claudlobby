"""What outlived the resolver.

The matcher's resolver mode and the plane readers behind it were deleted with
#2152: their only caller was `report-back.sh`, which #1989 removed, and report
verbs now name their assignment instead of having one chosen for them. Two
tests here pin code that is still live: the id-less content key, and the one
plane open per matcher call.
"""

from __future__ import annotations

from tests.plane_fixtures import F, NOW_EPOCH, REPO, _matcher, _scene


def test_sha256_hex32_is_the_content_key_and_fails_loudly_without_a_tool(tmp_path):
    """The bash key an id-less dispatch's `dispatch-log:sha:<key>` ref derives
    from must be the first 32 hex of the row's sha256 (the importer that once
    shared this definition is gone, R3 — the bash side is the one definition
    now), and with no sha tool on PATH it must FAIL (empty + nonzero) rather
    than mint junk — the door then discloses and emits the communication only."""
    import hashlib
    import subprocess
    lib = REPO / "claudlobby/_runtime_scripts" / "lib-common.sh"
    line = '{"ts":"2026-09-02T10:00:00Z","task":"do \\"the\\" thing\\\\n","x":1}'
    ok = subprocess.run(["bash", "-c", f'source "{lib}"; sha256_hex32 "$1"', "_", line],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0 and ok.stdout.strip() == hashlib.sha256(line.encode()).hexdigest()[:32]
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("bash", "cut", "printf"):
        src = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if src and src.startswith("/"):
            (bare / tool).symlink_to(src)
    no_tool = subprocess.run(["bash", "-c", f'source "{lib}"; sha256_hex32 "$1"', "_", line],
                             capture_output=True, text=True, timeout=30, env={"PATH": str(bare)})
    assert no_tool.returncode != 0 and no_tool.stdout.strip() == ""


def test_a_plane_mode_call_opens_the_plane_once(tmp_path):
    """The fold's efficiency claim, pinned at the CLI surface: one read-only
    open per invocation (the roster scan and the read share the session)."""
    root, paths, _, _ = _scene(tmp_path)
    tracer = tmp_path / "sitecustomize.py"
    tracer.write_text(
        "import sqlite3, os\n_real = sqlite3.connect\n"
        "def _c(*a, **k):\n    open(os.environ['CONNECT_LOG'], 'a').write('open\\n')\n    return _real(*a, **k)\n"
        "sqlite3.connect = _c\n")
    for args in (("--open", "w1"), ("--all", str(NOW_EPOCH)), ("--unassigned", str(NOW_EPOCH))):
        log = tmp_path / "connects.log"
        log.write_text("")
        r = _matcher(root, *args, "--fleet", F, PYTHONPATH=str(tmp_path), CONNECT_LOG=str(log))
        assert r.returncode == 0, (args, r.stderr)
        assert log.read_text().count("open") == 1, (args, log.read_text())
