"""Falsify component-only composition; real composer wiring is tested by dry runs."""
import pytest

from claudlobby.ab_component_gate import SOURCE, assert_component_only, main
from claudlobby.component_sources import END_MARKER


def block(source, body):
    return f"<!-- claudlobby:source {source} -->\n{body}\n{END_MARKER}\n\n"


@pytest.fixture
def pair(tmp_path):
    source = tmp_path / "token-efficiency.md"
    source.write_text("---\ntitle: Token Efficiency\n---\n# Token Efficiency\n\nKeep every fact.\n")
    first = block("shared/library/expertise/one.md", "First instructions.")
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
    lambda text, p: text.replace("expertise/one.md", "expertise/other.md"),
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
