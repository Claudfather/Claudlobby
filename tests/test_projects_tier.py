"""projects.yaml — the third config tier (goal-aware-fleet plan, Phase 2).

Schema + semantics: documentation/projects-yaml-schema.md. Load -> validate
-> compose, mirroring fleet.yaml; the repo->tier map lands in EVERY bot's
bot.conf.
"""

from pathlib import Path
from textwrap import dedent

from tests.conftest import install_real_template

from claudlobby.composer import compose_bot_conf, compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate

REPO_DIR = Path(__file__).resolve().parent.parent

# Shared fixture: 'surprise' is an unknown key; 'metrics' is the reserved one.
UNKNOWN_KEYS_YAML = (
    "projects:\n  p:\n    title: P\n    repos: [a/b]\n    surprise: 1\n"
    "    metrics: [{name: x}]\n"
)

PROJECTS_YAML = dedent("""\
    projects:
      acme-shop:
        title: Acme Shop storefront
        repos: [acme/storefront, acme/autopilot]
        mission_file: missions/acme-shop.md
        validation:
          tier: preview
          preview:
            source: vercel
            require_ack: true
          notes: revenue-facing; preview link + operator ack before close
      post-scheduler:
        title: Post Scheduler SaaS
        repos: [acme/post-scheduler]
        validation:
          tier: review
""")


def _write_projects(fleet_dir: Path, text: str = PROJECTS_YAML) -> None:
    (fleet_dir / "projects.yaml").write_text(text)


def _load(fleet_dir: Path):
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


# --- config: loading -----------------------------------------------------------


def test_projects_yaml_loaded_beside_fleet_yaml(fleet_dir):
    _write_projects(fleet_dir)
    fleet = _load(fleet_dir)
    assert set(fleet.projects) == {"acme-shop", "post-scheduler"}
    p = fleet.projects["acme-shop"]
    assert p.key == "acme-shop"
    assert p.title == "Acme Shop storefront"
    assert p.repos == ["acme/storefront", "acme/autopilot"]
    assert p.mission_file == "missions/acme-shop.md"
    assert p.validation.tier == "preview"
    assert p.validation.preview == {"source": "vercel", "require_ack": True}
    assert "operator ack" in p.validation.notes


def test_no_projects_yaml_means_empty_projects(fleet_dir):
    fleet = _load(fleet_dir)
    assert fleet.projects == {}


def test_validation_block_optional_default_tier_review(fleet_dir):
    _write_projects(
        fleet_dir,
        "projects:\n  bare:\n    title: Bare\n    repos: [acme/bare]\n",
    )
    fleet = _load(fleet_dir)
    assert fleet.projects["bare"].validation.tier == "review"


def test_all_comments_projects_yaml_is_still_optional(fleet_dir):
    # A fully commented-out file parses to None — must behave like absence.
    _write_projects(fleet_dir, "# projects:\n#   p:\n#     repos: [a/b]\n")
    assert _load(fleet_dir).projects == {}


# --- validator -----------------------------------------------------------------


def _validated(fleet_dir):
    fleet = _load(fleet_dir)
    return validate(fleet, _paths(fleet_dir))


def test_valid_projects_produce_no_project_findings(fleet_dir):
    (fleet_dir / "missions").mkdir()
    (fleet_dir / "missions" / "acme-shop.md").write_text("# mission\n")
    _write_projects(fleet_dir)
    report = _validated(fleet_dir)
    assert not [e for e in report.errors if "project" in e.lower()]
    assert not [w for w in report.warnings if "project" in w.lower()]


def test_bad_tier_errors_with_did_you_mean(fleet_dir):
    _write_projects(
        fleet_dir,
        "projects:\n  p:\n    title: P\n    repos: [a/b]\n"
        "    validation: {tier: reviw}\n",
    )
    report = _validated(fleet_dir)
    hits = [e for e in report.errors if "reviw" in e]
    assert hits, report.errors
    assert "review" in hits[0], "should suggest the closest valid tier"


def test_empty_repos_errors(fleet_dir):
    _write_projects(fleet_dir, "projects:\n  p:\n    title: P\n    repos: []\n")
    report = _validated(fleet_dir)
    assert any("repos" in e and "p" in e for e in report.errors)


def test_bad_project_key_errors(fleet_dir):
    _write_projects(
        fleet_dir, "projects:\n  Bad Key!:\n    title: P\n    repos: [a/b]\n"
    )
    report = _validated(fleet_dir)
    assert any("Bad Key!" in e for e in report.errors)


def test_unknown_key_warns_and_metrics_warns_reserved(fleet_dir):
    _write_projects(fleet_dir, UNKNOWN_KEYS_YAML)
    # Load preserves unknowns in .raw (the precondition the warnings read from)
    p = _load(fleet_dir).projects["p"]
    assert set(p.raw) == {"surprise", "metrics"}
    report = _validated(fleet_dir)
    assert any("surprise" in w for w in report.warnings)
    metrics_w = [w for w in report.warnings if "metrics" in w]
    assert metrics_w and "reserved" in metrics_w[0]


def test_missing_mission_file_warns(fleet_dir):
    _write_projects(
        fleet_dir,
        "projects:\n  p:\n    title: P\n    repos: [a/b]\n"
        "    mission_file: missions/nope.md\n",
    )
    report = _validated(fleet_dir)
    assert any("missions/nope.md" in w for w in report.warnings)


def test_overlapping_repos_across_projects_warns(fleet_dir):
    _write_projects(
        fleet_dir,
        "projects:\n"
        "  p1: {title: P1, repos: [acme/shared]}\n"
        "  p2: {title: P2, repos: [acme/shared]}\n",
    )
    report = _validated(fleet_dir)
    assert any(
        "acme/shared" in w and "p1" in w and "p2" in w for w in report.warnings
    ), "repo claimed by two projects makes tier resolution ambiguous"


# --- composer ------------------------------------------------------------------


def test_every_bot_conf_carries_the_tier_map(fleet_dir):
    _write_projects(fleet_dir)
    fleet = _load(fleet_dir)
    paths = _paths(fleet_dir)
    for bot_id in ("lead", "worker-1"):  # manager AND worker — no "sprint owner"
        conf = compose_bot_conf(fleet.bots[bot_id], fleet, paths)
        # _shq quotes only when needed: bare tier, quoted space-joined repos
        assert "export PROJECT_TIER_ACME_SHOP=preview" in conf
        assert (
            "export PROJECT_REPOS_ACME_SHOP='acme/storefront acme/autopilot'" in conf
        )
        assert "export PROJECT_TIER_POST_SCHEDULER=review" in conf


def test_no_projects_no_project_env(fleet_dir):
    fleet = _load(fleet_dir)
    conf = compose_bot_conf(fleet.bots["lead"], fleet, _paths(fleet_dir))
    assert "PROJECT_TIER_" not in conf


def test_manager_claude_md_gets_projects_table(fleet_dir):
    install_real_template(fleet_dir)
    _write_projects(fleet_dir)
    fleet = _load(fleet_dir)
    md = compose_claude_md(fleet.bots["lead"], fleet, _paths(fleet_dir))
    assert "## Projects" in md
    assert "acme-shop" in md and "preview" in md
    assert "missions/acme-shop.md" in md


def test_worker_claude_md_has_no_projects_table(fleet_dir):
    install_real_template(fleet_dir)
    _write_projects(fleet_dir)
    fleet = _load(fleet_dir)
    md = compose_claude_md(fleet.bots["worker-1"], fleet, _paths(fleet_dir))
    assert "## Projects" not in md


# --- root example files --------------------------------------------------------


def test_projects_yaml_example_parses_and_exercises_v1_schema(fleet_dir):
    example = REPO_DIR / "projects.yaml.example"
    assert example.is_file(), "projects.yaml.example must ship at repo root"
    _write_projects(fleet_dir, example.read_text())
    fleet = _load(fleet_dir)
    assert fleet.projects, "example must define at least one project"
    fields = set()
    for p in fleet.projects.values():
        assert not p.raw, f"example uses unknown keys: {sorted(p.raw)}"
        fields.update(
            k
            for k, v in (
                ("mission_file", p.mission_file),
                ("preview", p.validation.preview),
                ("notes", p.validation.notes),
            )
            if v
        )
    assert fields == {"mission_file", "preview", "notes"}, (
        "example must exercise every v1 field across its projects"
    )
    text = example.read_text()
    assert "# metrics:" in text and "metrics plan" in text, (
        "metrics block must be present but commented, marked reserved"
    )


def test_fleet_yaml_example_cross_references_projects_yaml():
    assert "projects.yaml" in (REPO_DIR / "fleet.yaml.example").read_text()
