"""SupervisionSpec -- one spec behind both unit renderers (#1573).

Design: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
section 6.6. Before this module, `compose_systemd_unit` and
`compose_launchd_plist` (claudlobby/composer.py) each read `bot`/`fleet`/
`paths` directly and built their own text -- two independent places a future
field could land in one format and not the other, with nothing to catch the
drift.

`build_supervision_spec` is the ONE place `bot`/`fleet`/`paths` get read.
`render_systemd_unit` and `render_launchd_plist` are pure functions of a
`SupervisionSpec` and nothing else -- a renderer that reaches around the spec
for `bot`, `fleet` or `paths` directly defeats the boundary this module
exists to draw. `tests/test_supervision_roundtrip.py` pins it: it parses
each rendered format back into a spec and asserts both equal the spec that
built them, so a fact one renderer learns and the other does not is a
failing test.

The idiom table (design doc section 6.6) is the whole allowed difference
between the two formats:

| spec property | systemd idiom | launchd idiom |
|---|---|---|
| launcher exits, session survives | `Type=simple` + `RemainAfterExit=yes` + `KillMode=process` | `RunAtLoad` with the launcher as `ProgramArguments`; `KeepAlive` keyed on `SuccessfulExit=false` |
| restart on launcher failure | `Restart=on-failure`, `RestartSec=5` | `KeepAlive` `SuccessfulExit=false` |
| environment the launcher needs | `Environment=` lines (`CLAUDLOBBY_ROOT`, `TMUX_TMPDIR`) | `EnvironmentVariables` dict with the same two keys plus `PATH` and `HOME`, which a LaunchAgent does not inherit |
| stop tears the session down | `ExecStop` kills the bot's tmux server; `ExecStopPost` removes `.tmux-env` | launchd has no stop hook -- a plist cannot carry this. PR B's adapter (`svc_disenroll`) performs the same teardown on launchd |

This task (PR A task 5) does not add the adapter, so the round-trip test
fills `stop_command`/`stop_post_command` on the launchd side from the built
spec directly rather than from parsed plist text -- there is no plist text
to parse them from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import BotConfig, FleetConfig
from .paths import Paths

# Pinned fleet-wide tmux tmpdir. Mirrors claudlobby/composer.py's own
# `_TMUX_TMPDIR` (used there for bot.conf, unrelated to this module).
# Duplicated rather than imported so this module stays a leaf that composer.py
# depends on, never the reverse -- importing back from composer.py would be
# circular, since composer.py imports the renderers from here.
_TMUX_TMPDIR = "/tmp"

# The launchd EnvironmentVariables PATH. A LaunchAgent does not inherit the
# user's shell PATH, so composed bots pin one explicitly. Historically a
# literal inside compose_launchd_plist; unchanged here, just named.
_LAUNCHD_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"

# The one systemd-only line the round-trip test is allowed to skip without it
# counting as a divergence: the boot-stagger ExecStartPre, which launchd has
# no equivalent for and which PR B retires outright. Named so a reader who
# hits it in the parser knows it is a deliberate, temporary allowance and not
# a gap in the boundary.
RETIRED_IN_PR_B = ("ExecStartPre",)


@dataclass(frozen=True)
class SupervisionSpec:
    """Everything either unit renderer needs, and the only thing either may
    read from.

    `frozen=True` makes an instance immutable (no field can be reassigned)
    but NOT hashable: `environment` and `launchd_environment_extra` are
    `dict` fields, and a dataclass with an unhashable field raises on
    `hash()` even when frozen -- Python derives `__hash__` from the fields'
    own hashes, and `dict` has none. Nothing hashes a `SupervisionSpec`
    today; a caller that needs one in a set or as a dict key will need its
    own hashable projection rather than relying on this dataclass.
    """

    label: str
    description: str
    bot_dir: Path
    launcher: Path
    launcher_args: tuple[str, ...]
    working_dir: Path
    environment: dict[str, str]
    launchd_environment_extra: dict[str, str]
    stop_command: str
    stop_post_command: str
    stdout_log: Path
    stderr_log: Path
    restart_on_failure: bool = True


def build_supervision_spec(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> SupervisionSpec:
    """The one place `bot`/`fleet`/`paths` get read to build a supervision spec."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    label = f"{fleet.service_prefix}.{bot.bot_id}"
    log_dir = paths.lib / "logs"
    return SupervisionSpec(
        label=label,
        description=f"claudlobby bot: {bot.name} ({fleet.name})",
        bot_dir=bot_dir,
        launcher=paths.lib / "start-bot.sh",
        launcher_args=(str(bot_dir),),
        working_dir=bot_dir,
        environment={
            "CLAUDLOBBY_ROOT": str(paths.root),
            "TMUX_TMPDIR": _TMUX_TMPDIR,
        },
        launchd_environment_extra={
            "PATH": _LAUNCHD_PATH,
            "HOME": str(Path.home()),
        },
        stop_command=f"tmux -L {label} kill-server 2>/dev/null || true",
        stop_post_command=f"rm -f {bot_dir}/.tmux-env",
        stdout_log=log_dir / f"{bot.bot_id}.out.log",
        stderr_log=log_dir / f"{bot.bot_id}.err.log",
        restart_on_failure=True,
    )


def render_systemd_unit(spec: SupervisionSpec, *, boot_delay_s: int = 0) -> str:
    """Pure function of `spec`. Text is byte-identical to the pre-#1573
    `compose_systemd_unit` output for the same bot/fleet/paths."""
    stagger = f"\nExecStartPre=/bin/sleep {boot_delay_s}" if boot_delay_s > 0 else ""
    launcher_cmd = " ".join([str(spec.launcher), *spec.launcher_args])
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in spec.environment.items())
    restart_directive = "on-failure" if spec.restart_on_failure else "no"
    return f"""# Generated by claudlobby — do not hand-edit.
[Unit]
Description={spec.description}
After=network-online.target

[Service]
Type=simple
# start-bot.sh exits 0 after spawning tmux. Without these two, the default
# control-group cleanup kills the tmux server we just started. KillMode=process
# limits the kill to the main process; RemainAfterExit=yes keeps the unit
# "active" while tmux runs underneath.
#
# LOAD-BEARING BEYOND CLEANUP: Type=simple + a spawner ExecStart that exits +
# RemainAfterExit=yes is what makes SubState a boot-progress signal, and
# service_is_starting (lib-common.sh) reads it as one:
#   activating      ExecStartPre — the boot stagger sleep
#   active/running  start-bot.sh executing; tmux session not up yet
#   active/exited   steady state — spawner done, tmux running underneath
# If ExecStart ever execs into a foreground process, or RemainAfterExit is
# dropped, active/running becomes the STEADY state and keepalive's dead-session
# watchdog silently stops restarting anything — a failure shaped exactly like a
# healthy fleet. tests/test_composer.py asserts this triple; do not relax it
# without reading service_is_starting first.
RemainAfterExit=yes
KillMode=process
WorkingDirectory={spec.working_dir}{stagger}
ExecStart={launcher_cmd}
# kill-server (not kill-session): this bot owns its tmux server. kill-server
# tears the whole server down deterministically; kill-session leaves the emptied
# server orphaned unless tmux's exit-empty default reaps it.
ExecStop=/bin/sh -c '{spec.stop_command}'
ExecStopPost=/bin/{spec.stop_post_command}
# Restart= here only fires on non-zero exit of start-bot.sh — i.e., a config
# failure before tmux ever spawned. Tmux dying after we've gone "active" is
# detected by lib/keepalive.sh, NOT by systemd, because exit 0 + RemainAfterExit
# leaves the unit looking healthy regardless of what tmux is doing.
Restart={restart_directive}
RestartSec=5
{env_lines}

[Install]
WantedBy=default.target
"""


# NOTE: launchd has no ExecStartPre equivalent for boot stagger.
# On macOS, fleet boot contention is less of an issue (Mac Mini has
# more cores/RAM than Pi). If needed, stagger can be added via a
# BOOT_DELAY env var honored by start-bot.sh itself.
def render_launchd_plist(spec: SupervisionSpec) -> str:
    """Pure function of `spec`. Text is byte-identical to the pre-#1573
    `compose_launchd_plist` output for the same bot/fleet/paths."""
    program_arguments = "\n".join(
        f"        <string>{arg}</string>" for arg in (str(spec.launcher), *spec.launcher_args)
    )
    successful_exit = "false" if spec.restart_on_failure else "true"
    env_vars = {**spec.environment, **spec.launchd_environment_extra}
    env_lines = "\n".join(
        f"        <key>{k}</key><string>{v}</string>" for k, v in env_vars.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by claudlobby — do not hand-edit. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{spec.label}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments}
    </array>
    <key>WorkingDirectory</key><string>{spec.working_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><{successful_exit}/>
    </dict>
    <key>StandardOutPath</key><string>{spec.stdout_log}</string>
    <key>StandardErrorPath</key><string>{spec.stderr_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_lines}
    </dict>
</dict>
</plist>
"""
