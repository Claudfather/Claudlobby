"""Ordered reports across parent refreshes; #1661 removes one false bot.env ID warning."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.fixtures.bot_validation_snapshot import snapshot

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def characterized(tmp_path_factory):
    root = tmp_path_factory.mktemp("bot-validation")
    actual = snapshot(SOURCE, root)
    # Substitute only the fixture's absolute root. No message sorting, severity
    # removal, deduplication, or diagnostic normalization is permitted.
    expected = json.loads((SOURCE / "tests/fixtures/bot_validation_expected.json").read_text()
                          .replace("@FIXTURE_ROOT@", str(root)))
    return actual, expected


def test_complete_ordered_public_reports_and_probe_counts(characterized):
    actual, expected = characterized
    assert actual == expected


def test_fixture_exercises_collect_all_order_and_severity(characterized):
    actual, _ = characterized
    assert actual["minimal"]["errors"] == actual["minimal"]["warnings"] == []
    dense = actual["dense"]
    assert dense["has_errors"] is True and dense["has_issues"] is True
    assert dense["errors"][0].startswith("bot 'first': bots.first.env.FOREIGN_PATH")
    assert dense["errors"][-1].startswith("bot 'later': expertise list is empty")
    assert any("credential_sources['Z_TOKEN']" in error and "did you mean 'literal'" in error
               for error in dense["errors"])
    assert "voice file 'missing-voice'" in dense["warnings"][0]
    assert "bot 'later': observability.bridge_heal_max_attempts" in dense["warnings"][-1]
    assert any("integration 'risky-integration'" in error for error in dense["errors"])
    assert any("skill 'risky-skill'" in error for error in dense["errors"])


def test_probe_cost_is_per_run_or_distinct_vault(characterized):
    actual, _ = characterized
    for case in actual.values():
        assert case["probes"]["which:claudron"] == 1
        assert case["probes"]["available_expertise"] == 1
        assert case["probes"]["available_mcp"] == 1
        assert case["probes"]["available_tools"] == 1
    assert "git_identity" not in actual["minimal"]["probes"]
    assert actual["dense"]["probes"]["git_identity"] == 1
    assert actual["dense"]["probes"]["git_rewrite"] == 1
    assert actual["credentials"]["probes"]["git_identity"] == 1
    for case, expected in (("shared-vault", 1), ("distinct-vaults", 2)):
        assert sum(count for key, count in actual[case]["probes"].items() if key.startswith("vault:")) == expected


def test_env_empty_assignment_and_instance_names_remain_meaningful(characterized):
    actual, _ = characterized
    for case in ("env-fleet-empty", "env-bot-empty"):
        assert any("SAMPLE_WORK_TOKEN" in warning and "SET BUT EMPTY" in warning
                   for warning in actual[case]["warnings"])
    assert any("SAMPLE_WORK_TOKEN" in warning and "no .env tier sets it" in warning
               for warning in actual["env-absent"]["warnings"])
    for case in ("env-process", "env-bot-present"):
        assert not any("requires SAMPLE_WORK_TOKEN" in warning for warning in actual[case]["warnings"])
    assert not any("telegram.token_env" in warning for warning in actual["credentials"]["warnings"])


def test_real_overlay_and_effective_equipment_resolution(characterized):
    actual, _ = characterized
    assert actual["overlay"]["errors"] == actual["overlay"]["warnings"] == []
    assert actual["overlay"]["equipment"]["first"] == {
        "skills": ["bundle/", "required-skill"], "integrations": ["bundle/"],
    }
    assert "sample" in actual["dense"]["equipment"]["first"]["integrations"]
    # The explicit bot.conf override now satisfies the env consumer, but its
    # independent App-topology warning remains part of the ordered report.
    assert not any("routing requires GITHUB_APP_ID," in w for w in actual["dense"]["warnings"])
    assert any("bot-tier env overrides GITHUB_APP_ID" in w for w in actual["dense"]["warnings"])
    assert "required-skill" in actual["dense"]["equipment"]["first"]["skills"]
    role_warnings = actual["roles"]["warnings"]
    assert any("bot 'first'" in w and "coordinator" in w for w in role_warnings)
    assert any("bot 'worker'" in w and "worker (not a manager)" in w for w in role_warnings)
    assert not any("bot 'lead'" in w for w in role_warnings)


def test_only_the_existing_value_error_is_collected(characterized):
    actual, _ = characterized
    assert actual["settings-value-error"]["errors"] == ["bot 'first': fixture settings failure"]
    assert actual["settings-value-error"]["exception"] is None
    assert actual["settings-runtime-error"]["exception"] == {
        "type": "RuntimeError", "message": "fixture settings failure",
    }
    assert all(case["exception"] is None for name, case in actual.items() if name != "settings-runtime-error")


@pytest.mark.parametrize("modules", [("validator", "composer"), ("composer", "validator")])
def test_import_order_in_fresh_process(modules, tmp_path):
    code = "import importlib; from pathlib import Path; " + "; ".join(
        f"assert Path(importlib.import_module('claudlobby.{name}').__file__).resolve().is_relative_to(Path({str(SOURCE)!r}))"
        for name in modules
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=SOURCE, text=True, capture_output=True,
                            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(SOURCE),
                                 "PLANE_EMIT_DISABLED": "1", "PLANE_SOCKET": str(tmp_path / "unbound.sock")})
    assert result.returncode == 0, result.stderr
