"""sanitize_tmux_input (lib-common) — last line of defense before send-keys (#545).

CSI sequences must be stripped whole BEFORE control bytes are handled: the
old order deleted the ESC byte first, so the ANSI-strip sed could never match
and the printable remainder ("[31m") leaked into the sent text. Control bytes
convert to spaces rather than deleting, so a multi-line message keeps its
word boundaries ("do x\nthen" must not become "do xthen").
"""

from __future__ import annotations

from tests.conftest import call_lib_fn


def _sanitized(value: str) -> str:
    return call_lib_fn("sanitize_tmux_input", value)


def test_csi_sequences_stripped_whole_no_residue():
    assert _sanitized("a\x1b[31mRED\x1b[0m b") == "aRED b"


def test_newlines_become_spaces_not_word_merges():
    assert _sanitized("do x\nthen y") == "do x then y"


def test_control_runs_squeeze_to_single_space():
    assert _sanitized("x\r\n\ty") == "x y"


def test_bare_esc_and_del_become_space():
    # Non-CSI controls have no printable remainder to preserve — space, then
    # squeeze against neighbors.
    assert _sanitized("a\x1bb\x7fc") == "a b c"


def test_plain_text_passes_through():
    assert _sanitized("run /claudna:audit security on repo-x") == (
        "run /claudna:audit security on repo-x"
    )
