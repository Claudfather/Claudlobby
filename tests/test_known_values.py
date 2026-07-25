"""Tests for known_values.py — known-good sets, closest_match, _parse_effort,
and validator checks that use them."""

from __future__ import annotations


from claudlobby.known_values import (
    _AUTO_ELIGIBLE_RENAMES,
    _AUTO_ELIGIBLE_STANDALONE,
    AUTO_ELIGIBLE_SKILLS,
    BYPASS_ACTIONS,
    CLAUDNA_SKILL_RENAMES,
    EXPERTISE_CORE_TOOLS,
    KNOWN_EFFORTS,
    KNOWN_HOOK_EVENTS,
    KNOWN_MODELS,
    OUTCOME_ACTIONS,
    OUTCOME_KEYS,
    VALID_PERMISSION_MODES,
    closest_match,
)


# ── closest_match ────────────────────────────────────────────────


class TestClosestMatch:
    def test_exact_match(self):
        assert closest_match("sonnet", KNOWN_MODELS) == "sonnet"

    def test_close_typo(self):
        assert closest_match("sonnett", KNOWN_MODELS) == "sonnet"

    def test_case_insensitive(self):
        assert closest_match("SONNET", KNOWN_MODELS) == "sonnet"
        assert closest_match("High", KNOWN_EFFORTS) == "high"

    def test_no_match(self):
        assert closest_match("totally-bogus-model-xyz", KNOWN_MODELS) is None

    def test_hook_event_typo(self):
        assert closest_match("PreTooluse", KNOWN_HOOK_EVENTS) == "PreToolUse"
        assert closest_match("PostTooluse", KNOWN_HOOK_EVENTS) == "PostToolUse"

    def test_effort_typo(self):
        assert closest_match("hig", KNOWN_EFFORTS) == "high"
        assert closest_match("mediumm", KNOWN_EFFORTS) == "medium"

    def test_distant_value_returns_none(self):
        assert closest_match("x", KNOWN_EFFORTS) is None

    def test_permission_mode_typo(self):
        assert closest_match("dontask", VALID_PERMISSION_MODES) == "dontAsk"


# ── Known-good sets are non-empty ─────────────────────────────────


class TestSetsPopulated:
    def test_known_models(self):
        assert len(KNOWN_MODELS) >= 3
        assert "sonnet" in KNOWN_MODELS
        assert "opus" in KNOWN_MODELS
        assert "haiku" in KNOWN_MODELS
        assert "fable" in KNOWN_MODELS
        assert "claude-fable-5" in KNOWN_MODELS
        # Current GA full-ID pins (#771): a bot pinning the live model must
        # not trip the "not in known models" warning.
        assert "claude-opus-4-8" in KNOWN_MODELS
        assert "claude-sonnet-5" in KNOWN_MODELS

    def test_known_efforts(self):
        assert KNOWN_EFFORTS == frozenset({"low", "medium", "high", "max"})

    def test_known_hook_events(self):
        # The full authoritative Claude Code hook-event set
        # (code.claude.com/docs/en/hooks.md), pinned literally here as an
        # independent spec. Completeness matters: a missing name false-warns a
        # valid hook as "silently ignored" (#771/#777).
        authoritative = {
            "SessionStart",
            "Setup",
            "UserPromptSubmit",
            "UserPromptExpansion",
            "PreToolUse",
            "PermissionRequest",
            "PermissionDenied",
            "PostToolUse",
            "PostToolUseFailure",
            "PostToolBatch",
            "Notification",
            "MessageDisplay",
            "SubagentStart",
            "SubagentStop",
            "TaskCreated",
            "TaskCompleted",
            "Stop",
            "StopFailure",
            "TeammateIdle",
            "InstructionsLoaded",
            "ConfigChange",
            "CwdChanged",
            "FileChanged",
            "WorktreeCreate",
            "WorktreeRemove",
            "PreCompact",
            "PostCompact",
            "Elicitation",
            "ElicitationResult",
            "SessionEnd",
        }
        missing = authoritative - KNOWN_HOOK_EVENTS
        assert not missing, f"KNOWN_HOOK_EVENTS missing: {sorted(missing)}"

    def test_permission_modes(self):
        assert "default" in VALID_PERMISSION_MODES
        assert "bypassPermissions" in VALID_PERMISSION_MODES

    def test_auto_eligible_skills(self):
        # Pin the dead -> live rename map exactly — keys AND values. The
        # consolidated token is not the old name minus prefix (security-audit ->
        # `security`, docs-review -> `docs`, frontend-performance-audit ->
        # `frontend-perf`), so the values must be pinned literally.
        assert _AUTO_ELIGIBLE_RENAMES == {
            "/claudna:tech-debt": "/claudna:audit tech-debt",
            "/claudna:security-audit": "/claudna:audit security",
            "/claudna:docs-review": "/claudna:audit docs",
            "/claudna:access-path-audit": "/claudna:audit access-path",
            "/claudna:frontend-performance-audit": "/claudna:audit frontend-perf",
            "/claudna:session-handoff": "/claudna:session handoff",
        }
        # The four un-consolidated standalones survive under their own names.
        assert _AUTO_ELIGIBLE_STANDALONE == frozenset(
            {
                "/claudna:product-enhance",
                "/claudna:product-vision",
                "/claudna:visual-crawl",
                "/claudna:implement-plan",
            }
        )
        # SSOT invariant: eligible == the live rename targets + the survivors, so
        # a dead hyphen-name can never be eligible.
        assert AUTO_ELIGIBLE_SKILLS == (
            frozenset(_AUTO_ELIGIBLE_RENAMES.values()) | _AUTO_ELIGIBLE_STANDALONE
        )

    def test_outcome_keys(self):
        assert "completed" in OUTCOME_KEYS
        assert "blocked" in OUTCOME_KEYS

    def test_outcome_actions(self):
        assert "report" in OUTCOME_ACTIONS

    def test_bypass_actions(self):
        assert "comment_and_label" in BYPASS_ACTIONS

    def test_expertise_core_tools(self):
        assert "Write" in EXPERTISE_CORE_TOOLS["software-engineering"]
        assert "Agent" in EXPERTISE_CORE_TOOLS["orchestration"]


# ── SSOT cross-check: guards derive from the canonical rename map ─


class TestClaudnaRenameSSOT:
    """The dead-name CI guards and the auto-eligible subset must stay views of
    CLAUDNA_SKILL_RENAMES — hand-retyped guard lists are the drift class this
    pins against (#570)."""

    def test_doc_guard_covers_every_dead_name(self):
        from tests.test_no_dead_claudna_refs import DEAD_REF

        for dead in CLAUDNA_SKILL_RENAMES:
            bare = "/" + dead.removeprefix("/claudna:")
            assert DEAD_REF.search(dead), dead
            assert DEAD_REF.search(bare), bare

    def test_session_guard_covers_every_dead_session_name(self):
        from tests.test_no_dead_session_command import _DEAD_SESSION_CMD

        session_dead = {
            k
            for k, live in CLAUDNA_SKILL_RENAMES.items()
            if live.startswith("/claudna:session ")
        }
        # The three standalones the session engine replaced — pinned literally,
        # like the auto-eligible map above.
        assert session_dead == {
            "/claudna:session-handoff",
            "/claudna:session-resume",
            "/claudna:name-session",
        }
        for dead in session_dead:
            bare = "/" + dead.removeprefix("/claudna:")
            assert _DEAD_SESSION_CMD.search(dead), dead
            assert _DEAD_SESSION_CMD.search(bare), bare

    def test_live_values_never_read_as_dead(self):
        from tests.test_no_dead_claudna_refs import DEAD_REF
        from tests.test_no_dead_session_command import _DEAD_SESSION_CMD

        for live in CLAUDNA_SKILL_RENAMES.values():
            assert not DEAD_REF.search(live), live
            assert not _DEAD_SESSION_CMD.search(live), live

    def test_auto_eligible_is_subset_of_canonical(self):
        assert _AUTO_ELIGIBLE_RENAMES.items() <= CLAUDNA_SKILL_RENAMES.items()

    def test_session_hyphen_typo_policy_agrees_across_guards(self):
        """The session-hyphen typo family (live verbs with no dead standalone,
        e.g. `checkpoint`) is guard-local pattern policy — pin the guards'
        intended split so it cannot drift silently: both ban the namespaced
        form; the bare form is doc-guard territory, and lib/ coverage for it
        rides on the doc guard scanning lib/*.sh."""
        from tests.test_no_dead_claudna_refs import DEAD_REF, SCAN_GLOBS
        from tests.test_no_dead_session_command import _DEAD_SESSION_CMD

        assert DEAD_REF.search("/claudna:session-checkpoint")
        assert _DEAD_SESSION_CMD.search("/claudna:session-checkpoint")
        assert DEAD_REF.search("/session-checkpoint")
        assert ("lib", "*.sh") in SCAN_GLOBS
        # Neither guard flags the live space-form or the unrelated bare tmux
        # `session-name` (no leading slash).
        for benign in ("/claudna:session resume", "tmux session-name x"):
            assert not DEAD_REF.search(benign), benign
            assert not _DEAD_SESSION_CMD.search(benign), benign


# ── _parse_enum (config.py) ──────────────────────────────────────


class TestParseEnum:
    def test_valid_efforts(self):
        from claudlobby.config import _parse_enum

        assert _parse_enum("effort", "high", KNOWN_EFFORTS) == "high"
        assert _parse_enum("effort", "low", KNOWN_EFFORTS) == "low"
        assert _parse_enum("effort", "medium", KNOWN_EFFORTS) == "medium"
        assert _parse_enum("effort", "max", KNOWN_EFFORTS) == "max"

    def test_valid_permission_modes(self):
        from claudlobby.config import _parse_enum

        assert (
            _parse_enum("permission_mode", "default", VALID_PERMISSION_MODES)
            == "default"
        )
        assert (
            _parse_enum("permission_mode", "dontAsk", VALID_PERMISSION_MODES)
            == "dontAsk"
        )

    def test_none_passes_through(self):
        from claudlobby.config import _parse_enum

        assert _parse_enum("effort", None, KNOWN_EFFORTS) is None

    def test_invalid_raises_with_suggestion(self):
        import pytest as _pytest

        from claudlobby.config import _parse_enum

        with _pytest.raises(ValueError, match="Did you mean 'high'"):
            _parse_enum("effort", "hig", KNOWN_EFFORTS)

    def test_invalid_raises_without_suggestion(self):
        import pytest as _pytest

        from claudlobby.config import _parse_enum

        with _pytest.raises(ValueError, match="Must be one of"):
            _parse_enum("effort", "turbo", KNOWN_EFFORTS)

    def test_permission_mode_suggestion(self):
        import pytest as _pytest

        from claudlobby.config import _parse_enum

        with _pytest.raises(ValueError, match="Did you mean 'dontAsk'"):
            _parse_enum("permission_mode", "dontask", VALID_PERMISSION_MODES)


# ── Validator integration: model/hook/model_strategy checks ──────


class TestValidatorModelCheck:
    """Validator emits warnings for unknown models."""

    def _make_fleet_and_validate(self, tmp_path, bot_kwargs):
        from claudlobby.config import BotConfig, FleetConfig
        from claudlobby.paths import Paths
        from claudlobby.validator import ValidationReport, _validate_bots

        root = tmp_path / "claudlobby"
        root.mkdir()
        lib = root / "library"
        lib.mkdir()
        (root / "lib").mkdir()
        # Create expertise file so it passes
        exp_dir = lib / "expertise"
        exp_dir.mkdir()
        (exp_dir / "eng.md").write_text("---\ntitle: eng\n---\n# eng\n")

        paths = Paths(root=root)
        bot = BotConfig(bot_id="test", name="test", expertise=["eng"], **bot_kwargs)
        fleet = FleetConfig(name="test", service_prefix="com.test", bots={"test": bot})
        report = ValidationReport()
        _validate_bots(fleet, paths, {}, report)
        return report

    def test_known_model_no_warning(self, tmp_path):
        report = self._make_fleet_and_validate(tmp_path, {"model": "sonnet"})
        model_warnings = [
            w for w in report.warnings if "model" in w.lower() and "known models" in w
        ]
        assert len(model_warnings) == 0

    def test_unknown_model_warns(self, tmp_path):
        report = self._make_fleet_and_validate(tmp_path, {"model": "gpt-4"})
        model_warnings = [w for w in report.warnings if "known models" in w]
        assert len(model_warnings) == 1
        assert "gpt-4" in model_warnings[0]

    def test_unknown_model_with_suggestion(self, tmp_path):
        report = self._make_fleet_and_validate(tmp_path, {"model": "sonnett"})
        model_warnings = [w for w in report.warnings if "known models" in w]
        assert len(model_warnings) == 1
        assert "did you mean 'sonnet'" in model_warnings[0]

    def test_no_model_no_warning(self, tmp_path):
        report = self._make_fleet_and_validate(tmp_path, {})
        model_warnings = [w for w in report.warnings if "known models" in w]
        assert len(model_warnings) == 0

    def test_current_full_id_pins_no_warning(self, tmp_path):
        # A bot pinning a current GA full model ID must not warn (#771).
        for i, model in enumerate(("claude-opus-4-8", "claude-sonnet-5")):
            sub = tmp_path / f"case{i}"
            sub.mkdir()
            report = self._make_fleet_and_validate(sub, {"model": model})
            model_warnings = [w for w in report.warnings if "known models" in w]
            assert model_warnings == [], f"{model} unexpectedly warned"


class TestValidatorHookEventCheck:
    """Validator emits warnings for unknown hook events."""

    def _make_fleet_and_validate(self, tmp_path, hooks):
        from claudlobby.config import BotConfig, FleetConfig
        from claudlobby.paths import Paths
        from claudlobby.validator import ValidationReport, _validate_bots

        root = tmp_path / "claudlobby"
        root.mkdir()
        lib = root / "library"
        lib.mkdir()
        (root / "lib").mkdir()
        exp_dir = lib / "expertise"
        exp_dir.mkdir()
        (exp_dir / "eng.md").write_text("---\ntitle: eng\n---\n# eng\n")

        paths = Paths(root=root)
        bot = BotConfig(bot_id="test", name="test", expertise=["eng"], hooks=hooks)
        fleet = FleetConfig(name="test", service_prefix="com.test", bots={"test": bot})
        report = ValidationReport()
        _validate_bots(fleet, paths, {}, report)
        return report

    def test_known_event_no_warning(self, tmp_path):
        hooks = {"PreToolUse": [{"command": "echo hi"}]}
        report = self._make_fleet_and_validate(tmp_path, hooks)
        hook_warnings = [w for w in report.warnings if "not recognized" in w]
        assert len(hook_warnings) == 0

    def test_unknown_event_warns(self, tmp_path):
        hooks = {"BeforeToolUse": [{"command": "echo hi"}]}
        report = self._make_fleet_and_validate(tmp_path, hooks)
        hook_warnings = [w for w in report.warnings if "not recognized" in w]
        assert len(hook_warnings) == 1
        assert "BeforeToolUse" in hook_warnings[0]

    def test_unknown_event_with_suggestion(self, tmp_path):
        hooks = {"PreTooluse": [{"command": "echo hi"}]}
        report = self._make_fleet_and_validate(tmp_path, hooks)
        hook_warnings = [w for w in report.warnings if "not recognized" in w]
        assert len(hook_warnings) == 1
        assert "did you mean 'PreToolUse'" in hook_warnings[0]

    def test_all_known_hook_events_validate_clean(self, tmp_path):
        # Non-vacuous: drive EVERY recognized event through the real validator
        # and assert zero "not recognized" warnings. (A live-fleet validate is
        # vacuous — the fleet declares none of these names — so exercise the
        # allowlist directly, #777.)
        for i, event in enumerate(sorted(KNOWN_HOOK_EVENTS)):
            sub = tmp_path / f"e{i}"
            sub.mkdir()
            report = self._make_fleet_and_validate(
                sub, {event: [{"command": "echo hi"}]}
            )
            not_recognized = [w for w in report.warnings if "not recognized" in w]
            assert not_recognized == [], f"{event} unexpectedly warned"


class TestValidatorExpertiseSuggestion:
    """Validator suggests closest expertise name on typo."""

    def test_expertise_typo_suggestion(self, tmp_path):
        from claudlobby.config import BotConfig, FleetConfig
        from claudlobby.paths import Paths
        from claudlobby.validator import ValidationReport, _validate_bots

        root = tmp_path / "claudlobby"
        root.mkdir()
        lib = root / "library"
        lib.mkdir()
        (root / "lib").mkdir()
        exp_dir = lib / "expertise"
        exp_dir.mkdir()
        (exp_dir / "software-engineering.md").write_text("---\ntitle: SE\n---\n# SE\n")

        paths = Paths(root=root)
        bot = BotConfig(
            bot_id="test",
            name="test",
            expertise=["software-enginering"],  # typo
        )
        fleet = FleetConfig(name="test", service_prefix="com.test", bots={"test": bot})
        report = ValidationReport()
        _validate_bots(fleet, paths, {}, report)
        expertise_errors = [e for e in report.errors if "expertise" in e]
        assert len(expertise_errors) == 1
        assert "did you mean 'software-engineering'" in expertise_errors[0]
