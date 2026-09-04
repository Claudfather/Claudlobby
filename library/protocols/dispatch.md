---
title: Dispatch Protocol
---

# Dispatch Protocol

Manager → worker via the socket-aware `lib/dispatch.sh` helper (each bot runs on its **own** tmux server, so a raw `tmux send-keys -t <worker>` against the default per-user socket no longer reaches it). The default payload format is `[BOTCOMMAND]` — a structured envelope that workers parse on receipt (see `worker-lifecycle` for the inbound spec).

## [BOTCOMMAND] format

```
[BOTCOMMAND] <manager> | <type> | <summary> | <key:value pairs>
```

**Types:**

| Type | Purpose |
|------|---------|
| `task` | Implementation work — branch, code, PR |
| `cancel` | Abort current task, discard WIP |
| `compact` | Run `/compact` to free context |
| `restart` | Wrap up, report back, expect session restart |
| `query` | Answer inline — no branch, no PR |

**Key-value pairs** (optional, pipe-delimited after summary):

| Key | Values / format | Purpose |
|-----|-----------------|---------|
| `repo:<name>` | Repository name | Target repo for the work |
| `branch:<name>` | Branch name | Specific branch to work on |
| `report:<target>` | Bot name or channel | Where to send the `[BOTREPORT]` |
| `priority:<level>` | `high` / `normal` / `low` | Task priority |
| `ref:<url>` | Issue or PR URL | Originating issue or context link |
| `workstream:<ws-id>` | Workstream id | Registry entry this task advances |
| `task:<task-id>` | `t-<epoch>-<hex4>` | **Task identity** — minted by `dispatch-task.sh`, recorded in the dispatch ledger. The worker MUST echo it in every `[BOTREPORT]` for this task (`report-back.sh --task <id>`): the overdue watchdog joins on it, and an id-less report can never close an id'd dispatch. |

### Always zone a timestamp

**Never write a bare `HH:MM` to another bot. Always `10:47 EDT` or `14:47Z`.**

The host clock runs UTC and bots report in local time, so a bare figure is
ambiguous at the moment it is read and unrecoverable afterwards. It cost us
twice in twelve hours: a four-hour margin was read as an eighteen-minute
emergency, and a three-way roster is now permanently unreconcilable because each
disclosure used a different zone and none of them said which.

The second failure is the one that argues for the rule. A misread margin is
caught the moment someone checks; a set of bare timestamps from different bots
cannot be reconciled later at any effort, because the information needed to
align them was never written down. Zone it at the point of writing or it is
gone.

### Examples

**Task dispatch:**

```
[BOTCOMMAND] ari | task | Fix rate-limit bypass in auth middleware | repo:backend | priority:high | ref:https://github.com/org/backend/issues/42
```

**Cancel in-flight work:**

```
[BOTCOMMAND] ari | cancel | Dropping the auth refactor — scope changed
```

**Free context on a worker:**

```
[BOTCOMMAND] ari | compact | Free context before next task
```

**Restart a worker:**

```
[BOTCOMMAND] ari | restart | Rolling restart for config reload
```

**Query (no branch/PR):**

```
[BOTCOMMAND] ari | query | What's the current retry logic in payment_service.py? | repo:backend
```

## Sending a dispatch (socket-aware)

Each bot runs on its **own** tmux server (a private `-L <socket>`), so a raw `tmux send-keys -t <worker> …` against the default per-user socket no longer reaches it. Dispatch through `lib/dispatch.sh`, which resolves the worker's socket from its session name and does the race-safe two-step send (text, pause, Enter) so Claude Code's TUI never swallows keystrokes during render:

```bash
$CLAUDLOBBY_ROOT/lib/dispatch.sh <worker> '[BOTCOMMAND] <manager> | task | <summary> | repo:<name>'
```

Full example:

```bash
$CLAUDLOBBY_ROOT/lib/dispatch.sh eng-1 '[BOTCOMMAND] ari | task | Run security audit on repo-a | repo:repo-a | priority:high | ref:https://github.com/org/repo-a/issues/99'
```

`dispatch.sh` prepends `set +H;` itself (disabling bash history expansion, which silently mangles `!` in prompts), sanitizes the input, and — on a miss (the worker's session is gone on its socket) — logs a `send_miss` event rather than silently dropping. You never hand-type `tmux send-keys -t`.

## Freeform fallback

For ad-hoc prompts that don't fit the structured format (exploratory questions, multi-paragraph context), freeform dispatch still works — any dispatch without a `[BOTCOMMAND]` prefix is treated as a freeform task:

```bash
$CLAUDLOBBY_ROOT/lib/dispatch.sh eng-1 "Look at the flaky test in tests/test_auth.py -- it passes locally but fails in CI about 30% of the time. Root-cause it and fix."
```

Prefer `[BOTCOMMAND]` for anything with a clear type, repo, or priority. Use freeform for exploratory or context-heavy dispatches where the overhead of structured fields isn't worth it.

After dispatch: workers do NOT post a Telegram ack — their first id-carrying `[BOTREPORT]` row is the acknowledgement (Worker Lifecycle, Step 2) — and they may go quiet during work before posting completion. Do not poll a **tracked** (id'd) dispatch; the watchdog section below covers it. An **untracked freeform send has no watchdog** — capturing the worker's pane if nothing comes back is your only net there.

## Tracked dispatch & the overdue watchdog

For tasks you want tracked, dispatch via `lib/dispatch-task.sh` instead of raw `send-keys` — and pass at least `--botcommand` (or any envelope flag: `--repo`, `--priority`, `--ref`, `--workstream`) so the send mints a task id:

```bash
$CLAUDLOBBY_ROOT/lib/dispatch-task.sh --botcommand <worker> "<task>"
$CLAUDLOBBY_ROOT/lib/dispatch-task.sh --repo <name> --workstream <ws-id> <worker> "<task>"
```

### Sending a peer a message that asks nothing

**Use `--type` for anything that is not a task, and the envelope stops minting.**

```bash
$CLAUDLOBBY_ROOT/lib/dispatch-task.sh --type query <bot> "<question answered inline>"
```

`--type task|cancel|compact|restart|query` (default `task`) implies `--botcommand` — which is now exactly `--type task`, kept as an alias — so you still get the `[BOTCOMMAND]` format — a finding, a relay, a retraction, a correction all read correctly as fleet messages. **Only `task` mints an id.**

This exists because the two used to be one decision: `--botcommand` alone minted, so a peer note you sent in the fleet format became a tracked row that nothing had been asked to close, and it stayed open forever. Measured at 68 such rows on this host, 57 of them addressed to managers (#1187).

**Pick the type by what you are asking for, not by how important the message is.** A `query` is answered inline, so nothing it produces can be *joined* against an id — which is not the same as producing nothing. It still files a terminal `[BOTREPORT]`: `worker-lifecycle` routes `query` to "skip to Step 8", and Step 8 *is* the terminal report. `restart` reports terminally too. If you are unsure, `query` is the safe way to be wrong: it costs an untracked message — which is what every raw send already is — where the reverse costs a row nobody can ever close. An unrecognised `--type` is refused rather than defaulted, so a typo will not quietly mint.

**A non-`task` row records no `expected_by` either.** Withholding the id alone would be half a fix: the watchdog matches on the deadline, not on the id, so an id-less row that still carried one would go overdue and push a `[FLEET-PULSE]` alert naming nothing. Both are withheld together.

**You do not have to do anything for the worker's answer to stay harmless.** A terminal report carrying no id normally resolves to the bot's oldest open id'd dispatch (#835), which would silently close unrelated in-progress work as `completed`. That resolution is suppressed automatically while a non-`task` note is the most recent thing you sent the bot — enforced in `dispatch-overdue.py`, not by worker discipline, so it holds for a bot that has not restarted since this landed. It resumes on the bot's next report, so a peer note costs one un-auto-closed row at most, never a false completion.

`task`-type envelope sends mint a `task:<id>`, record it (with a deadline from `OBSERVABILITY_DISPATCH_DEADLINE`, or `--deadline-min N`) on the plane as a work item + assignment, and transmit it — the overdue watchdog then joins on identity, and the worker's terminal report closes exactly that task. A bare `dispatch-task.sh <worker> <task…>` still works but stays id-less (matched by bot+time, one report closes all open dispatches for that bot) — prefer the id-minting form for anything you want individually tracked. The fleet pulse then watches it: if the deadline passes with no terminal `[BOTREPORT]` (completed/failed/blocked), it emits `overdue_dispatch` and pushes a debounced `[FLEET-PULSE]` note into **your** session. So you don't have to remember to poll — an unanswered task surfaces itself. **The page is gated, not unconditional**: recent progress from that bot, supersession, a worker respawn since dispatch, the age cap, an unreachable manager, or the debounce each suppress it — so the absence of a page is not proof all is well.

When you get an `overdue_dispatch` alert: check the worker (cross-reference `activity_stuck` — it may be hung, see `fleet-observability`). Then recover it, re-dispatch/reassign if it's wedged or mis-scoped, or escalate to the human. The watchdog tells you *something is overdue*; the call on what to do is yours. A worker's terminal report closes the dispatch automatically — no manual bookkeeping.

## Preflight: ensure the worker is up under proper supervision

Before dispatch, verify the target session exists. If it doesn't, **always bring it up via `lib/spin-up-bot.sh <bot-dir>`** — never `start-bot.sh` directly. `spin-up-bot.sh` is host-aware: it enrolls the bot as a systemd-user service on Linux or a launchd LaunchAgent on macOS, so the bot is supervised (auto-restart on crash, picked up by the fleet keepalive timer). `start-bot.sh` only spawns a raw tmux session — bots launched that way are invisible to the keepalive scope and won't survive a crash.

```bash
# Idiomatic worker spin-up (idempotent: restarts if already enrolled):
$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh $CLAUDLOBBY_ROOT/local/<fleet>/runtime/bots/<bot>
```

To audit/repair an entire fleet's supervision state in one shot:

```bash
$CLAUDLOBBY_ROOT/lib/reconcile-fleet.sh <fleet>          # report only
$CLAUDLOBBY_ROOT/lib/reconcile-fleet.sh <fleet> --enroll  # enroll orphans AND prune fleet-state — see below
```

**`--enroll` also prunes the shared fleet-state, and that prune is scoped.**
Alongside enrolling orphans it applies the fleet-state prune to
`state/fleet-state.json` — one file shared by every fleet on the host (#892), so
the scoping is what keeps one fleet from reaping another's rows. A row is yours
to remove only when **all three** hold:

1. your `fleet.yaml` no longer declares it — the reason to prune at all;
2. **no** fleet on this host declares it — so a sibling's live bot is never
   touched, and a bot that just moved via `claudlobby move-bot` survives even
   though its stamp still names the old fleet;
3. it is **stamped as yours** — a sibling's *departed* bot is still theirs.

Anything else is reported and left alone. An unstamped row predates stamping and
is **kept**, which makes that transition safe by construction rather than by
migration.

**What is still true of the flag.** It writes to a host-global file, so it is not
a read-only operation confined to your own fleet. And it refuses rather than
guesses: if any fleet's manifest cannot be parsed, the prune declines and touches
nothing — a silently-empty roster would read as "no fleet declares this bot" and
license exactly the deletion condition 2 exists to prevent (#1146).

Consequences of a missing row are bounded: it degrades that bot's STATE to
`unknown`, never `down`, `fleet-pulse` does not read the file, and rows
regenerate on each bot's next start or report. Still reach for the bare form
unless you actually intend to enroll — the flag reads like "also fix the orphans"
and does more than that — but the blast radius is now your own fleet's departed
rows, not the host.

`reconcile-fleet.sh` reports five buckets: healthy (tmux + unit), orphan (tmux but no unit — unsupervised), missing (unit but no tmux — down), unsupervised-down (neither — declared but nothing running or supervised; keepalive cannot revive it), unbound (running but not in any fleet.yaml — investigate before killing).

## Preflight: check shared knowledge before dispatch

Before dispatching to a repo, check what the fleet already knows about it: is there an active plan for the target repo, and are there learnings the worker should start with?

**Use the door your Shared Documentation section names, not a hardcoded path.** A vault-wired fleet queries Claudron (`claudron recall`); a fleet with a raw doc tree scans `shared/planning/active/INDEX.md` and `shared/knowledge/<repo>/INDEX.md`. Both answer the same question, and your composed instructions say which one you have — reaching for the wrong one on a vault-wired fleet means hand-opening files the vault door exists to replace (#1172).

If relevant docs exist, **include the key context in the dispatch prompt** so the engineer doesn't duplicate work, contradict an in-flight plan, or re-discover something the fleet already knows.

Example: if an active plan covers auth refactoring in `backend`, and you're dispatching a login endpoint task to the same repo, reference the plan in the dispatch: "See the active backend auth-rework plan — your task aligns with phase 2."

## Manager: active-plan monitoring

The orchestrator periodically reviews the fleet's active plans — through the door its Shared Documentation section names, same rule as above — to:

- **Surface stale plans** — status: active but `updated:` older than 7 days. Ping the owner.
- **Detect conflicts** — two active plans touching the same repo. Flag for human resolution.
- **Catch forgotten transitions** — a completed task whose plan still says status: active. Nudge the owner to update status and regenerate the directory's INDEX.md with whichever indexing skill they have.
