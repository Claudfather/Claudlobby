"""#1102 R3 / M1 — the composed SessionStart boot-brief hook.

Three properties, each the sole defense for its class:

1. **Default off means byte-off.** With no ``brief:`` stanza the composed
   settings are identical to pre-M1 output — `claudlobby generate` on an
   unchanged fleet.yaml produces zero diff (#904 acceptance criterion).
2. **The compose-time gate is the arming decision.** Composed settings outlive
   installs on this estate (the merged-but-not-installed gap was measured live
   during R3-F1 ratification — #1102), so the knob must refuse to arm against
   a CLI that lacks the verb, and the probe must not run when the knob is off.
3. **The hook entries are exactly the locked shape**: matchers ``startup`` and
   ``compact`` (never resume/clear/fork), explicit timeout, fail-open command.
"""

from __future__ import annotations

import pytest

from claudlobby.composer import compose_settings_local
from claudlobby.config import BotConfig, FleetConfig, ScopeConfig
from claudlobby.paths import Paths


def _fleet(bot: BotConfig) -> FleetConfig:
    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        bots={bot.bot_id: bot},
        mission="Ship.",
    )


def _bot(**kw) -> BotConfig:
    base = dict(
        bot_id="alex",
        name="Alex",
        expertise=["software-engineering"],
        scope=ScopeConfig(org="acme", repos=["acme/widget"]),
    )
    base.update(kw)
    return BotConfig(**base)


@pytest.fixture
def paths(tmp_path) -> Paths:
    (tmp_path / "lib").mkdir()
    return Paths(root=tmp_path, fleet_dir=None)


def _session_start(settings: dict) -> list:
    return (settings.get("hooks") or {}).get("SessionStart", [])


class TestKnobDefaultOff:
    def test_no_stanza_composes_no_brief_hook(self, paths):
        bot = _bot()
        settings = compose_settings_local(bot, _fleet(bot), paths)
        for group in _session_start(settings):
            for hook in group.get("hooks", []):
                assert "brief" not in hook.get("command", "")

    def test_probe_never_runs_when_off(self, paths, monkeypatch):
        import claudlobby.composer as composer_mod

        calls = []
        monkeypatch.setattr(
            composer_mod,
            "_brief_cli_probe",
            lambda: calls.append(1) or (None, "stub"),
        )
        bot = _bot()
        compose_settings_local(bot, _fleet(bot), paths)
        assert calls == []


class TestKnobArmed:
    def _armed_settings(self, paths, monkeypatch):
        import claudlobby.composer as composer_mod

        monkeypatch.setattr(
            composer_mod,
            "_brief_cli_probe",
            lambda: ("/fake/bin/claudlobby", ""),
        )
        bot = _bot(brief_on_start=True)
        return compose_settings_local(bot, _fleet(bot), paths)

    def test_two_matcher_groups_startup_and_compact(self, paths, monkeypatch):
        settings = self._armed_settings(paths, monkeypatch)
        groups = _session_start(settings)
        matchers = {
            g.get("matcher")
            for g in groups
            if any("--boot" in h.get("command", "") for h in g.get("hooks", []))
        }
        assert matchers == {"startup", "compact"}

    def test_hook_command_is_fail_open_and_bot_scoped(self, paths, monkeypatch):
        settings = self._armed_settings(paths, monkeypatch)
        cmds = [
            h["command"]
            for g in _session_start(settings)
            for h in g.get("hooks", [])
            if "--boot" in h.get("command", "")
        ]
        assert cmds, "no boot-brief hook composed"
        for cmd in cmds:
            # C2: the EXECUTED path is the certified absolute exe, so the
            # probed artifact and the runtime artifact are one object
            assert cmd.startswith("/fake/bin/claudlobby brief --bot alex --boot")
            assert "||" in cmd  # fail-open: a door failure injects one line
            fallback = cmd.split("||", 1)[1]
            assert "unavailable" in fallback
            # the fallback text names the door for the READER (bare name)
            assert "claudlobby brief --bot alex" in fallback

    def test_explicit_timeout_composed(self, paths, monkeypatch):
        settings = self._armed_settings(paths, monkeypatch)
        for g in _session_start(settings):
            for h in g.get("hooks", []):
                if "--boot" in h.get("command", ""):
                    # platform default is 600s; the design budget is explicit
                    assert h.get("timeout") == 10

    def test_fleet_own_sessionstart_hooks_survive(self, paths, monkeypatch):
        import claudlobby.composer as composer_mod

        monkeypatch.setattr(
            composer_mod,
            "_brief_cli_probe",
            lambda: ("/fake/bin/claudlobby", ""),
        )
        bot = _bot(
            brief_on_start=True,
            hooks={
                "SessionStart": [
                    {"command": "echo mine", "matcher": "startup", "timeout": 5}
                ]
            },
        )
        settings = compose_settings_local(bot, _fleet(bot), paths)
        cmds = [
            h["command"] for g in _session_start(settings) for h in g.get("hooks", [])
        ]
        assert "echo mine" in cmds
        assert any("--boot" in c for c in cmds)


class TestComposeTimeGate:
    def test_probe_failure_refuses_to_arm(self, paths, monkeypatch):
        import claudlobby.composer as composer_mod

        monkeypatch.setattr(
            composer_mod,
            "_brief_cli_probe",
            lambda: (None, "claudlobby: error: invalid choice: 'brief'"),
        )
        bot = _bot(brief_on_start=True)
        with pytest.raises(ValueError) as e:
            compose_settings_local(bot, _fleet(bot), paths)
        msg = str(e.value)
        assert "brief" in msg
        assert "install" in msg  # names the merged-but-not-installed gap


class TestConfigParse:
    def test_brief_stanza_parses_strict_bool(self):
        from claudlobby.config import _parse_brief

        assert _parse_brief(None) is False
        assert _parse_brief({"on_start": True}) is True
        assert _parse_brief({"on_start": False}) is False

    def test_non_bool_arming_value_is_a_parse_error(self):
        # An ARMING knob must not arm on a typo: "on_start: tue" is a YAML
        # string, truthy under bool(), and would silently switch runtime
        # behavior on — the no-silent-switches failure exactly.
        from claudlobby.config import _parse_brief

        with pytest.raises(ValueError):
            _parse_brief({"on_start": "tue"})
        with pytest.raises(ValueError):
            _parse_brief("on")


class TestRealProbe:
    """The probe itself, unstubbed — cache bypassed via __wrapped__ so each
    leg probes its own PATH (the @functools.cache is process-scoped by
    design; these tests must not share one memoized verdict)."""

    def _probe_with_path(self, monkeypatch, path_value: str):
        import claudlobby.composer as composer_mod

        monkeypatch.setenv("PATH", path_value)
        return composer_mod._brief_cli_probe.__wrapped__()

    def test_missing_binary_refuses(self, monkeypatch, tmp_path):
        exe, why = self._probe_with_path(monkeypatch, str(tmp_path))
        assert exe is None
        assert "PATH" in why

    def test_stale_install_refuses_with_its_own_error(self, monkeypatch, tmp_path):
        fake = tmp_path / "claudlobby"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "brief" ]; then\n'
            '  echo "claudlobby: error: argument cmd: invalid choice: brief" >&2\n'
            "  exit 2\n"
            "fi\n"
            "exit 0\n"
        )
        fake.chmod(0o755)
        exe, why = self._probe_with_path(
            monkeypatch, f"{tmp_path}:/usr/bin:/bin"
        )
        assert exe is None
        assert "invalid choice" in why

    def test_current_install_certifies_the_resolved_exe(
        self, monkeypatch, tmp_path
    ):
        fake = tmp_path / "claudlobby"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "brief" ]; then echo "usage: ... --boot ..."; exit 0; fi\n'
            "exit 0\n"
        )
        fake.chmod(0o755)
        exe, why = self._probe_with_path(
            monkeypatch, f"{tmp_path}:/usr/bin:/bin"
        )
        assert exe == str(fake)
        assert why == ""
