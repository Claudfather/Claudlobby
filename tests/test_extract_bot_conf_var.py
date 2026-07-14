"""``extract_bot_conf_var`` must treat an absent var as empty, not an error.

Found by the #610 repo-wide scan for the failing-last-statement class: the
helper's ``grep -m1 | cut | tr`` pipeline returns grep's 1 when the var is
absent from the conf file, so under a strict caller (``set -euo pipefail``)
the ``SERVICE_PREFIX="$(extract_bot_conf_var ...)"`` assignment in
``resolve_timer_unit`` aborts the enroller — making its designed graceful
fallback ("SERVICE_PREFIX not set ..." → return 2) unreachable, exactly like
fleet-pulse's dead ``|| continue`` guard in #610. Composed bot.confs always
carry SERVICE_PREFIX (composer emits it unconditionally), so this bites only
on legacy or partially-written confs — latent, same class.

CI runs pytest only; the bash is exercised via subprocess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _extract_strict(conf: Path, var: str) -> tuple[str, int]:
    """Call extract_bot_conf_var the way production does: a command-substitution
    assignment inside a ``set -euo pipefail`` shell."""
    script = (
        'set -euo pipefail; . "$1"; '
        'v="$(extract_bot_conf_var "$2" "$3")"; printf "OK[%s]" "$v"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(LIB_COMMON), str(conf), var],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return proc.stdout, proc.returncode


def test_absent_var_yields_empty_not_abort(tmp_path):
    conf = tmp_path / "bot.conf"
    conf.write_text("export BOT_NAME=b1\n")
    out, rc = _extract_strict(conf, "SERVICE_PREFIX")
    assert rc == 0, "absent var is a normal state — strict callers must survive"
    assert out == "OK[]"


def test_present_var_extracted(tmp_path):
    conf = tmp_path / "bot.conf"
    conf.write_text("export BOT_NAME=b1\nexport SERVICE_PREFIX='claudlobby'\n")
    out, rc = _extract_strict(conf, "SERVICE_PREFIX")
    assert rc == 0
    assert out == "OK[claudlobby]"
