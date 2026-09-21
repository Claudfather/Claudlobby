"""Run every hermetic bash suite in tests/ under the pytest lane so CI enforces them.

The ``tests/*.sh`` suites are standalone bash (not pytest-collected on their own),
so without this wrapper an edit to a helper they cover breaks them with zero CI
signal — a green badge that does not actually cover the shell suites (#723; the
earlier #696 finding-3 that ``10/10 hermetic`` was never gated). This wrapper
discovers *every* ``tests/test_*.sh`` by glob and runs each via subprocess,
failing if it exits non-zero.

Glob, not a hand-maintained list: a new suite is gated the moment it lands, with
no allowlist to forget — that drift is exactly what let ``test_tg_post.sh`` ship
ungated. Discovery is guarded below so it can never silently collapse to zero.

Contract: every ``tests/*.sh`` suite must be hermetic — it stubs tmux, network
(curl/gh), sudo, and any real service onto a private PATH so it runs clean on the
Linux CI runner. A suite that reaches a real service is a bug in the suite; fix
the suite, do not drop it from the glob.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import constructed_env

TESTS_DIR = Path(__file__).resolve().parent

# Every bash suite in tests/, discovered by glob so the collected set never drifts
# from what is on disk. Sorted for a stable parametrize order and test-id sequence.
SH_SUITES = sorted(p.name for p in TESTS_DIR.glob("test_*.sh"))

# bash's exact wording when a SOURCED file fails to parse (final wave item 8).
# A syntax error inside lib/supervisor.sh (sourced by lib-common.sh, sourced
# by nearly every suite here) does abort the whole suite under its
# `set -euo pipefail` -- but MEASURED (bash 3.2.57, macOS): a suite that also
# sets `trap '...' EXIT` (mktemp-dir cleanup, the common idiom in these
# suites) has that trap's own last command's exit status become the process's
# FINAL reported exit code, overwriting the errexit-triggered abort's nonzero
# one -- so the suite can exit 0 having sourced nothing past the syntax
# error and run zero assertions. The returncode check below cannot see that
# (it only sees a nonzero exit, and here there isn't one); this catches it by
# reading for bash's own diagnostic instead.
#
# Matched at `syntax error`, NOT at the longer `syntax error near unexpected
# token`: bash 3.2.57 has TWO wordings for this, and the narrow one missed the
# likelier accident. MEASURED on the shebang target -- every UNTERMINATED
# construct (a truncated file, a half-applied edit: an open `if`, an open
# `case`, an open function body) prints `: line N: syntax error: unexpected
# end of file`, four of four shapes probed. The mutant that happened to be
# written when this guard landed was an empty `then`-body, which is one of
# the few shapes that DOES say "near unexpected token" -- so the guard passed
# its own demonstration while blind to the wider class its comment claims.
_SYNTAX_ERROR_RE = re.compile(r": line \d+: syntax error")


def test_sh_suites_discovered():
    """Discovery must never silently collapse to zero suites — an empty glob would
    reproduce the #723 gap (shell coverage present in the tree, absent from CI)."""
    assert SH_SUITES, f"no tests/test_*.sh suites discovered under {TESTS_DIR}"


@pytest.mark.parametrize("suite", SH_SUITES)
def test_hermetic_bash_suite(suite, tmp_path):
    path = TESTS_DIR / suite
    assert path.is_file(), f"missing bash suite: {suite}"
    bash = shutil.which("bash") or "/bin/bash"
    # Bounded so a wedged suite fails CI instead of hanging the runner forever.
    # Constructed env (#846): this spawn is the choke point every suite passes
    # through, and inheriting the caller's env handed bot-session vars
    # (BOT_DIR, CLAUDLOBBY_ROOT, FLEET_STATE_PATH, ...) to all of them — the
    # vector that wrote synthetic send_retry rows into six production ledgers.
    # The wrapper now enforces the env half of the hermetic contract it
    # declares; a direct `bash tests/<suite>.sh` still relies on the suite's
    # own constructed destinations.
    proc = subprocess.run(
        [bash, str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=constructed_env(HOME=tmp_path, TMPDIR=tmp_path),
    )
    assert proc.returncode == 0, (
        f"{suite} failed (exit {proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    # A syntax error in a sourced file can leave a suite at exit 0 having run
    # zero assertions (final wave item 8) -- the rc check above is blind to
    # that. Checked on both streams: bash prints the diagnostic to stderr,
    # but a suite that captures its own sourcing could relay it via stdout.
    for _stream_name, _stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        assert not _SYNTAX_ERROR_RE.search(_stream), (
            f"{suite} printed a bash syntax error on {_stream_name} despite "
            f"exit 0 -- a sourced file likely failed to parse and the suite "
            f"ran zero assertions:\n{_stream}"
        )
