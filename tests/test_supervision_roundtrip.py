"""Round-trip equivalence: one SupervisionSpec behind both unit renderers
(#1573, design doc §6.6,
`.superpowers/sdd/2026-09-20-boot-admission-pr-a-truth-and-adapter/` task 5).

`claudlobby/supervision.py` builds ONE `SupervisionSpec` per bot and hands it
to two pure renderers, `render_systemd_unit` and `render_launchd_plist`. This
module renders both formats for every bot in a fixture fleet, parses each
back into a spec (`parse_systemd_unit` / `parse_launchd_plist` below), and
asserts both equal the spec that built them. A fact one renderer learns that
the other does not is a failing test — that is the property this pins.

Two fields are format-specific BY DESIGN and can never round-trip through
text, so the two `spec_from_*` readers below fill them from the spec that
built the text (`fallback`) rather than parsing them — never inventing a
value the format cannot carry (design doc §6.6's idiom table):

  * `launchd_environment_extra` + `stdout_log` + `stderr_log` — launchd-only
    idiom. The systemd unit has no `EnvironmentVariables`/`StandardOutPath`/
    `StandardErrorPath` lines at all.
  * `stop_command` / `description` — the systemd unit carries these
    (`ExecStop`, `Description=`); the plist has no stop hook and no
    description field. `stop_post_command` (the `.tmux-env` cleanup) is in
    neither unit text any more: explicit supervisor teardown (`supervisor.sh`)
    owns it on both platforms, so both readers fill it from `fallback`, and
    its absence from the rendered text is asserted directly
    (`TestFormatSpecificAllowances`).

Generated commands now go through the stable managed-Git entry, so the
ExecStart line is a quoted argv (systemd's `"..."` syntax with `\\`, `\"`,
`%%` and `$$` escapes) and an `Environment=` assignment is quoted whole when
its value is not plain. `spec_from_systemd` decodes both.
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby.config import BotConfig, FleetConfig, TeamConfig
from tests.package_fixtures import git_package
from claudlobby.paths import Paths
from claudlobby.supervision import (
    RETIRED_IN_PR_B,
    SupervisionSpec,
    build_supervision_spec,
    render_launchd_plist,
    render_systemd_unit,
)

# Generic placeholder fleet — no real fleet/bot/host/person identifier
# (public repo). One manager ("lead"), two workers ("w1", "w2"); mirrors
# tests/test_boot_policy_conformance.py's fixture.
_BOT_IDS = ["lead", "w1", "w2"]


def _fixture_fleet() -> FleetConfig:
    return FleetConfig(
        name="fixture-fleet",
        service_prefix="com.fixture",
        manager="lead",
        bots={
            "lead": BotConfig(bot_id="lead", name="lead", expertise=["orchestration"]),
            "w1": BotConfig(bot_id="w1", name="w1", expertise=["eng"]),
            "w2": BotConfig(bot_id="w2", name="w2", expertise=["eng"]),
        },
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["w1", "w2"])},
    )


def _fixture_paths(tmp_path) -> Paths:
    root = tmp_path / "claudlobby"
    for bot_id in _BOT_IDS:
        (root / "runtime" / "bots" / bot_id).mkdir(parents=True)
    (root / "lib").mkdir()
    return Paths(root=root, fleet_dir=root, package=git_package(root))


# ----------------------------------------------------------------------
# parse_systemd_unit / parse_launchd_plist
# ----------------------------------------------------------------------


def parse_systemd_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """A line parser (F6 — never `configparser`, which collapses repeated
    keys): `[Section]` headers, `Key=Value` lines, `#` comments skipped,
    repeated keys collected into a list in order (systemd itself treats
    repeated `Environment=` this way).

    `RETIRED_IN_PR_B` names are dropped rather than collected — the boot
    stagger `ExecStartPre` is the one line this task allows a systemd unit
    to carry that launchd has no equivalent for, and PR B retires it
    outright, so the round trip does not need to look inside it.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        header = re.match(r"^\[(\w+)\]$", line)
        if header:
            section = header.group(1)
            sections.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in RETIRED_IN_PR_B:
            continue
        sections[section].setdefault(key, []).append(value)
    return sections


def parse_launchd_plist(text: str) -> dict:
    return plistlib.loads(text.encode())


# ----------------------------------------------------------------------
# spec_from_systemd / spec_from_launchd
# ----------------------------------------------------------------------

_EXEC_STOP_RE = re.compile(r"^/bin/sh -c '(.*)'$")
_QUOTED_WORD_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_LABEL_FROM_STOP_COMMAND_RE = re.compile(r"tmux -L (\S+) kill-server")


def _systemd_unquote(word: str) -> str:
    """Inverse of installation.unit_systemd_command's per-word escaping."""
    return re.sub(
        r'\\(.)|%%|\$\$',
        lambda m: m.group(1) if m.group(1) is not None else m.group(0)[0],
        word,
    )


def _systemd_argv(command: str) -> list[str]:
    """A fully quoted ExecStart line back into its argv."""
    words = _QUOTED_WORD_RE.findall(command)
    assert " ".join(f'"{w}"' for w in words) == command, command
    return [_systemd_unquote(w) for w in words]


def _systemd_environment_assignment(line: str) -> tuple[str, str]:
    """One `Environment=` value: bare `K=V`, or `"K=V"` quoted whole with
    `%` doubled (unit_systemd_environment)."""
    if line.startswith('"'):
        match = _QUOTED_WORD_RE.fullmatch(line)
        assert match, line
        line = re.sub(r"\\(.)", r"\1", match.group(1))
    key, value = line.split("=", 1)
    return key, value.replace("%%", "%")


def spec_from_systemd(parsed: dict, fallback: SupervisionSpec) -> SupervisionSpec:
    """Rebuilds a spec from a parsed systemd unit. `launchd_environment_extra`,
    `stdout_log` and `stderr_log` are launchd-only idiom the unit carries no
    lines for at all, and `stop_post_command` is owned by explicit supervisor
    teardown rather than any unit directive, so those come from `fallback`
    (the spec that built the text) rather than being parsed."""
    unit = parsed["Unit"]
    service = parsed["Service"]
    (description,) = unit["Description"]
    (working_dir_raw,) = service["WorkingDirectory"]
    (exec_start,) = service["ExecStart"]
    (exec_stop,) = service["ExecStop"]
    assert "ExecStopPost" not in service
    (restart,) = service["Restart"]

    launcher_str, *launcher_args = _systemd_argv(exec_start)
    stop_command = _EXEC_STOP_RE.match(exec_stop).group(1)
    stop_post_command = fallback.stop_post_command
    label = _LABEL_FROM_STOP_COMMAND_RE.search(stop_command).group(1)

    environment: dict[str, str] = {}
    for line in service.get("Environment", []):
        key, value = _systemd_environment_assignment(line)
        environment[key] = value

    working_dir = Path(working_dir_raw)
    return SupervisionSpec(
        label=label,
        description=description,
        bot_dir=working_dir,
        launcher=Path(launcher_str),
        launcher_args=tuple(launcher_args),
        working_dir=working_dir,
        environment=environment,
        launchd_environment_extra=fallback.launchd_environment_extra,
        stop_command=stop_command,
        stop_post_command=stop_post_command,
        stdout_log=fallback.stdout_log,
        stderr_log=fallback.stderr_log,
        restart_on_failure=(restart == "on-failure"),
    )


def spec_from_launchd(parsed: dict, fallback: SupervisionSpec) -> SupervisionSpec:
    """Rebuilds a spec from a parsed launchd plist. `description`,
    `stop_command` and `stop_post_command` come from `fallback` — the plist
    has no description field and no stop hook at all (PR B's adapter carries
    the teardown on launchd; see supervision.py's module docstring)."""
    program_arguments = list(parsed["ProgramArguments"])
    launcher_str, *launcher_args = program_arguments
    env_vars = dict(parsed["EnvironmentVariables"])
    extra_keys = set(fallback.launchd_environment_extra)
    environment = {k: v for k, v in env_vars.items() if k not in extra_keys}
    launchd_environment_extra = {k: v for k, v in env_vars.items() if k in extra_keys}
    successful_exit = parsed["KeepAlive"]["SuccessfulExit"]

    working_dir = Path(parsed["WorkingDirectory"])
    return SupervisionSpec(
        label=parsed["Label"],
        description=fallback.description,
        bot_dir=working_dir,
        launcher=Path(launcher_str),
        launcher_args=tuple(launcher_args),
        working_dir=working_dir,
        environment=environment,
        launchd_environment_extra=launchd_environment_extra,
        stop_command=fallback.stop_command,
        stop_post_command=fallback.stop_post_command,
        stdout_log=Path(parsed["StandardOutPath"]),
        stderr_log=Path(parsed["StandardErrorPath"]),
        restart_on_failure=(successful_exit is False),
    )


# ----------------------------------------------------------------------
# The round trip
# ----------------------------------------------------------------------


class TestSupervisionRoundtrip:
    def test_selected_release_context_survives_both_renderers(self, tmp_path):
        """The managed Git installation's context, not an ambient interpreter's,
        reaches both unit formats: the stable installation CLI is the launcher,
        the committed native entry follows it, and no release or artifact
        selector is carried."""
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        installation = paths.package.installation
        assert installation is not None
        spec = build_supervision_spec(fleet.bots["lead"], fleet, paths)
        expected = {
            "CLAUDLOBBY_ROOT": str(paths.root),
            "FLEET_ROOT": str(paths.fleet_config_dir),
            "CLAUDLOBBY_NATIVE_DIR": str(paths.package.runtime_native),
            "CLAUDLOBBY_LIBRARY_DIR": str(paths.package.runtime_library),
            "CLAUDLOBBY_CLI": str(installation.cli),
            "CLAUDLOBBY_INSTALLATION": str(installation.directory),
        }
        assert expected.items() <= spec.environment.items()
        assert not {"CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"} & spec.environment.keys()
        bot_dir = paths.bot_runtime("lead")
        assert spec.launcher == installation.cli
        assert spec.launcher_args == (
            "--root", str(paths.root), "_native", "--unit", spec.label,
            "--phase", "bots", "--mode", "exec", "--",
            str(paths.package.runtime_native / "start-bot.sh"), str(bot_dir),
        )
        assert spec_from_systemd(parse_systemd_unit(render_systemd_unit(spec)), spec) == spec
        assert spec_from_launchd(parse_launchd_plist(render_launchd_plist(spec)), spec) == spec

    @pytest.mark.parametrize("bot_id", _BOT_IDS)
    def test_systemd_round_trips(self, tmp_path, bot_id):
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots[bot_id], fleet, paths)

        unit = render_systemd_unit(spec)
        rebuilt = spec_from_systemd(parse_systemd_unit(unit), spec)

        assert rebuilt == spec

    @pytest.mark.parametrize("bot_id", _BOT_IDS)
    def test_launchd_round_trips(self, tmp_path, bot_id):
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots[bot_id], fleet, paths)

        plist = render_launchd_plist(spec)
        rebuilt = spec_from_launchd(parse_launchd_plist(plist), spec)

        assert rebuilt == spec

    def test_systemd_round_trips_with_boot_stagger(self, tmp_path):
        """RETIRED_IN_PR_B: the ExecStartPre stagger is the one systemd-only
        line the round trip allows — this proves the allowance actually
        works (the parser drops the line; the rebuilt spec still matches),
        not just that it is declared."""
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots["lead"], fleet, paths)

        unit = render_systemd_unit(spec, boot_delay_s=5)
        assert "ExecStartPre=/bin/sleep 5" in unit

        parsed = parse_systemd_unit(unit)
        assert "ExecStartPre" not in parsed["Service"]
        assert spec_from_systemd(parsed, spec) == spec


class TestFormatSpecificAllowances:
    """The idiom table's one allowed divergence (design doc §6.6): the plist
    cannot carry a stop hook at all. Pinned as a positive (systemd has it)
    and a negative (launchd does not), rather than trusting the round trip's
    fallback-filled equality alone to notice a leak."""

    @pytest.mark.parametrize("bot_id", _BOT_IDS)
    def test_stop_hook_in_systemd_absent_from_plist(self, tmp_path, bot_id):
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots[bot_id], fleet, paths)

        unit = render_systemd_unit(spec)
        plist = render_launchd_plist(spec)

        assert f"ExecStop=/bin/sh -c '{spec.stop_command}'" in unit
        # The .tmux-env cleanup is no unit directive on either platform: a raw
        # ExecStopPost would also run after a refused ExecStart, so explicit
        # supervisor teardown owns it. The spec still carries the command.
        directives = [
            ln for ln in unit.splitlines() if ln.strip() and not ln.startswith("#")
        ]
        assert not any(ln.startswith("ExecStopPost=") for ln in directives)
        assert not any(".tmux-env" in ln for ln in directives)
        assert spec.stop_post_command == f"rm -f {spec.bot_dir}/.tmux-env"
        assert "Explicit supervisor teardown" in unit
        # No stop hook of any shape reaches the plist: neither the tmux
        # kill-server invocation nor the .tmux-env cleanup appears in it.
        assert "kill-server" not in plist
        assert ".tmux-env" not in plist

    @pytest.mark.parametrize("bot_id", _BOT_IDS)
    def test_path_and_home_only_in_launchd(self, tmp_path, bot_id):
        """Generalizes test_composer.py::test_launchd_systemd_path_parity's
        intent to the per-bot supervision units: PATH and HOME are the
        LaunchAgent idiom (a LaunchAgent does not inherit the shell's PATH);
        the systemd unit needs neither and carries neither."""
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots[bot_id], fleet, paths)

        unit = render_systemd_unit(spec)
        plist = render_launchd_plist(spec)
        parsed_plist = parse_launchd_plist(plist)

        assert parsed_plist["EnvironmentVariables"]["PATH"] == (
            spec.launchd_environment_extra["PATH"]
        )
        assert parsed_plist["EnvironmentVariables"]["HOME"] == (
            spec.launchd_environment_extra["HOME"]
        )
        unit_lines = unit.splitlines()
        assert not any(ln.startswith("Environment=PATH=") for ln in unit_lines)
        assert not any(ln.startswith("Environment=HOME=") for ln in unit_lines)


class TestLabelMatchesBotService:
    """`svc_unit_name` and the installers (claudlobby/_runtime_scripts/supervisor.sh,
    lib/install-bot*.sh) rely on one invariant: the label a rendered unit
    answers to is the SAME string bot.conf's BOT_SERVICE= names. Before this
    test nothing pinned the two together -- `build_supervision_spec`'s
    `label = f"{fleet.service_prefix}.{bot.bot_id}"` and
    `compose_bot_conf`'s `bot_service = f"{fleet.service_prefix}.{bot.bot_id}"`
    are two independent f-strings, and a mutant that changes either
    separator (mutant `spec-label-drifts`, PR A final wave item 6) survives
    every other test in this file, since those all build a spec directly and
    never cross-check it against composer.py's own bot.conf output. This
    goes through the REAL composer entry points -- `compose_bot_conf`,
    `compose_systemd_unit`, `compose_launchd_plist` -- not
    `build_supervision_spec` plus a renderer, so a drift between the two
    call sites cannot hide behind a fixture that only ever exercises one."""

    @pytest.mark.parametrize("bot_id", _BOT_IDS)
    def test_label_matches_bot_conf_bot_service(self, tmp_path, bot_id):
        import claudlobby.composer as composer_module

        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        bot = fleet.bots[bot_id]

        conf = composer_module.compose_bot_conf(bot, fleet, paths)
        unit = composer_module.compose_systemd_unit(bot, fleet, paths)
        plist = composer_module.compose_launchd_plist(bot, fleet, paths)

        bot_service_lines = [
            line for line in conf.splitlines() if line.startswith("BOT_SERVICE=")
        ]
        assert len(bot_service_lines) == 1
        # `_shq` (shlex.quote) leaves an alnum-plus-dot identifier like
        # "com.fixture.lead" unquoted, so a plain split recovers it verbatim.
        bot_service = bot_service_lines[0].split("=", 1)[1]
        assert bot_service == f"{fleet.service_prefix}.{bot.bot_id}"

        parsed_plist = parse_launchd_plist(plist)
        assert parsed_plist["Label"] == bot_service

        exec_stop_lines = [
            line for line in unit.splitlines() if line.startswith("ExecStop=")
        ]
        assert len(exec_stop_lines) == 1
        assert f"tmux -L {bot_service} kill-server" in exec_stop_lines[0]


class TestRoundtripCatchesADivergence:
    """The probe: a fact one renderer learns that the other does not must be
    a failing test, which is the whole point of pinning the boundary with a
    round trip instead of two independent snapshot tests."""

    def test_a_dropped_environment_key_fails_the_round_trip(self, tmp_path, monkeypatch):
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        base_spec = build_supervision_spec(fleet.bots["lead"], fleet, paths)
        spec = replace(
            base_spec,
            environment={**base_spec.environment, "PROBE_KEY": "probe-value"},
        )

        # First, show the healthy case: both renderers learn the new key.
        systemd_rebuilt = spec_from_systemd(parse_systemd_unit(render_systemd_unit(spec)), spec)
        launchd_rebuilt = spec_from_launchd(parse_launchd_plist(render_launchd_plist(spec)), spec)
        assert systemd_rebuilt.environment["PROBE_KEY"] == "probe-value"
        assert launchd_rebuilt.environment["PROBE_KEY"] == "probe-value"
        assert systemd_rebuilt == spec
        assert launchd_rebuilt == spec

        # Now simulate a launchd renderer that never learned about it — the
        # exact shape of the drift this test suite exists to catch.
        import claudlobby.supervision as supervision_module

        def _renderer_that_forgot_the_key(forgotten_spec: SupervisionSpec) -> str:
            stale = replace(
                forgotten_spec,
                environment={
                    k: v for k, v in forgotten_spec.environment.items() if k != "PROBE_KEY"
                },
            )
            return render_launchd_plist(stale)

        monkeypatch.setattr(
            supervision_module, "render_launchd_plist", _renderer_that_forgot_the_key
        )

        broken_plist = supervision_module.render_launchd_plist(spec)
        broken_rebuilt = spec_from_launchd(parse_launchd_plist(broken_plist), spec)

        # Shown red: the round trip does not silently agree with the spec
        # that built it once one side forgets a fact the other carries.
        assert broken_rebuilt != spec
        assert broken_rebuilt.environment != spec.environment
        with pytest.raises(AssertionError):
            assert broken_rebuilt == spec

    def test_an_added_environment_key_fails_the_round_trip(self, tmp_path, monkeypatch):
        """Symmetric to the dropped-key case above (parked at task 5): a
        renderer that EMITS a key the spec never carried is exactly as much
        a divergence as one that drops a key the spec does carry, and the
        round trip must catch both the same way -- covering only the
        drop direction would leave an added fact free to leak into one
        format and not the other."""
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)
        spec = build_supervision_spec(fleet.bots["lead"], fleet, paths)

        import claudlobby.supervision as supervision_module

        def _renderer_that_invented_a_key(a_spec: SupervisionSpec) -> str:
            embellished = replace(
                a_spec,
                environment={**a_spec.environment, "INVENTED_KEY": "not-in-spec"},
            )
            return render_launchd_plist(embellished)

        monkeypatch.setattr(
            supervision_module, "render_launchd_plist", _renderer_that_invented_a_key
        )

        broken_plist = supervision_module.render_launchd_plist(spec)
        broken_rebuilt = spec_from_launchd(parse_launchd_plist(broken_plist), spec)

        # Shown red the same way: the round trip does not silently agree with
        # the spec that built it once one side learns a fact the other never had.
        assert broken_rebuilt != spec
        assert broken_rebuilt.environment != spec.environment
        assert "INVENTED_KEY" in broken_rebuilt.environment
        with pytest.raises(AssertionError):
            assert broken_rebuilt == spec


# ----------------------------------------------------------------------
# _TMUX_TMPDIR parity (parked at task 5)
# ----------------------------------------------------------------------


def test_tmux_tmpdir_constant_matches_composer():
    """claudlobby/supervision.py's module docstring: `_TMUX_TMPDIR` is
    duplicated in claudlobby/composer.py rather than imported, to keep this
    module a leaf composer.py depends on and never the reverse (importing
    back from composer.py would be circular, since composer.py imports the
    renderers from here). A one-line pin so the two copies -- nothing else
    enforces this -- can never quietly drift apart."""
    import claudlobby.composer as composer_module
    import claudlobby.supervision as supervision_module

    assert composer_module._TMUX_TMPDIR == supervision_module._TMUX_TMPDIR
