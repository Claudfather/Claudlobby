---
name: fleet-status
description: "Quick health check across your own fleet's bots — session and service observations, reported context-degraded state, who's idle/working/down/unknown."
argument-hint: "[bot-name]"
---

# Fleet Status

Check health of the bots in your own fleet. The generated bot context selects
the fleet; do not name it. Run each command below as one literal command in its
own Bash tool call and read the returned JSON directly — no pipes, shell
variables or loops, which are not the granted operation.

## Step 1: Read the fleet

```bash
claudlobby --json fleet status
```

`data.bots[]` lists every declared bot with its private-session, supervision
and recorded-heartbeat observations. This is the source of truth for declared
versus observed bots. Do not list tmux sessions yourself: bots run on the
fleet's private socket, so the default socket shows none of them and would read
every worker as dead.

Inspect `ok` first. A refusal or an unknown observation is **unknown**, not
down; say which source could not answer.

## Step 2: One bot in detail (optional)

For a single bot, or any bot Step 1 shows as down or unknown:

```bash
claudlobby --json bot status BOT
claudlobby --json bot session BOT
```

Replace `BOT` with its literal declared ID. `bot session` reads the private
session and native enrollment; it does not inspect another process by name.
For recent output, `claudlobby --json bot logs BOT --lines 50` is a bounded log
tail; a missing log is different from an unreadable one.

## Checks

### Reported context state

No bot can measure a context percentage, so do not ask for one and do not
report one (`context-management`). What IS available is the worker's own
`context-degraded` report:

Choose a 24-hour cutoff from the current time in RFC3339 form with an offset,
then read the report pages directly:

```bash
claudlobby --json fleet reports list --since CUTOFF
```

Replace `CUTOFF` with the literal timestamp.

Inspect `ok` and each item's captured summary for `context-degraded`; continue
with `--cursor NEXT_CURSOR` until `data.next_cursor` is null. A withheld summary
cannot prove the worker did not report degradation. Count completed reports in
the same window only after reading every page.

Any bot listed there is asking to be restarted — pair it with its completed
count in the same window before deciding.

The generated context already selects your fleet; reading another fleet is an
operator action, not this skill. Inspect the result envelope and exit status
before treating an empty page as clear. Do not pipe the CLI straight to `grep`:
that hides its failure status, and a single page may omit later reports.

## Report Format

```
FLEET STATUS

Bots (from claudlobby fleet status):
  <bot-a>: running (idle)
  <bot-b>: running (working — current task)
  <bot-c>: down (declared, no private session observed)
  <bot-d>: unknown (which source could not answer)
```

If an argument is provided (a specific bot name), check only that bot instead of the full fleet.
