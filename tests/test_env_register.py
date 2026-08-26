"""The derived credential register (#1214 F6 / #1226 stage 4).

The register earns its place only if it shows what was SHADOWED, not just what
resolved — so the shadowing tests here are the point of the file, and the
"reports what resolved" tests are the floor.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import env_register as reg
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

REPO_ROOT = Path(__file__).resolve().parent.parent

FLEET = dedent("""\
    fleet:
      name: acme
      service_prefix: com.acme
      defaults:
        expertise: [x]
        mcp: [github]
      bots:
        solo:
          expertise: [x]
    """)


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    """A root carrying the REAL resolver, so the register reads the real order."""
    for kind in ("mcp", "expertise", "integrations", "skills"):
        (tmp_path / "library" / kind).mkdir(parents=True)
    (tmp_path / "lib").mkdir()
    for f in ("lib-common.sh", "env-tiers.sh"):
        (tmp_path / "lib" / f).write_bytes((REPO_ROOT / "lib" / f).read_bytes())
    (tmp_path / "library" / "expertise" / "x.md").write_text("---\ntitle: x\n---\n# x\n")
    (tmp_path / "library" / "mcp" / "github.json").write_text(
        json.dumps(
            {
                "mcpServers": {"github": {"command": "x", "env": {"T": "${GITHUB_PAT}"}}},
                "_env_contract": {
                    "GITHUB_PAT": {
                        "description": "PAT",
                        "default_tier": "fleet",
                        "secret": True,
                    }
                },
            }
        )
    )
    # Real overlay layout: the fleet lives at local/<name>/, which is where the
    # runtime resolver looks. A fixture with root == fleet_dir makes the fleet
    # tier unreachable and would have tested a shape production never has.
    fleet_dir = tmp_path / "local" / "acme"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(FLEET)
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
    paths = Paths(root=tmp_path, fleet_dir=fleet_dir)
    paths.bot_runtime("solo").mkdir(parents=True)
    return fleet, paths, fleet_dir, home


def _row(r, name="GITHUB_PAT"):
    return next(x for x in r.rows if x.name == name)


def test_a_var_set_once_reports_set(world) -> None:
    fleet, paths, root, _ = world
    (root / ".env").write_text("export GITHUB_PAT=real\n")
    r = reg.build(fleet, paths, bot="solo")
    assert _row(r).state == reg.SET
    assert _row(r).tier == "fleet"
    assert reg.exits_nonzero(r) is False


def test_a_var_no_tier_sets_reports_unset(world) -> None:
    fleet, paths, _, _ = world
    r = reg.build(fleet, paths, bot="solo")
    assert _row(r).state == reg.UNSET
    assert _row(r).tier == "-"
    # UNSET must not fail the command: an un-filled credential is ordinary, and
    # failing on it trains operators to ignore the tool, taking the real signal.
    assert reg.exits_nonzero(r) is False


def test_an_empty_win_over_a_real_value_is_BLANKED_and_names_the_loser(world) -> None:
    """The row the whole register exists for.

    Invisible to every other check: the key IS set, so nothing calls it
    missing; a value DOES exist, so nothing calls it unconfigured.
    """
    fleet, paths, root, home = world
    (home / ".env").write_text("export GITHUB_PAT=real_host_value\n")
    (root / ".env").write_text("export GITHUB_PAT=\n")
    r = reg.build(fleet, paths, bot="solo")
    row = _row(r)
    assert row.state == reg.BLANKED
    assert row.tier == "fleet"
    assert row.blanked == ("host",)
    assert reg.exits_nonzero(r) is True

    out = reg.format_report(r)
    assert "BLANKED" in out
    assert "overrides a real value at: host" in out


def test_an_empty_with_nothing_upstream_is_EMPTY_not_BLANKED(world) -> None:
    """Distinct states, opposite remedies — and conflating them would make the
    BLANKED count fire on every unfilled stub, burying the real rows."""
    fleet, paths, root, _ = world
    (root / ".env").write_text("export GITHUB_PAT=\n")
    r = reg.build(fleet, paths, bot="solo")
    assert _row(r).state == reg.EMPTY
    assert _row(r).blanked == ()
    assert reg.exits_nonzero(r) is False


def test_worst_rows_sort_first(world) -> None:
    fleet, paths, root, home = world
    (home / ".env").write_text("export GITHUB_PAT=real\n")
    (root / ".env").write_text("export GITHUB_PAT=\nexport OTHER=x\n")
    r = reg.build(fleet, paths, bot="solo")
    assert r.rows[0].state == reg.BLANKED


def test_no_credential_value_is_ever_emitted(world) -> None:
    """Rows carry a key, a tier and a state. The report is pasted into chat."""
    fleet, paths, root, home = world
    (home / ".env").write_text("export GITHUB_PAT=SUPERSECRETVALUE\n")
    (root / ".env").write_text("export GITHUB_PAT=\n")
    r = reg.build(fleet, paths, bot="solo")
    assert "SUPERSECRETVALUE" not in reg.format_report(r)
    assert "SUPERSECRETVALUE" not in json.dumps([x._asdict() for x in r.rows])


def test_undeclared_keys_are_reported_not_actioned(world) -> None:
    """Usually legitimate — but also what a renamed contract key leaves behind,
    and nothing else says so."""
    fleet, paths, root, _ = world
    (root / ".env").write_text("export GITHUB_PAT=v\nexport LEFTOVER_KEY=1\n")
    r = reg.build(fleet, paths, bot="solo")
    assert "LEFTOVER_KEY" in r.undeclared
    assert all(x.name != "LEFTOVER_KEY" for x in r.rows)


def test_the_bot_tier_is_reported_unresolved_without_a_bot(world) -> None:
    """Saying so beats silently answering a narrower question than was asked."""
    fleet, paths, _, _ = world
    r = reg.build(fleet, paths, bot=None)
    states = {t: st for t, _, st in r.tiers}
    assert states["bot"] == "unresolved"
    assert "not shown here" in reg.format_report(r)


def test_the_register_refuses_rather_than_guessing(world) -> None:
    """Its whole claim is that it reports what a boot would actually find."""
    fleet, paths, _, _ = world
    (paths.root / "lib" / "env-tiers.sh").unlink()
    with pytest.raises(reg.ResolverUnavailable):
        reg.build(fleet, paths, bot="solo")


def test_declarations_come_from_the_shipped_walk_not_a_private_scan(world) -> None:
    """A private scan would report vars the compositor does not believe in, or
    miss ones it does — the exact class of defect creds-reconcile already hit."""
    src = Path(reg.__file__).read_text()
    assert "collect_env_contracts" in src
