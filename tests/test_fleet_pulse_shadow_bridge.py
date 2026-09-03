"""The cutover shadow bridge in fleet-pulse.sh, driven for real: the
function text is extracted from the shipped script and run under
`set -euo pipefail` against the REAL debounce helper (lib-common.sh), with
the stdlib check and the pager stubbed. Every path the sweep can take."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _harness(tmp_path):
    t = tmp_path / "h"
    (t / "lib").mkdir(parents=True)
    (t / "state").mkdir()
    (t / "lib" / "lib-common.sh").write_text((REPO / "lib" / "lib-common.sh").read_text())
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    start = src.index("_shadow_page() {")
    end = src.index("# _emit_new_orphans <bot_dir> <bot_id>")
    (t / "bridge.sh").write_text(src[start:end])
    (t / "lib" / "tg-post.sh").write_text('#!/bin/bash\necho "PAGED: $1" >> "$TG_LOG"; exit "${TG_RC:-0}"\n')
    (t / "lib" / "tg-post.sh").chmod(0o755)
    (t / "lib" / "plane-shadow-check.py").write_text(
        'import os, sys\nrc = int(os.environ.get("CHECK_RC", "0"))\n'
        'if rc == 1: print("w1 overdue 2026-09-03T01:00:00+00:00")\n'
        'if rc == 3: print("plane-shadow-check: no plane db", file=sys.stderr)\nsys.exit(rc)\n')
    (t / "run.sh").write_text(
        '#!/bin/bash\nset -euo pipefail\nexport CLAUDLOBBY_ROOT="$T"\nsource "$T/lib/lib-common.sh"\n'
        'LIB_DIR="$T/lib"; state_dir="$T/state"; fleet=f; _ESCALATION_CHAT_ID="$CHAT"; _ESCALATION_STATE_DIR=""\n'
        'source "$T/bridge.sh"\n_shadow_bridge\necho after-bridge-ok\n')
    return t


def _sweep(t, *, armed="1", check_rc="0", tg_rc="0", chat="chat"):
    log = t / "tg.log"
    log.write_text("")
    r = subprocess.run(["bash", str(t / "run.sh")], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "T": str(t), "TG_LOG": str(log), "CHECK_RC": check_rc,
                            "TG_RC": tg_rc, "PLANE_SHADOW_ENABLED": armed, "CHAT": chat,
                            "HOME": str(t)})
    assert r.returncode == 0 and "after-bridge-ok" in r.stdout, (r.stdout, r.stderr)   # set -e safe, every path
    paged = log.read_text().count("PAGED")
    marker = (t / "state" / "fleet.shadow_divergence").exists()
    return paged, marker, r.stderr


def test_the_bridge_pages_once_debounces_clears_on_clean_and_survives_every_failure(tmp_path):
    t = _harness(tmp_path)
    assert _sweep(t, armed="0", check_rc="1")[:2] == (0, False)                  # dormant: silent
    paged, marker, err = _sweep(t, chat="", check_rc="1")
    assert (paged, marker) == (0, False) and "no escalation chat" in err         # armed, nowhere to page: disclosed
    assert _sweep(t, check_rc="1")[:2] == (1, True)                              # diverged: paged, marker written
    assert _sweep(t, check_rc="1")[:2] == (0, True)                              # inside the window: debounced
    assert _sweep(t, check_rc="0")[:2] == (0, False)                             # clean: marker cleared
    assert _sweep(t, check_rc="1")[:2] == (1, True)                              # a recurrence pages again
    paged, marker, err = _sweep(t, check_rc="3")
    assert paged == 0 and "shadow check unavailable (rc 3)" in err               # cannot run: disclosed, never clean
    (t / "state" / "fleet.shadow_divergence").unlink()
    paged, marker, err = _sweep(t, check_rc="1", tg_rc="7")
    assert paged == 1 and marker is False and "ALERT-DELIVERY-FAILED" in err and "exit 7" in err
    assert _sweep(t, check_rc="1")[:2] == (1, True)                              # the failed page retries next sweep
