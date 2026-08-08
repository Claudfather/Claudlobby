"""#1043 — the two roster doors must agree on the happy path and only there.

`parse_fleet_bots` soft-fails by contract: a missing or unparseable manifest
yields no output, and `bot_in_fleet` reads an empty list as "declared", so its
callers fall back to scanning every directory. That is CORRECT for an action —
a supervision filter has to keep working on a host whose manifest is briefly
broken — and WRONG for a measurement, whose denominator has no degraded mode: a
roster that silently shrinks by a whole fleet turns "6 of 21" into "6 of 19" and
the baseline stops being comparable.

So there are two doors rather than one widened one, and this is the gate that
keeps them honest. Widening `parse_fleet_bots` to fail loudly would break four
supervision scripts; narrowing `declared_bots_strict` to soft-fail would
reinstate the silent denominator. The pair is only safe while they agree
wherever nothing is broken and diverge wherever something is.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib" / "lib-common.sh"

MANIFEST = textwrap.dedent(
    """\
    fleet:
      name: {name}
      bots:
    {bots}
      plugins:
        additional: []
    """
)


def _run(root: Path, snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set +e; source "{LIB}" >/dev/null 2>&1; set +e; {snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"CLAUDLOBBY_ROOT": str(root), "HOME": str(root), "PATH": "/usr/bin:/bin"},
    )


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    for fleet, bots in (("alpha", ["one", "two"]), ("beta", ["three"])):
        d = tmp_path / "local" / "home" / fleet
        d.mkdir(parents=True)
        body = "\n".join(f"    {b}:\n      expertise: [x]" for b in bots)
        (d / "fleet.yaml").write_text(MANIFEST.format(name=fleet, bots=body))
    return tmp_path


def _strict_names(root: Path) -> list[str]:
    out = _run(root, "declared_bots_strict /dev/null | cut -f1 | sort")
    return [ln for ln in out.stdout.split() if ln]


def _soft_names(root: Path) -> list[str]:
    out = _run(
        root,
        'for m in "$CLAUDLOBBY_ROOT"/local/*/*/fleet.yaml; do '
        'parse_fleet_bots "$m"; done | sort',
    )
    return [ln for ln in out.stdout.split() if ln]


def test_the_doors_agree_when_nothing_is_broken(estate: Path):
    assert _strict_names(estate) == ["one", "three", "two"]
    assert _soft_names(estate) == _strict_names(estate)


def test_strict_door_is_loud_where_the_soft_door_is_silent(estate: Path):
    """The whole reason both exist: same broken input, opposite correct answer."""
    broken = estate / "local" / "home" / "beta" / "fleet.yaml"
    broken.write_text("fleet:\n  name: beta\n  # no bots block at all\n")

    soft = _run(estate, f'parse_fleet_bots "{broken}"; echo "rc=$?"')
    assert "rc=0" in soft.stdout, soft.stdout
    assert soft.stdout.strip() == "rc=0", (
        "parse_fleet_bots must stay silent and successful on a broken manifest — "
        f"four supervision scripts depend on it: {soft.stdout!r}"
    )

    strict = _run(estate, 'declared_bots_strict /dev/null >/dev/null; echo "rc=$?"')
    assert "rc=1" in strict.stdout, (
        f"declared_bots_strict must fail loudly on the same input: {strict.stdout!r}"
    )


def test_the_broken_manifest_is_named_not_merely_counted(estate: Path, tmp_path: Path):
    broken = estate / "local" / "home" / "beta" / "fleet.yaml"
    broken.write_text("fleet:\n  name: beta\n")
    report = tmp_path / "bad.txt"
    _run(estate, f'declared_bots_strict "{report}" >/dev/null')
    text = report.read_text()
    assert "beta/fleet.yaml" in text, text
    assert "bots:" in text, f"the reason must be stated, not just the path: {text!r}"


def test_a_partial_roster_is_still_emitted_so_the_caller_decides(estate: Path):
    """rc says "do not trust this"; the rows still come out.

    The caller owns the policy — selfstart-snapshot refuses outright unless an
    override is set, while another caller might legitimately proceed. Swallowing
    the rows here would take that decision away from both.
    """
    (estate / "local" / "home" / "beta" / "fleet.yaml").write_text(
        "fleet:\n  name: beta\n"
    )
    assert _strict_names(estate) == ["one", "two"]
