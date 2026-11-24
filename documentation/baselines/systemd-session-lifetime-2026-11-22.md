# Phase 0 proof B — systemd and a surviving tmux worker

On 2026-11-22, the Raspberry Pi’s systemd user manager restarted a killed observer in 2.257 seconds. The replacement observed the same private tmux server and sleeping worker. During the retry interval the unit reported `activating/auto-restart` with no main PID. A separate, explicitly non-retryable exit produced `failed/failed`.

## Setup

The Raspberry Pi ran Linux aarch64, systemd 252.39 and tmux 3.3a. Its one-minute load average just before the run was 10.25. One temporary Python observer ran in a uniquely named transient user unit created by `systemd-run --user`. The unit used `Type=simple`, `KillMode=process`, `Restart=on-failure` and `RestartSec=2`. Its private socket, tmux configuration (empty), readiness record and sleeping worker were isolated from the fleet.

The observer created a session when its socket had none. It atomically published its PID and the live server and pane PIDs. The probe polled at 50 ms intervals. Restart timing ran from requesting SIGKILL to observing the new readiness record and verifying PID continuity. Stop timing ran from requesting stop to observing process and session absence. These timings include command and polling overhead under the observed host load and configured restart delay. The probe compared those identities to the unit's MainPID and a separate tmux query. With KillMode=process the stop targeted the observer. The server survived observer SIGKILL, so this result cannot be generalized to other kill modes. On SIGTERM the observer stopped its server and exited successfully. For the terminal-failure control only, SIGUSR1 made it stop its server and exit 42, which the fixture declared in `RestartPreventExitStatus`. This control demonstrates a distinct manager state without exhausting a retry quota.

## Observations

| Action | Native evidence |
|---|---|
| Start | Active/running. Observer 3264840, server 3264898, worker 3264903. |
| Send SIGKILL to the unit's main process only | Activating/auto-restart, MainPID 0, Result=signal. |
| Wait for the replacement's readiness record | After 2.257 s, active/running with observer 3265481 and NRestarts=1. Server 3264898 and worker 3264903 still served the same pane. |
| Trigger the exit-42 control | Failed/failed, MainPID 0, Result=exit-code. |
| Start the unit again and issue an intentional stop | Within 0.056 s the new observer, server and worker PIDs were gone. Six seconds later the readiness record had not advanced and the socket served no session. The transient unit was unloaded (not-found, inactive/dead). |

As in [proof A](launchd-session-lifetime-2026-11-22.md), readiness requires observing the serving worker. On Linux the updater must also recognize the retry interval as a distinct native state and prove quiet separately before acting. These timings are single observations. Phase 3 still owns real-agent assignment continuity, agent-only and server-only failures, and generation-safe identity checks.

After SIGKILL, the server and worker continued running without the original observer. The intentional-stop row demonstrates cleanup by the observer’s handler. Manager-only cleanup was outside this experiment.
