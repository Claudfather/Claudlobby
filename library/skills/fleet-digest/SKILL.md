---
name: fleet-digest
description: "Assemble the monitor's pass input. Joins the transcript-digest log with vitals, utilization, and report-back rollups into one bounded, coverage-honest summary. Reads only pre-aggregated sources — never raw transcripts."
argument-hint: "[days] [fleet]"
tool_grants:
  - "Bash(jq *)"
  - "Bash(python3 *)"
---

# Fleet Digest

Build the input for a monitoring pass. You **assemble and bound**; you do not
reason and you do not emit findings — that is `/fleet-observe`. Keeping those
apart is what stops thousands of raw rows reaching an Opus reasoning pass.

Contract: the `fleet-monitoring` protocol. Read its evidence-contract and
token-discipline sections before changing anything here.

**Arguments:** `$1` = window in days (default `7`). `$2` = fleet filter
(default: all fleets).

## Step 1 — Locate the log

```bash
DIGEST_DIR="${SESSION_DIGEST_LOG_DIR:-$CLAUDLOBBY_ROOT/state/transcript-digests}"
```

If the directory does not exist, **stop and say so**. That is a complete,
correct result: the digester is dormant by default (`SESSION_DIGEST_ENABLED=1`
arms a fleet), so an absent log means the instrument is off, not that the estate
was quiet. Do not fall back to any other source and do not infer health.

## Step 2 — Select the window

```bash
DAYS="${1:-7}"
for i in $(seq 0 $((DAYS - 1))); do
  date -d "-$i day" +%Y-%m-%d 2>/dev/null || date -v-"$i"d +%Y-%m-%d
done | while read -r d; do
  f="$DIGEST_DIR/transcript-digest-$d.jsonl"; [ -f "$f" ] && cat "$f"
done > /tmp/window.jsonl
```

Record **which dates had a file and which did not** — a missing date is a
coverage fact, not something to smooth over.

## Step 3 — Coverage first, before any aggregation

Coverage is the part most likely to be quietly dropped, so compute it first and
carry it through:

```bash
jq -s '{
  rows: length,
  fleets: (map(.fleet) | unique),
  bots:   (map(.bot)   | unique | length),
  by_status: (group_by(.status) | map({(.[0].status): length}) | add),
  window_first: (min_by(.ts).ts), window_last: (max_by(.ts).ts)
}' /tmp/window.jsonl
```

Then state, explicitly:

- days requested vs days with a file
- fleets present in the log vs fleets on the host (a fleet on the host with zero
  rows has the digester **off** — name it; that absence is itself reportable)
- counts of `ok` / `skipped` / `error`

`skipped` means the session was below `SESSION_DIGEST_MIN_TURNS`. It is **not** a
failure and must not be counted as one.

## Step 4 — Aggregate the rubric fields

The reasoning substrate is `context` · `worked` · `failed` · `would_change` ·
`reusable`. Reduce, do not forward wholesale:

```bash
# Volume signals per fleet
jq -s 'group_by(.fleet) | map({
  fleet: .[0].fleet, sessions: length,
  turns: (map(.turns) | add), tool_calls: (map(.tool_calls) | add),
  transcript_bytes: (map(.transcript_bytes) | add)
})' /tmp/window.jsonl

# Non-empty friction rows, newest first — these carry the signal
jq -c 'select((.failed // "") != "" or (.would_change // "") != "")
       | {session_id, bot, fleet, ts, failed, would_change}' /tmp/window.jsonl
```

Group recurring themes across sessions rather than listing every row. **Keep the
`session_id` on every theme** — `/fleet-observe` cannot cite what you dropped, and
an uncitable theme is unusable downstream.

## Step 5 — Join the rollups

```bash
claudlobby --fleet "$F" uptime
claudlobby --fleet "$F" utilization
claudlobby --fleet "$F" report-back --since "${DAYS}d"
```

These answer "was the fleet even working?" — the denominator for anything the
digest rows suggest. A spike in `failed` rows across a week when utilization
halved is a different story from the same spike at steady load.

If a rollup command fails, report the failure verbatim and continue with the
rest. Never synthesise a rollup you did not get.

## Step 6 — Bound the output

Budget at **≈4 characters per token** (planning estimate only — use
`lib/transcript-usage.py` for actual spend). Target a summary that comfortably
fits a reasoning pass alongside its instructions.

If you must cut, cut in this order — **and say what you cut**:

1. `ok` rows with all-empty rubric fields (they carry no signal)
2. `skipped` rows (retain the count only)
3. Oldest days first, never newest
4. Repeated instances of a theme already represented — keep the count and at
   least one citable `session_id`

**Never** cut without saying so. A silently truncated summary makes the
reasoning pass confidently wrong, and `no-fabrication` covers exactly this.

## Output

```
COVERAGE
  window: <N> days requested, <M> days with data (missing: <dates>)
  fleets in log: <list>   |   fleets with digester OFF: <list>
  rows: <N> ok · <N> skipped · <N> error
  truncation: <none | what was dropped and why>

VOLUME (per fleet)
  <fleet>: <sessions> sessions · <turns> turns · <tool_calls> tool calls

FRICTION THEMES (each with citable session_ids)
  <theme> — <N> sessions — [<session_id>, ...]

ROLLUPS
  uptime / utilization / report-back highlights, or the verbatim failure

UNRESOLVED
  anything the log could not answer — a gap here is a finding for /fleet-observe
```

## Do not

- **Do not read raw transcripts.** Not as a fallback, not to resolve an ambiguity.
  If the digest cannot answer it, the instrument is inadequate — record that under
  UNRESOLVED and let `/fleet-observe` make it a finding.
- **Do not reason or conclude.** No "this suggests", no severity, no
  recommendations. Assemble; `/fleet-observe` judges.
- **Do not emit unredacted rubric text.** It is model-written free text over real
  sessions. Redact secrets to their last four characters and personal identifiers
  to a placeholder before quoting.
- **Do not infer health from missing data.** Say the instrument was off.
