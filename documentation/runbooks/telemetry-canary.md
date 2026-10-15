# Telemetry intake canary (#2145 P2-a1)

Status: **seven-day floor-host gate pending**. Local receiver/sink evidence proves the
code path only. It does not replace C6b or authorize production activation.
No model, credentials, Telegram messages or native services are needed for local HTTP checks.

## Independent root preparation (operator, pending)

Use a candidate sealed release and a fresh root, distinct fleet/bot labels, a private
`fleet.accounts.default` account, and distinct `fleet.service_prefix` and
`host.unit_prefix`. Keep the override outside the source tree. Park every unrelated host
job, including `claude-update`, `orphan-browser-reaper` and `plane-view` (production :8899).
The host override is host-scoped; `fleet.yaml` cannot park host jobs.

```bash
# Set these to the independent canary paths and the candidate sealed CLI/release.
CANARY_ROOT=/absolute/path/to/independent-canary
CANARY_CLI=/absolute/path/to/candidate/bin/claudlobby
CANARY_RELEASE=RELEASE_ID
export CLAUDLOBBY_HOST_SYSTEM_YAML="$CANARY_ROOT/host-system.yaml"
"$CANARY_CLI" --root "$CANARY_ROOT" host setup
# Author local/canary/fleet.yaml: one RC/Telegram bot, telemetry: {enabled: true, content: metadata}.
# Author host-system.yaml: distinct unit_prefix, plane-otel.enroll: true, unrelated jobs parked.
"$CANARY_CLI" --root "$CANARY_ROOT" config validate
"$CANARY_CLI" --root "$CANARY_ROOT" config plan --release "$CANARY_RELEASE"
"$CANARY_CLI" --root "$CANARY_ROOT" config diff PLAN_ID
```

Before activation, inspect `state/config-plans/PLAN_ID/plan.json` `.effects.units[]`
for exact unit names. Verify seven telemetry exports, the loopback endpoint and metadata
resource attribute, `UMask=0077` / `Umask=63`, no unintended units, no `telemetry-intake`
warning. Record the host's fleet RSS/CPU baseline and the Pi 5 budget before arming.
`config plan`, `config diff` and `host activate` take no `--fleet`.

```bash
# Operator action after the isolated plan is reviewed; not run by local validation.
"$CANARY_CLI" --root "$CANARY_ROOT" host activate PLAN_ID --install-directory UNIT_DIRECTORY
"$CANARY_CLI" --root "$CANARY_ROOT" plane doctor --json
"$CANARY_CLI" --root "$CANARY_ROOT" --fleet canary host doctor
```

## Timed observation (all pending)

- Day 1: RC ready after restart; an operator's inbound Telegram message gets a delivered reply.
  Logs and metrics grow; directory 0700 and files 0600; no prompt/account identity attributes
  in metadata logs; `otel sink` and enrollment/listening rungs green; no refusal/unwritten counts.
- Hourly for seven days: capture intake `ps -o rss=,%cpu= -p PID`, the host's
  `FLEET_RSS_MB` series, `du -sk "$CANARY_ROOT/state/otel"`, stats (p50/p95/max time to
  HTTP 200, accepted/refused/unwritten), and summarizer-child session ids.
- Compare per-session `tool_result` counts with distinct transcript `tool_use` ids.
  Compare token counters by type with main-session transcripts and bot-day usage including
  sidechains; compare `api_request` count with turns. Explain every ratio beyond window tolerance.
- Day 7: repeat delivered-reply check. Verify fleet RSS/CPU delta within the agreed floor-host
  budget, p95 far below the measured exporter timeout, raw size ≤1,920 MiB and no stale files,
  no refusals/unwritten bodies, and no `OTEL_*` in a hook subprocess.

A missed criterion opens a defect investigation. Record the exact release, host, times,
commands and measured results in the parent epic run log. P2-a2 remains held until this
week passes; do not claim timed gates from local HTTP evidence.
