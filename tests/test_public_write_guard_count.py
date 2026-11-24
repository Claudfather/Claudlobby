"""``public-write-guard.py --count``: how many of a text's lines have a hit on the
host's list, and never which terms or which lines.

The terms here are INVENTED, as in tests/test_public_write_guard.py: the real list
is host configuration and never appears in a repository. Every run below also
checks that its output holds no term."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GUARD = (
    Path(__file__).resolve().parent.parent
    / "claudlobby"
    / "_runtime_scripts"
    / "public-write-guard.py"
)
TERMS = "# invented terms for the tests\nzephyr[ ._-]?widgets\nquillon\\s+media\n"
# Every match of either term holds one of these words, so an output that showed a
# term, a matched line or a line of the input would hold one of them too.
WORDS = ("zephyr", "quillon")


@pytest.fixture
def env(tmp_path):
    terms = tmp_path / "terms"
    terms.write_text(TERMS)
    return {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PUBLIC_WRITE_GUARD_TERMS": str(terms),
        "PLANE_EMIT_DISABLED": "1",
    }


def count(env, *files, stdin=""):
    p = subprocess.run(
        ["python3", str(GUARD), "--count", *map(str, files)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    out = (p.stdout + p.stderr).lower()
    assert not any(w in out for w in WORDS), "an output holds a term"
    return p.returncode, p.stdout, p.stderr


def test_a_regex_line_counts_a_text_a_literal_reader_misses(env, tmp_path):
    """The list is one regular expression per line. Read as literal strings, as
    `grep -c -i -F -f` reads it, these lines match nothing here."""
    f = tmp_path / "notes.txt"
    f.write_text("plain words\nnotes on Zephyr_Widgets\nthe Quillon   Media notes\n")
    rc, out, _ = count(env, f)
    assert (rc, out) == (1, f"{f}: 2 line(s) with a hit\n")
    literal = [t for t in TERMS.splitlines() if t and not t.startswith("#")]
    lines = f.read_text().lower().splitlines()
    assert not any(t.lower() in line for t in literal for line in lines)


def test_a_comment_line_counts_nothing(env):
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text("# quillon media\nzephyr[ ._-]?widgets\n")
    rc, out, _ = count(env, stdin="notes on quillon media\n# quillon media\n")
    assert (rc, out) == (0, "standard input: 0 line(s) with a hit\n")


@pytest.mark.parametrize(
    "text", [None, "# invented, and no term yet\n\n"], ids=["no-file", "comments-only"]
)
def test_an_absent_list_exits_2_before_reading_an_input(env, tmp_path, text):
    """An input that cannot be read would count as a hit (exit 1): the list's state
    comes first."""
    terms = Path(env["PUBLIC_WRITE_GUARD_TERMS"])
    if text is None:
        terms.unlink()
    else:
        terms.write_text(text)
    rc, out, err = count(env, tmp_path / "not-there.txt")
    assert (rc, out, err) == (2, "", f"{terms}: absent\n")


@pytest.mark.parametrize(
    "line, why",
    [
        ("(?P=quillon)", "line 2 does not compile"),
        ("zephyr|", "line 2 can match an empty string"),
    ],
    ids=["does-not-compile", "matches-empty"],
)
def test_a_broken_list_exits_2_naming_the_line_not_its_text(env, line, why):
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text(f"zephyr[ ._-]?widgets\n{line}\n")
    rc, out, err = count(env, stdin="notes on Zephyr-Widgets\n")
    assert (rc, out) == (2, "") and why in err


def test_each_input_gets_its_count_and_any_hit_exits_1(env, tmp_path):
    first, hits, last = tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.txt"
    first.write_text("plain\n")
    hits.write_text("zephyr widgets, then Zephyr.Widgets\nplain\nquillon media\nZEPHYRWIDGETS\n")
    last.write_text("plain\n")
    rc, out, _ = count(env, first, hits, last)
    assert rc == 1
    assert out.splitlines() == [
        f"{first}: 0 line(s) with a hit",
        f"{hits}: 3 line(s) with a hit",
        f"{last}: 0 line(s) with a hit",
    ]
    rc, out, _ = count(env, first, last)
    assert rc == 0 and out.count(": 0 line(s) with a hit") == 2


def test_standard_input_is_read_when_no_file_is_named(env):
    rc, out, _ = count(env, stdin="plain\nZephyr widgets\n")
    assert (rc, out) == (1, "standard input: 1 line(s) with a hit\n")


def test_a_hit_across_a_line_break_counts_each_line_it_touches(env):
    """The guard matches a write's content as a whole, so `\\s+` takes a line break
    there; a reader that went line by line would read 0 here."""
    rc, out, _ = count(env, stdin="plain\nnotes on quillon\n  media, and more\nplain\n")
    assert (rc, out) == (1, "standard input: 2 line(s) with a hit\n")


def test_an_input_that_cannot_be_read_counts_as_a_hit(env, tmp_path):
    missing = tmp_path / "not-there.txt"
    rc, out, _ = count(env, missing, tmp_path)
    assert rc == 1
    assert out.splitlines() == [
        f"{missing}: cannot be read, counted as a hit",
        f"{tmp_path}: cannot be read, counted as a hit",
    ]


def test_an_input_whose_name_has_a_hit_is_named_by_its_place(env, tmp_path):
    """A name is not content, so it is no hit, and it is not printed either."""
    plain, named = tmp_path / "plain.txt", tmp_path / "zephyr-widgets.txt"
    plain.write_text("plain\n")
    named.write_text("plain\n")
    rc, out, _ = count(env, plain, named)
    assert rc == 0
    assert out.splitlines() == [f"{plain}: 0 line(s) with a hit", "input 2: 0 line(s) with a hit"]


def test_a_file_is_read_as_the_guard_reads_a_body_file(env, tmp_path):
    """A byte that does not decode is replaced, and the rest of the file is read."""
    f = tmp_path / "notes.txt"
    f.write_bytes(b"caf\xe9 notes\nZephyr widgets\n")
    rc, out, _ = count(env, f)
    assert (rc, out) == (1, f"{f}: 1 line(s) with a hit\n")


def test_no_output_holds_a_term(env, tmp_path):
    """Every run in this file checks it; this one puts a term everywhere at once: in
    each line of the input, in the file's name, and then in a broken list."""
    named = tmp_path / "quillon media.txt"
    named.write_text("Zephyr widgets\nquillon media\n" * 50)
    rc, out, err = count(env, named)
    assert (rc, out, err) == (1, "input 1: 100 line(s) with a hit\n", "")
    Path(env["PUBLIC_WRITE_GUARD_TERMS"]).write_text("zephyr[ ._-]?widgets\nquillon(\n")
    rc, out, err = count(env, named)
    assert (rc, out) == (2, "") and "line 2 does not compile" in err


def test_the_off_switch_does_not_change_a_count(env, tmp_path):
    off = tmp_path / "root" / "state" / "public-write-guard"
    off.mkdir(parents=True)
    (off / "disabled").write_text("")
    env["CLAUDLOBBY_ROOT"] = str(tmp_path / "root")
    rc, out, _ = count(env, stdin="Zephyr widgets\n")
    assert (rc, out) == (1, "standard input: 1 line(s) with a hit\n")
