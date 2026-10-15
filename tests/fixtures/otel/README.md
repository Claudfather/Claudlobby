# Native OTLP fixture provenance

These are request bodies captured from the installed Claude Code 2.1.301 exporter
on 2026-10-15, refreshing the C6a transport shape measured in the epic run log
on 2026-10-05 (2.1.289 / floor-host 2.1.281). No fixture was read from producer source.
The independent native hook-dispatch experiment used private HOME/config/state, a
fake API key and a loopback Messages SSE stub returning a fixed response.
The logs include an actual successful Bash true tool_result and hook dispatch. It made
no provider/model call. Actual HTTP POSTs to /v1/logs and /v1/metrics were captured.
The original private evidence is under `/private/tmp/2145-native-dispatch-evidence/`:
`tool-arm/captures/005-logs.body` and `captures/002-metrics.body`, with decoded JSON companions. Capture is owned
by the parent epic's native-dispatch validation, source clauDNA 3c857c3.

The envelopes, arrays, attribute keys, OTLP value types and metric/event names are
retained. Session/account/message ids, email, paths and timestamps are replaced;
prompt/response text is redacted. These transport fixtures do not prove the
seven-day floor-host canary or provider billing/usage accuracy.
