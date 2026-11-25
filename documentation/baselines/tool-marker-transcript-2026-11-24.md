# Proof C — tool-call marker and transcript freshness

Retain the marker to preserve the observed macOS freshness behavior. On one isolated macOS canary, transcript freshness was true while the marker was stale or absent for a combined 51 minutes 25 seconds. Other busy checks and the worker’s actual activity during those intervals were outside this comparison. The largest absolute difference between the two modification times at an observed marker touch was 6.678 seconds; the marker was newer at that event.

| Window | Observed hours | Prompts submitted | Marker changes | Largest absolute gap at a change | Transcript alone fresh |
|---|---:|---:|---:|---:|---:|
| First sampler | 15.439 | 8 | 32 | 6.678 s | 1,793.833 s |
| Replacement sampler | 9.379 | 5 | 20 | 5.653 s | 1,291.602 s |

The canary ran Claude Code 2.1.330 on runtime commit `8d3a79aa`. Prompts requested one Bash and one Read call approximately every two hours. Each sampler targeted a 50 ms interval and recorded changes in the marker and the exact transcript named in the native-session receipt. Freshness follows the runtime's inclusive 180-second window with whole-second timestamps. Durations use successive observed transitions through the last saved heartbeat of each run. No sampled interval had a fresh marker and stale transcript. Transcript-only freshness includes startup and writes outside tool calls.

The first sampler exited unexpectedly. The second window ended with the host reboot. Both final heartbeats identified the same confirmed native session. Combined coverage was 24 hours 49 minutes across the two windows, excluding the gap. The longest sampling interval was 3.99 seconds, so closely spaced changes could coalesce. Linux and long-running calls were outside this experiment. This workload demonstrated a behavior difference; it cannot establish an upper bound on transcript lag.
