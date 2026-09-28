"""The lib-common task ID mint remains valid for historical IDs."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
TASK_ID_RE = re.compile(r"^t-[0-9]+-[0-9a-f]{4}$")

def _sourced(fn_call: str):
    return subprocess.run(["bash", "-c", f'. "{LIB_DIR}/lib-common.sh"; {fn_call}'],
                          capture_output=True, text=True, timeout=60)

class TestMintTaskId:
    def test_shape_matches_pinned_grammar(self):
        r = _sourced("mint_task_id")
        assert r.returncode == 0, r.stderr
        assert TASK_ID_RE.match(r.stdout.strip()), r.stdout

    def test_two_mints_differ(self):
        r = _sourced("mint_task_id; mint_task_id")
        a, b = r.stdout.split()
        assert a != b, "collision-safety: same-second mints must differ"

    def test_survives_sanitize_tmux_input(self):
        r = _sourced('tid=$(mint_task_id); sanitize_tmux_input "$tid"')
        assert TASK_ID_RE.match(r.stdout.strip()), (
            "id must pass the envelope sanitizer untouched"
        )
