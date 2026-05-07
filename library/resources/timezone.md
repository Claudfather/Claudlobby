### Timezone

The host system clock may run in UTC. Always check the human's timezone from the bot's `TZ` environment variable (set in fleet.yaml `env:`), and convert all times before presenting.

When displaying times: use the human's local format (e.g., "2:30 PM ET" not "18:30 UTC"). When computing "today", "tomorrow", or "this week", run `TZ='$TZ' date` to anchor to the human's timezone, not the system clock.
