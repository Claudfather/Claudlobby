---
name: fleet-digest
description: "Use when a monitoring pass needs its input — usually as the first step of /fleet-observe. Assembles the monitor's pass input: joins the plane's session_summary events with vitals, utilization, and fleet report rollups into one bounded, coverage-honest summary. Reads only pre-aggregated sources — never raw transcripts."
argument-hint: "[days] [fleet]"
tool_grants:
  - "Bash(jq *)"
  - "Bash(python3 *)"
  - "Bash(claudlobby --json event list *)"
---

# Fleet Digest

Build the input for a monitoring pass. You **assemble and bound**; you do not
reason and you do not emit findings — that is `/fleet-observe`. Keeping those
apart is what stops thousands of raw rows reaching an Opus reasoning pass.

Contract: the `fleet-monitoring` protocol. Read its evidence-contract and
token-discipline sections before changing anything here.

**Arguments:** `$1` = window in days (default `7`). `$2` = fleet label for the
coverage rows (default: your own fleet's name from `claudlobby --json context
show`).

## Step 1 — Scope: your own fleet

The digest is a `session_summary` **system event on the plane**, recorded by the
`session-export` fleet job from clauDNA's export door. `claudlobby event list` reads the plane one
fleet at a time, and the generated bot context selects **your own fleet**; the
commands below never name a fleet. A sweep of other fleets is an operator action
from an operator shell (`claudlobby --fleet NAME --json event list …`), not a
grant of this skill — report other fleets as out of scope rather than reading
them.

```bash
FLEETS="${2:?pass your own fleet name from claudlobby --json context show}"
```

## Step 2 — Assemble the window from the plane

`claudlobby event list --since` bounds the window at the Plane reader. For
each fleet, pull its `session_summary` events and reshape each plane row back to
the flat shape the rest of this skill reads: the pre-aggregated digest rides
`.data`, while `bot` and `occurred_at` sit on each `data.items[]` entry — lift
`.data` up and carry `bot`, `ts` and the fleet. Follow `data.next_cursor` until
empty before claiming complete coverage.

```bash
DAYS="${1:-7}"
SINCE="$(date -u -d "-$((DAYS - 1)) day" +%Y-%m-%d 2>/dev/null \
         || date -u -v-"$((DAYS - 1))"d +%Y-%m-%d)"

: > /tmp/window.jsonl
: > /tmp/coverage.txt
for F in $FLEETS; do
  cursor=""; n=0; failed=0
  while :; do
    if [ -n "$cursor" ]; then
      out="$(claudlobby --json event list --type session_summary --since "${DAYS}d" --limit 1000 --cursor "$cursor")"; rc=$?
    else
      out="$(claudlobby --json event list --type session_summary --since "${DAYS}d" --limit 1000)"; rc=$?
    fi
    if [ "$rc" -ne 0 ]; then failed=1; break; fi
    page="$(printf '%s\n' "$out" | jq -c --arg fleet "$F" --arg since "$SINCE" \
      '.data.items[] | select((.occurred_at // "")[0:10] >= $since) | .data + {fleet: $fleet, bot: .bot, ts: .occurred_at}')"
    if [ -n "$page" ]; then
      printf '%s\n' "$page" >> /tmp/window.jsonl
      n=$((n + $(printf '%s\n' "$page" | wc -l)))
    fi
    cursor="$(printf '%s\n' "$out" | jq -r '.data.next_cursor // empty')"
    [ -n "$cursor" ] || break
  done
  if [ "$failed" -eq 1 ]; then
    # A failed Plane read is NOT "no rows"; even a partial page is unknown.
    printf '%s\tUNREACHABLE\n' "$F" >> /tmp/coverage.txt
  else
    printf '%s\t%s\n' "$F" "$n" >> /tmp/coverage.txt
  fi
done
```

`/tmp/coverage.txt` now holds one row per fleet — a row count, or `UNREACHABLE`.
A fleet with **zero rows and no error** has no sealed segment in the window,
a disabled `session-export` job (`SESSION_EXPORT_ENABLED=0`), or stale/old
entrypoints. Name which from the per-bot state files: in an overlay fleet,
set `JOB=session-export` and read `"$BOT_DIR/../../$JOB/"*.json`; in root mode,
read `"$BOT_DIR/../../fleet/$JOB/"*.json`. These are `<bot>.json` files in the
`session-export` directory under `<fleet_state>`.
Read them with granted `jq`, looking at `last_skip`, `last_ack_at`, `pending`,
`consecutive_failures` and `stall`. Missing/unreadable files mean unknown coverage.
The operator's `host doctor` rung reads these same files; the monitor does not run
it or the private export tick (which spawns a store process for every bot).

**If the window is empty, stop with the coverage explanation.** A fleet marked
`UNREACHABLE` has unknown coverage; report its read failure and do not infer health.

**If every row is skipped, print `COVERAGE: summaries available on 0 of N represented bots
(all rows skipped: <reason tally>)` and stop.** N is the distinct bots represented
in the rows, not the whole declared roster. Identity and segment volume are real;
there is no journey to reason over. Report this as coverage once, never per session.

## Step 3 — Coverage first, before any aggregation

Coverage is the part most likely to be quietly dropped, so compute it first and
carry it through:

```bash
jq -s '{
  rows: length,
  fleets: (map(.fleet) | unique),
  bots:   (map([.fleet, .bot]) | unique | length),
  bots_with_summaries: (map(select(.status=="ok") | [.fleet, .bot]) | unique | length),
  by_status: (group_by(.status) | map({(.[0].status): length}) | add),
  window_first: (min_by(.ts).ts), window_last: (max_by(.ts).ts)
}' /tmp/window.jsonl
```

Here k is `bots_with_summaries` and N is `bots`: observed summary availability
among represented bots, never a claim about arming configuration. An armed bot
can still yield only `trivial`, `no_transcript` or `gave_up` rows. Only separately
read configuration could establish whether its spend switch is armed.

Then state, explicitly (`/tmp/coverage.txt` carries a row per fleet):

- days requested vs days with data
- fleets with data vs fleets with zero rows (name the cause from state files) vs fleets `UNREACHABLE`
  (the plane refused — coverage there is unknown, not empty)
- counts of `ok` / `skipped`

`skipped` means no summary was produced. `skipped_reason` names why: `disabled`
(summaries off by composition), `headless` (variable unset, not a composed bot),
`trivial`, `no_transcript`, `gave_up`, `retired`, `unexportable` (the consumer could
not record the item), or an unknown future reason. Every one is a skip, not a failure.

## Step 4 — Aggregate the journey fields

Read `journey.title/intent/outcome/done/in_progress/next`. The released 0.28 schema
uses `outcome: shipped|partial|blocked|abandoned|exploratory`, `arc` entries with
`step/result`, and `done/in_progress/next` entries with `text` (plus optional fields).
Reduce recurring themes rather than forwarding the whole model-written journey.

```bash
# Volume: segment counts on all rows; turns and bytes on ok rows only.
jq -s 'group_by(.fleet) | map({fleet: .[0].fleet,
  sessions: (map(.session_id) | unique | length),
  prompts: (map(.prompts // 0) | add), failures: (map(.failures // 0) | add),
  turns: (map(select(.status=="ok") | .turns // 0) | add),
  transcript_bytes: (map(select(.status=="ok") | .transcript_bytes // 0) | add)
})' /tmp/window.jsonl

# Friction: preserve each finding's source session and nested evidence.
jq -c 'select(.status=="ok" and
  (.journey.outcome != "shipped" or (.failures // 0)>0 or ((.journey.next // [])|length)>0))
  | {session_id, bot, fleet, ts, journey, failures}' /tmp/window.jsonl
```

`turns` and `transcript_bytes` are null on skipped rows. Tool-call totals wait for
a separate PR granting this skill the `plane samples` door; no such grant exists.
Keep the `session_id` on every theme so `/fleet-observe` can cite it.

## Step 5 — Join the rollups

```bash
claudlobby --json fleet uptime
claudlobby --json fleet utilization
claudlobby --json fleet reports list --since CUTOFF
```

Run each as one literal command; `CUTOFF` is the literal `<SINCE>T00:00:00Z`
with Step 2's date. These read your own fleet.

This cutoff is the first UTC midnight of the calendar-day window assembled in
Step 2, not a rolling `DAYS` × 24-hour cutoff. For reports, inspect `ok` and
follow `data.next_cursor` with `--cursor` until null before computing counts
or highlights. If a page fails or content is withheld, disclose that limit;
never count an unreadable page as zero.

These answer "was the fleet even working?" — the denominator for anything the
digest rows suggest. A spike in `failed` rows across a week when utilization
halved is a different story from the same spike at steady load.

If a rollup command fails, report the failure verbatim and continue with the
rest. Never synthesise a rollup you did not get.

## Step 6 — Bound the output

Budget at **≈4 characters per token** (planning estimate only — use
`claudlobby --json fleet usage --since 24h` for covered Claude token counts,
not quota). Target a summary that comfortably
fits a reasoning pass alongside its instructions.

If you must cut, cut in this order — **and say what you cut**:

1. `skipped` rows (retain the count only)
2. `ok` rows with an empty journey (retain the count)
3. Oldest days first, never newest
4. Repeated instances of a theme already represented — keep the count and at
   least one citable `session_id`

**Never** cut without saying so. A silently truncated summary makes the
reasoning pass confidently wrong, and `no-fabrication` covers exactly this.

## Output

```
COVERAGE
  window: <N> days requested, <M> days with data (missing: <dates>)
  fleets with data: <list>  |  zero rows + cause: <list>  |  UNREACHABLE: <list>
  summaries available: <k> of <N> represented bots
  rows: <N> ok · <N> skipped
  truncation: <none | what was dropped and why>

VOLUME (per fleet)
  <fleet>: <sessions> sessions · <turns> turns · <prompts> prompts · <failures> failures
  turns · bytes: ok rows only

FRICTION THEMES (each with citable session_ids)
  <theme> — <N> sessions — [<session_id>, ...]

ROLLUPS
  uptime / utilization / fleet report highlights, or the verbatim failure

UNRESOLVED
  anything the digests could not answer — a gap here is a finding for /fleet-observe
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
- **Do not infer health from missing data.** Name the coverage cause from the job state files.
