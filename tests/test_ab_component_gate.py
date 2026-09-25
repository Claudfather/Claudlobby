"""Falsify component-only composition; real composer wiring is tested by dry runs."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from claudlobby.ab_component_gate import SOURCE, assert_component_only, main
from claudlobby.component_sources import END_MARKER


def block(source, body):
    return f"<!-- claudlobby:source {source} -->\n{body}\n{END_MARKER}\n\n"


@pytest.fixture
def pair(tmp_path):
    source = tmp_path / "token-efficiency.md"
    source.write_text("---\ntitle: Token Efficiency\n---\n# Token Efficiency\n\nKeep every fact.\n")
    first = block("shared/library/protocols/context-management.md", "First instructions.")
    last = block("shared/library/protocols/last.md", "Last instructions.")
    component = block(SOURCE, "### Token Efficiency\n\nKeep every fact.")
    return source, "Header\n\n" + first + last + "Tail\n", first, component, last


def rendered(pair):
    _, without, first, component, _ = pair
    return without.replace(first, first + component)


def test_only_the_declared_block_is_allowed(pair):
    source, without, *_ = pair
    assert_component_only(without, rendered(pair), source)
    # Common content may vary: this gate checks the axis, not a second fixture.
    assert_component_only("Common preface\n" + without, "Common preface\n" + rendered(pair), source)


@pytest.mark.parametrize("change", [
    lambda text, p: text.replace("First instructions.", "Changed instructions."),
    lambda text, p: text.replace("Header\n", "Header\r\n"),
    lambda text, p: text.replace("protocols/context-management.md", "protocols/other.md"),
    lambda text, p: text.replace(p[2] + p[3] + p[4], p[4] + p[3] + p[2]),
    lambda text, p: text.replace("Header\n", "Header\nKeep every fact.\n"),
    lambda text, p: text.replace(p[3], p[3] + p[3]),
    lambda text, p: text.replace(p[3], ""),
    lambda text, p: text.replace("Keep every fact.", "Discard a fact."),
    lambda text, p: text.replace("### Token Efficiency", "#### Token Efficiency"),
    lambda text, p: text.replace(p[3], "```\n" + p[3] + "```\n"),
    lambda text, p: text + END_MARKER + "\n",
    lambda text, p: text.replace("Keep every fact.", f"<!-- claudlobby:source {SOURCE} -->\nKeep every fact."),
    lambda text, p: text.replace(END_MARKER, "", 1),
    lambda text, p: text.replace("<!-- claudlobby:source", " <!-- claudlobby:source", 1),
    lambda text, p: text + "<!-- claudlobby:source ignored-without-final-newline -->",
    lambda text, p: text.replace(p[3], p[3] + "\n"),
    lambda text, p: text.replace(p[3], p[3].replace("\n", "\r\n")),
])
def test_other_differences_and_broken_attribution_refuse(pair, change):
    with pytest.raises(ValueError):
        assert_component_only(pair[1], change(rendered(pair), pair), pair[0])


def test_control_arm_cannot_already_contain_the_component(pair):
    with pytest.raises(ValueError, match="absent WITHOUT"):
        assert_component_only(rendered(pair), rendered(pair) + pair[3], pair[0])


@pytest.mark.parametrize("location", ["before_header", "before_context", "after_other", "after_tail"])
def test_component_placement_is_part_of_the_frozen_axis(pair, location):
    source, without, first, component, last = pair
    moved = {
        "before_header": component + without,
        "before_context": without.replace(first, component + first),
        "after_other": without.replace(last, last + component),
        "after_tail": without + component,
    }[location]
    with pytest.raises(ValueError):
        assert_component_only(without, moved, source)


@pytest.mark.parametrize("context", ["missing", "duplicate", "section_boundary"])
def test_predecessor_must_be_unique_and_immediately_adjacent(pair, context):
    source, without, first, component, last = pair
    actual = rendered(pair)
    if context == "missing":
        without, actual = without.replace(first, ""), actual.replace(first, "")
    elif context == "duplicate":
        without, actual = without + first, actual + first
    else:
        without = without.replace(first, first + "## Other section\n\n")
        actual = without.replace(last, component + last)
    with pytest.raises(ValueError):
        assert_component_only(without, actual, source)


@pytest.mark.parametrize("shadow_package", [False, True])
def test_harness_uses_its_own_source_from_unrelated_cwd(tmp_path, shadow_package):
    repo = Path(__file__).resolve().parent.parent
    home, temp, bins, cwd = [tmp_path / name for name in ("home", "tmp", "bin", "cwd")]
    for path in (home, temp, bins, cwd):
        path.mkdir()
    # A real dry compose and its gate must select the same checkout even when
    # an ambient installation lacks the helper or cwd contains another package.
    if shadow_package:
        package = cwd / "claudlobby"
        package.mkdir()
        (package / "__init__.py").write_text("raise RuntimeError('wrong checkout imported')\n")
    (bins / "python3").write_text(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} "$@"\n')
    (bins / "python3").chmod(0o755)
    # Dry cleanup still asks tmux about fixed probe names. Never let this test
    # contact native supervision, model runtimes, or outbound clients.
    for name in ("tmux", "launchctl", "systemctl", "claude", "codex", "curl", "wget", "gh", "ssh"):
        (bins / name).write_text("#!/bin/sh\nexit 97\n")
        (bins / name).chmod(0o755)
    result = subprocess.run(
        ["bash", str(repo / "lib/ab-comms-eval.sh"), "--dry-run", "--experiment",
         "channel-brevity", "--reps", "1"],
        cwd=cwd, capture_output=True, text=True, timeout=60,
        env={"PATH": str(bins) + os.pathsep + os.environ["PATH"], "HOME": str(home),
             "TMPDIR": str(temp), "XDG_CONFIG_HOME": str(home / ".config"),
             "PLANE_EMIT_DISABLED": "1", "PLANE_SOCKET": str(tmp_path / "absent.sock"),
             "TELEGRAM_STATE_DIR": str(home / "channel"), "LANG": "C.UTF-8",
             "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "variant isolation: composed delta == component block" in result.stdout


def test_source_change_or_missing_source_refuses(pair):
    actual = rendered(pair)
    pair[0].write_text("---\ntitle: Token Efficiency\n---\nChanged after composition.\n")
    with pytest.raises(ValueError, match="loaded template block"):
        assert_component_only(pair[1], actual, pair[0])
    pair[0].unlink()
    with pytest.raises(ValueError, match="source is missing"):
        assert_component_only(pair[1], actual, pair[0])


def test_cli_preserves_newline_bytes_and_propagates_refusal(pair, tmp_path, capsys):
    without, with_component = tmp_path / "without.md", tmp_path / "with.md"
    without.write_bytes(pair[1].encode())
    with_component.write_bytes(rendered(pair).encode())
    args = [str(without), str(with_component), str(pair[0])]
    assert main(args) == 0
    with_component.write_bytes(rendered(pair).replace("Header\n", "Header\r\n").encode())
    assert main(args) == 1
    assert "outside the declared component" in capsys.readouterr().err
    assert main([]) == 2
