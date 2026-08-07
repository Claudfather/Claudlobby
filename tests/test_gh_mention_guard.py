"""The composed name list behind the GitHub mention guard (#1019).

Every fleet bot name is also a real GitHub account — all 21 resolve, 19 of them
to real people and 2 to organizations — so `@teammate` in a PR comment emails a
stranger. One asked us to stop. There is no safe name: an organization is
still an account we do not control. Whether a person-valued field accepts or
rejects one is untested — settling it needs a real assignment attempt.
`lib/gh-mention-guard.sh` rewrites those mentions out of GitHub-bound tool calls;
this covers the list it reads.

The list is the part that rots. A hardcoded one re-breaks the moment a bot is
added (#1009's defect class), and a fleet-scoped one misses cross-fleet
references — which are the majority, since a bot writing about another fleet's
worker has no relationship to that fleet.
"""

from __future__ import annotations

from pathlib import Path

from claudlobby.composer import compose_host_bot_handles
from claudlobby.paths import Paths


def _host(tmp_path: Path, fleets: dict[str, list[str]]) -> Path:
    for name, bots in fleets.items():
        d = tmp_path / "local" / name
        d.mkdir(parents=True)
        body = "\n".join(f"    {b}: {{expertise: [eng]}}" for b in bots)
        (d / "fleet.yaml").write_text(
            f"fleet:\n  name: {name}\n  bots:\n{body}\n", encoding="utf-8"
        )
    return tmp_path


def _handles(tmp_path: Path) -> list[str]:
    out = compose_host_bot_handles(Paths(root=tmp_path), output_dir=tmp_path / "out")
    return out.read_text().split()


def test_covers_every_fleet_on_the_host_not_just_one(tmp_path):
    """The requirement that makes this useful: cross-fleet mentions dominate."""
    _host(tmp_path, {"alpha": ["ravi", "dara"], "beta": ["kev", "saul"]})
    assert _handles(tmp_path) == ["dara", "kev", "ravi", "saul"]


def test_a_new_bot_is_covered_with_no_code_change(tmp_path):
    """A hardcoded list would re-break here — that is the whole point."""
    _host(tmp_path, {"alpha": ["ravi"]})
    assert _handles(tmp_path) == ["ravi"]
    (tmp_path / "local" / "alpha" / "fleet.yaml").write_text(
        "fleet:\n  name: alpha\n  bots:\n    ravi: {}\n    newbot: {}\n", encoding="utf-8"
    )
    assert _handles(tmp_path) == ["newbot", "ravi"]


def test_hyphenated_bot_names_are_admitted(tmp_path):
    """`worker-1` is a real bot-name shape (fleet.yaml.example uses it).

    An identifier filter that forbids hyphens would drop exactly those bots from
    the guard while the manifest still looked populated.
    """
    _host(tmp_path, {"alpha": ["worker-1", "reviewer-1"]})
    assert _handles(tmp_path) == ["reviewer-1", "worker-1"]


def test_regex_unsafe_names_are_excluded(tmp_path):
    """The names are spliced into a regex alternation in shell — anything with
    metacharacters or whitespace must never reach it."""
    _host(tmp_path, {"alpha": ["ravi"]})
    (tmp_path / "local" / "alpha" / "fleet.yaml").write_text(
        'fleet:\n  name: alpha\n  bots:\n    ravi: {}\n    "a.b": {}\n'
        '    "x y": {}\n    "-lead": {}\n',
        encoding="utf-8",
    )
    assert _handles(tmp_path) == ["ravi"]


def test_a_broken_sibling_manifest_does_not_empty_the_list(tmp_path):
    """One fleet's bad YAML must not disarm the guard for every other fleet.

    Regression: the first cut wrapped the read in a bare `except Exception`, so
    a missing `import yaml` raised NameError on EVERY fleet and was swallowed as
    "all manifests are broken". The guard composed an EMPTY list and would have
    protected nothing, silently — caught only by running it against the real
    host and seeing 0 handles where 21 were expected.
    """
    _host(tmp_path, {"alpha": ["ravi"], "beta": ["kev"]})
    (tmp_path / "local" / "beta" / "fleet.yaml").write_text("fleet: [oops\n", encoding="utf-8")
    assert _handles(tmp_path) == ["ravi"]


def test_no_fleets_yields_an_empty_but_present_manifest(tmp_path):
    """Present-and-empty is distinguishable by the hook from absent; absent is
    what makes it fail open and complain."""
    (tmp_path / "local").mkdir()
    out = compose_host_bot_handles(Paths(root=tmp_path), output_dir=tmp_path / "out")
    assert out.is_file() and out.read_text() == ""
