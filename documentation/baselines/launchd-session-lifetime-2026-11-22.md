# Phase 0 proof A — launchd and a surviving tmux worker

On 2026-11-22, an isolated GUI LaunchAgent on macOS 26.1 restarted a killed observer in 2.108 seconds. The replacement found the same private tmux server and worker. On bootout, the observer’s SIGTERM handler shut down its server, and the probe observed all three processes gone in 0.064 seconds. The job remained absent throughout a further six-second observation window.

## Setup and observations

The source baseline was `c06c5693`. The temporary agent ran an independent Python 3.12 observer with tmux 3.6a, a unique launchd label, an absolute private socket, a single named session and a sleeping worker. The plist used RunAtLoad, KeepAlive with SuccessfulExit false, and a two-second ThrottleInterval. The plist omitted AbandonProcessGroup. The probe observed server survival after observer SIGKILL; it did not measure process-group membership. The observer created the session if absent, published its own PID plus the server and pane PIDs in an atomic readiness record, and handled SIGTERM by shutting down its own server. The probe polled at 50 ms intervals. Restart timing ran from immediately after sending SIGKILL to observing the new readiness record. Stop timing ran from issuing bootout to observing process and session absence. These elapsed times include polling delay; the run did not isolate the throttle’s contribution.

| Action | Observer PID | Server PID | Worker PID | Observation |
|---|---:|---:|---:|---|
| Bootstrap and read the live pane | 19718 | 19722 | 19723 | All three identities present |
| SIGKILL only the observer | 19728 | 19722 | 19723 | New observer published readiness and live pane query matched |
| Boot out the exact launchd label | absent | absent | absent | All three PIDs gone and the socket no longer served a session |

## Readiness decision and limits

Use application-owned readiness for the current launch generation. A caller should match the reported identities to the live server and worker before declaring local readiness. Production agent readiness additionally needs its existing native session confirmation. A launchd job becoming active alone cannot establish either condition.

This single run demonstrates observer replacement with a surviving worker and deliberate teardown. The private prototype used PID equality over a short interval. Phase 3 still needs generation-safe identity, agent-only and server-only failure cases, and assignment continuity with a real agent. The measured durations describe this run and establish no timeout policy.
