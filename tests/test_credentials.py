"""Unit tests for claudlobby/credentials.py — the #1104 credential reconciler.

The properties under test are the ones the issue turns on: shape 3 is never
silently omitted, a shadowed value is never reported as merely missing, and no
credential VALUE ever reaches the output.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import credentials as creds

FLEET_YAML = dedent("""\
    fleet:
      name: t
      service_prefix: com.t
      accounts:
        default: ~/.claude
      bots:
        worker:
          expertise: [software-engineering]
          integrations: [acme]
""")

INTEGRATION = dedent("""\
    ---
    title: Acme
    type: cli
    env_contract:
      ACME_TOKEN:
        description: Acme API token
        default_tier: fleet
    ---

    # Acme
""")


@pytest.fixture
def estate(tmp_path: Path):
    """A root with a SEPARATE fleet dir, so tier shadowing is expressible.

    The shared `make_paths` helper sets root == fleet_dir, which collapses the
    two tiers into one file and makes the shadowing case — the one that bit the
    estate — impossible to write.
    """
    from claudlobby.config import load_fleet
    from claudlobby.paths import Paths

    root = tmp_path / "claudlobby"
    (root / "library" / "integrations").mkdir(parents=True)
    (root / "library" / "mcp").mkdir(parents=True)
    (root / "library" / "integrations" / "acme.md").write_text(INTEGRATION)

    fleet_dir = root / "local" / "t"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)

    fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=fleet_dir)
    return root, fleet_dir, fleet, paths


def _write_env(path: Path, **vars_):
    path.write_text("".join(f"export {k}={v}\n" for k, v in vars_.items()))


def _shape(findings, n):
    return [f for f in findings if f.shape == n]


class TestFrontmatter:
    def test_parses_env_contract(self):
        got = creds.parse_frontmatter_block(INTEGRATION, "env_contract")
        assert "ACME_TOKEN" in got
        assert got["ACME_TOKEN"]["default_tier"] == "fleet"

    def test_absent_key_is_empty_not_error(self):
        assert creds.parse_frontmatter_block(INTEGRATION, "consumer_contract") == {}

    def test_malformed_frontmatter_is_empty_not_raise(self):
        assert (
            creds.parse_frontmatter_block("no frontmatter here", "env_contract") == {}
        )
        assert creds.parse_frontmatter_block("---\nunterminated", "env_contract") == {}


class TestShapeOne:
    def test_value_present_is_ok(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="sekrit")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "OK"

    def test_absent_everywhere_is_fail(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", OTHER="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "absent from every tier" in row.detail

    def test_present_but_empty_is_distinct_from_absent(self, estate):
        """Different diagnosis, different remedy — someone provisioned the slot
        and left it blank, rather than nobody knowing it was needed."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "EMPTY" in row.detail
        assert "absent from every tier" not in row.detail

    def test_shadowed_by_tier_says_so(self, estate):
        """THE #1104 CASE. A value at root while the fleet reads its own .env is
        invisible in practice. Reporting it as plain 'missing' sends someone to
        add a credential that already exists."""
        root, fleet_dir, fleet, paths = estate
        _write_env(root / ".env", ACME_TOKEN="the-good-one")
        _write_env(fleet_dir / ".env", UNRELATED="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "shadowed, not missing" in row.detail
        assert "root" in row.detail


class TestShapeTwo:
    def test_value_with_no_equipped_consumer_fails(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x", ORPHAN_TOKEN="y")
        findings, _ = creds.reconcile(paths, fleet)
        subjects = {f.subject for f in _shape(findings, 2)}
        assert "ORPHAN_TOKEN" in subjects
        assert "ACME_TOKEN" not in subjects

    def test_empty_orphan_is_not_reported(self, estate):
        """An empty key is not a stored credential — reporting it as dead weight
        would bury the real ones."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x", ORPHAN_TOKEN="")
        findings, _ = creds.reconcile(paths, fleet)
        assert "ORPHAN_TOKEN" not in {f.subject for f in _shape(findings, 2)}


class TestShapeThreeIsNeverSilent:
    """The property the whole module turns on."""

    def test_unknown_is_emitted_for_every_equipped_integration(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        rows = _shape(findings, 3)
        assert [r.verdict for r in rows] == ["UNKNOWN"]
        assert "no consumer contract published for acme" in rows[0].detail

    def test_unknown_is_counted_in_the_report(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, scope = creds.reconcile(paths, fleet)
        text = creds.format_report(findings, scope)
        assert "unknown=1" in text
        assert "This is a gap, not a pass." in text

    def test_unknown_alone_does_not_fail_the_run(self, estate):
        """A disclosed gap is not a defect. If UNKNOWN exited nonzero, operators
        would suppress the check and lose shapes 1 and 2 with it."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        assert not creds.exits_nonzero(findings)

    def test_published_contract_resolves_to_ok(self, estate):
        root, fleet_dir, fleet, paths = estate
        (root / "library" / "integrations" / "acme.md").write_text(
            INTEGRATION.replace(
                "---\n\n# Acme",
                "consumer_contract:\n  ACME_TOKEN:\n    read_by: acme-cli\n---\n\n# Acme",
            )
        )
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = _shape(findings, 3)[0]
        assert row.verdict == "OK"
        assert "ACME_TOKEN" in row.detail

    def test_a_failing_run_still_exits_nonzero(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", NOTHING="x")
        findings, _ = creds.reconcile(paths, fleet)
        assert creds.exits_nonzero(findings)


class TestRedaction:
    def test_no_credential_value_reaches_the_report(self, estate):
        """This output goes into PR bodies and chat."""
        root, fleet_dir, fleet, paths = estate
        _write_env(root / ".env", ACME_TOKEN="SUPERSECRETVALUE")
        _write_env(fleet_dir / ".env", ORPHAN="ANOTHERSECRET")
        findings, scope = creds.reconcile(paths, fleet)
        text = creds.format_report(findings, scope)
        assert "SUPERSECRETVALUE" not in text
        assert "ANOTHERSECRET" not in text
        # ...while still naming the KEY, which is the actionable half.
        assert "ACME_TOKEN" in text
        assert "ORPHAN" in text


class TestTierResolution:
    def test_visible_tier_follows_the_shipped_resolver(self, estate):
        """Not a private rule — a second resolution rule is how root-vs-fleet
        became invisible in the first place."""
        root, fleet_dir, _fleet, paths = estate
        _write_env(root / ".env", A="1")
        label, _values = creds.visible_tier(paths)
        assert label == "root"
        _write_env(fleet_dir / ".env", A="2")
        label, _values = creds.visible_tier(paths)
        assert label == "fleet:t"
        assert Path(paths.env_file) == fleet_dir / ".env"

    def test_both_tiers_enumerated_even_though_one_is_read(self, estate):
        root, fleet_dir, _fleet, paths = estate
        _write_env(root / ".env", A="1")
        _write_env(fleet_dir / ".env", B="2")
        assert [t[0] for t in creds.env_tiers(paths)] == ["root", "fleet:t"]
