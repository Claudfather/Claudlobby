# Advanced Patterns

Patterns that extend a running claudlobby fleet beyond basic dispatch and briefings. Each section is self-contained — implement whichever ones fit your setup.

Prerequisites: a working fleet with at least a manager bot and one worker, supervised services (systemd user units on Linux, launchd LaunchAgents on macOS), and the shared `lib/` scripts. See [getting-started](getting-started.md) if you're not there yet.

Two mechanics run through most of these patterns. Read them once here:

- **Every bot runs on its own tmux server.** A bot's session lives on a *private* tmux server addressed by `-L <socket>`, where the socket name is the bot's `BOT_SERVICE` (also written to `bot.conf` as `TMUX_SOCKET`). One server's death drops only that bot, never the fleet. The practical consequence for these patterns: a bare `tmux -t <bot>` or `tmux send-keys -t <bot>` targets the *default* tmux server, which has none of your bots on it — the call silently no-ops rather than erroring. Always dispatch through the socket-aware helpers (`lib/dispatch.sh`, `lib/report-back.sh`, `lib/bot-sweep-cron.sh`) or address the socket explicitly (`tmux -L <bot-service> ...`). Section 5 covers the dispatch/report-back model.

- **Several patterns below ship as library skills, not recipes.** Where a pattern is a real skill, you enable it by adding its name to a bot's `skills:` list in `fleet.yaml` and running `claudlobby generate` — the compositor symlinks `library/skills/<name>/` into that bot's `.claude/skills/<name>/`. Because the symlink points at the shared library file, edits to `library/skills/<name>/SKILL.md` propagate live to every bot using it. Never hand-author a `SKILL.md` into a generated bot directory: the next `generate` overwrites it, and it defeats the whole point of composition. For those patterns, this doc gives you the *why*, the one-line wiring, and how to schedule it — the skill file itself is the source of truth for the steps, so we point at it rather than copy it.

---

## 1. Lifecycle Orchestration (/lifecycle)

**Shipped skill — `library/skills/lifecycle/SKILL.md`.**

A full development pipeline run by the manager bot: dispatch an engineer to implement, dispatch a reviewer, route the review (merge / send back for mechanical fixes / flag a human on ambiguous concerns), then run a retro and file follow-up issues — pulling a human in only when there's a real judgment call.

### Why

Without this, you manually dispatch the engineer, wait, dispatch the reviewer, wait, read the review, decide, merge, and forget to capture learnings. `/lifecycle` automates the chain and only interrupts you when a human decision is genuinely needed.

### Enable it

Add `lifecycle` to the manager bot's `skills:` and run `claudlobby generate`:

```yaml
bots:
  lead:
    skills: [dispatch, lifecycle, ...]
```

The example fleet already wires this on its `lead` bot. The phase-by-phase decision table (approve → merge, mechanical → send back, ambiguous → flag human, 3+ cycles → flag human) lives in `library/skills/lifecycle/SKILL.md`; that file is the source of truth, so it isn't restated here.

### How it dispatches

Lifecycle hands work to the engineer and reviewer through the same socket-aware path every fleet dispatch uses — `lib/dispatch.sh` (or `lib/dispatch-task.sh`, which additionally records the task to the dispatch ledger with a deadline so an overdue task surfaces as `overdue_dispatch`). Workers signal progress and completion with `lib/report-back.sh`, which the manager reads in its own pane. See Section 5 for both.

### Gotchas

- The manager waits for the `[BOTREPORT]` message rather than polling. Each dispatch can take minutes to tens of minutes; the report sits in the manager's pane buffer until it reaches a natural pause.
- "Mechanical fix" vs "ambiguous concern" is a judgment the manager makes by reading the review. Bias toward flagging a human early — a false escalation is cheaper than a bad merge.

---

## 2. Alert Sweep (/data-alert-sweep)

**Shipped skill — `library/skills/data-alert-sweep/SKILL.md`.**

Batch-process alerts from a monitoring channel: pull recent alerts, read each thread, check whether a PR already exists, investigate independently, then approve the existing fix, comment with a differing conclusion, or open a new PR — never a competing one.

### Why

Monitoring channels (Slack, Datadog, etc.) accumulate alerts faster than anyone triages them. This pattern works through them in bulk and, critically, refuses to create a second PR for a problem someone already has a PR for.

### Enable it

Add `data-alert-sweep` to the bot that owns the monitoring integrations (typically the manager, or a business bot with Slack + GitHub MCP) and run `claudlobby generate`. The skill's core principle — *always investigate independently; existing PRs and team comments are inputs, not conclusions; never open a competing PR* — is defined in the skill file.

### Scheduling

To run it on a cadence, use the socket-aware sweep dispatcher rather than a raw tmux keystroke: `lib/bot-sweep-cron.sh <bot-session> "/data-alert-sweep recent"` resolves the bot's private socket, skips the tick if the pane looks busy, and does the race-safe send. Wire that as a fleet job (a `jobs:` entry composed into a systemd/launchd timer — the same mechanism behind `data-sweep` and `disk-monitor`; see [fleet-yaml-schema](fleet-yaml-schema.md)) so enrollment is managed rather than living in a personal crontab.

### Gotchas

- Reading full threads for a busy channel is slow; scope the lookback (the skill takes `recent|today|all-open`).
- Related alerts often share a root cause. The skill errs toward one PR per alert rather than trying to cluster perfectly — accept some manual reconciliation.

---

## 3. Triage (/triage)

**Shipped skill — `library/skills/triage/SKILL.md`.**

Surface untracked work hiding in unstructured inputs (email, meeting notes, alert channels), enrich each item with external data (order lookups, existing issues, contact records), deduplicate against your tracker (Notion), and create structured tasks with source links.

### Why

Action items hide in inboxes and meeting notes and never become tracked tasks. This pattern reads those inputs and moves the net-new items into a structured system with context attached.

### Enable it

Add `triage` to a bot with the right integrations — the manager, or a business bot with Gmail + Notion + Shopify MCP — and run `claudlobby generate`. The source-to-enrichment mapping and the dedup-before-create rule are in the skill file.

### Scheduling

Same as the alert sweep: dispatch it on a cadence via `lib/bot-sweep-cron.sh <bot-session> "/triage email"` wired as a fleet job, so the send is socket-aware and the schedule is managed by a composed timer rather than a hand-edited crontab.

### Gotchas

- Deduplication is the hard part. Fuzzy matching against the tracker beats exact subject-line matching but still lets near-duplicates through occasionally — expect to clean up manually.
- Meeting-transcript items can be phantom (transcription errors). Have the bot mark them "from meeting notes (verify)" rather than treating them as confirmed.

---

## 4. Graceful Restart & Pre-Stop Handoff

Capture a bot's working context before a restart kills its process, so the next session can resume where it left off. The mechanism is real and current (`lib/pre-stop-handoff.sh`), but it is *not* wired to systemd `ExecStop` — it's invoked by whatever is doing the restart.

### Why

A blunt `systemctl restart` (or `launchctl kickstart -k`) kills the bot mid-thought. Any in-flight reasoning about ongoing tasks is lost. A handoff gives the bot a few seconds to write down what it's doing first; the fresh session reads it back and picks up.

### Two entry points

Which path runs the handoff depends on *who* is restarting the bot:

- **In-session restart (the `restart` skill).** When a bot restarts itself — `/restart`, or `/restart --auto` from an automated caller — the `restart` skill invokes `/claudna:session handoff` **directly** in that session, notifies the channel, then delegates the actual bounce to `lib/spin-up-bot.sh` (which picks systemd vs launchd for you). It deliberately does *not* shell out to `pre-stop-handoff.sh`: that script sends the handoff as a tmux keystroke and waits for it, so from inside the very session being restarted it would queue the handoff *behind* the restart and lose it. See `library/skills/restart/SKILL.md`.

- **External restarter (`lib/pre-stop-handoff.sh`).** When something *outside* the session bounces the bot and can't invoke a skill directly, it calls `lib/pre-stop-handoff.sh <bot-dir>` first. The canonical caller is `lib/weekly-worker-restart.sh`, which bounces worker bots weekly to pick up a staged Claude Code binary; it runs the handoff, then `spin-up-bot.sh`.

### What `pre-stop-handoff.sh` actually does

Given a bot directory, the script:

1. Loads the bot's config and resolves its tmux session name and **private socket** (`tmux_socket_for_bot`) — the handoff is sent on the bot's own server, not the default one.
2. Checks `<bot-dir>/.claude/session.md` (where clauDNA's `/claudna:session handoff` writes). If a handoff was written in the last 5 minutes, it's fresh enough — skip and exit.
3. Otherwise, if the session is live, sends `/claudna:session handoff --auto` and waits up to ~30 seconds for a new `session.md` to land.
4. Always exits 0. The handoff is best-effort and never blocks the restart: a timed-out or unreachable bot just proceeds to the bounce. (It also cleans up the transient `.tmux-env` secrets file on every exit path.)

### Resume on the next start

You don't configure resume separately. `lib/start-bot.sh` injects `/claudna:session resume --auto` as the new session's first keystroke; resume is age-gated (it only resumes from a checkpoint fresher than ~24h, else clean-starts). This no longer depends on the bot's `STARTUP_PROMPT` carrying a resume instruction.

### Gotchas

- **Don't add `ExecStop=.../pre-stop-handoff.sh` to a bot's `.service` file.** Bot units are generated by `claudlobby generate` (the file header says *do not hand-edit*), and the generated `ExecStop` simply tears down the bot's tmux server. Hand-edits are overwritten on the next generate. The handoff belongs at the *restarter*, per the two entry points above.
- Best-effort by design: if a bot is deeply stuck (unresponsive MCP, tight loop), the handoff times out and the restart proceeds anyway. That's the intended behavior.
- The `--auto` flag matters — it runs the handoff non-interactively so the bot doesn't stop to ask for confirmation.

---

## 5. Inter-Bot Communication (dispatch.sh + report-back.sh)

Structured, deterministic messaging between bots over tmux. The manager dispatches work to a worker; the worker reports back when it has something to say. Telegram is unreliable for bot-to-bot traffic (messages drop); a direct send into the peer's pane is instant and observable.

Because each bot is on its own tmux server (see the intro), both directions go through helpers that **resolve the peer's socket** and send safely — they never assume a shared default server.

### Dispatch: manager → worker

```bash
# Resolve the worker's socket, precheck the session, race-safe two-step send:
$CLAUDLOBBY_ROOT/lib/dispatch.sh <worker-session> "Implement X in org/repo. Branch + PR. Report back when done."
```

`dispatch.sh` reverse-resolves the worker's private socket from its session name, confirms the session exists on that socket, and sends the text and Enter as two steps (so a rendering TUI can't swallow the keystroke). If the peer can't be reached it logs a `send_miss` event and exits non-zero instead of silently dropping the message.

`lib/dispatch-task.sh` wraps `dispatch.sh` with accountability: it appends the task to `state/dispatch-log.jsonl` with a deadline (`expected_by`) before sending, so the fleet-pulse watchdog can flag the task `overdue_dispatch` if no terminal report arrives in time. Any envelope flag (`--botcommand`, `--repo`, `--priority`, `--ref`, `--workstream`) wraps the task in a `[BOTCOMMAND]` envelope **and mints a `task:<id>`** the worker echoes back (`report-back.sh --task <id>`), so the watchdog joins on identity — prefer `--botcommand` at minimum for anything individually tracked.

### Report-back: worker → manager

```bash
$CLAUDLOBBY_ROOT/lib/report-back.sh <bot> <status> "<summary>" [flags...]
```

Message format written into the manager's pane:

```
[BOTREPORT] <bot> | <status> | <summary> [| progress:<N>] [| pr:<url>] [| artifact:<url>]
```

**Statuses:** `completed`, `progress`, `blocked`, `failed`. (`progress` — paired with `--progress N` — lets a long-running task report a percentage without claiming it's done.)

**Optional fields:** `--pr <url>`, `--issues <url,url>`, `--skill <name>`, `--progress <N>`, `--artifact <url>` (source-of-findings provenance, repeatable). Older positional forms like a bare `pr:<url>` argument still work.

```bash
# Completed with a PR:
$CLAUDLOBBY_ROOT/lib/report-back.sh eng-1 completed "Added rate limiting to auth endpoint" --pr https://github.com/org/api/pull/87

# Blocked:
$CLAUDLOBBY_ROOT/lib/report-back.sh eng-1 blocked "Need DB migration permissions — cannot alter production schema"

# Mid-task progress:
$CLAUDLOBBY_ROOT/lib/report-back.sh eng-1 progress "Refactoring auth" --progress 40
```

Beyond the pane message, `report-back.sh` appends a structured JSONL event to the fleet report-back ledger and mirrors the bot's state (idle/working/blocked) to `fleet-state`, so completion is queryable via `claudlobby report-back` even if the manager missed the pane message.

### Where the manager address comes from — you don't set it by hand

`report-back.sh` sends to the session named in `MANAGER_TMUX` (default `claude-bot`) on the socket in `MANAGER_TMUX_SOCKET`. **The compositor sets both for you** from your `teams:` wiring: a bot listed in a team's `workers` gets `MANAGER_TMUX=<that team's manager>` and the manager's socket; a manager bot gets its own id. You configure the relationship in `fleet.yaml` (`teams:`), not the env var.

> If you're following an older guide that mentions `MANAGER_BOT_NAME`: that variable never existed in the shipping code and was a documented bug. The real variable is `MANAGER_TMUX`, and it's composed automatically.

### Gotchas

- The `|` delimiter means summaries must not contain pipes. Keep summaries to one sentence.
- `send-keys` has a practical length limit — keep the whole message well under ~500 characters. For detail, include a PR/issue URL and let the manager read it via GitHub MCP.
- A dropped cross-socket send is no longer silent: it emits a `send_miss` event to the sender's `data/events/` stream, so you can see when a report failed to land (e.g. the manager's session was down).

---

## 6. Git Pull Scheduler

Keep cloned repos fresh across all bots so they aren't creating PRs against stale code. `lib/git-pull-all.sh` handles it.

### What the script does

```bash
$CLAUDLOBBY_ROOT/lib/git-pull-all.sh /path/to/projects/dir
```

It runs `git pull --ff-only` on every immediate subdirectory that is a git repo, logging results to `git-pull.log` **one level above** the target dir. `--ff-only` is the entire safety mechanism: if a repo has uncommitted local changes, or is on a branch that has diverged from its upstream, the pull fails harmlessly for that repo (logged as a failure) rather than creating a merge commit. It does **not** inspect the branch name or skip non-`main` repos — it attempts a fast-forward on every repo and lets `--ff-only` be the guard.

When the target path is a fleet runtime projects dir (`.../runtime/bots/<bot>/projects`), the script consults the fleet's `fleet.yaml` roster and no-ops for a bot no longer declared in that fleet — so a stale scheduled entry can't resurrect a departed bot's runtime directory (which fleet supervision would then flag as an orphan). For any other directory of repos it behaves generically.

### Scheduling

```crontab
# Daily, staggered so pulls don't collide with active work:
30 6 * * *  /path/to/claudlobby/lib/git-pull-all.sh /path/to/projects
```

Cron is fine here — the script is plain bash with no tmux involved. If you'd rather have enrollment managed alongside the rest of the fleet's timers, wire it as a `jobs:` entry instead of a personal crontab.

### Gotchas

- Don't pull while a bot is actively working in a repo. Schedule during off-hours or stagger the times; a mid-work pull that can't fast-forward just fails safe, but it's noise.
- If a repo fails repeatedly, check the log — usually uncommitted changes, a force-pushed remote, or a diverged branch. `--ff-only` will never resolve those for you, by design.

---

## 7. Automated Code Audits

Scheduled code reviews the fleet initiates on its own, so audits actually happen instead of waiting for someone to remember. This is a **shipped, opt-in feature** — the rolling code-audit sweep — not something to build from scratch.

### Why

Manual audits happen when someone remembers, which means they don't. A scheduled sweep works through your repos on a cadence and guarantees no repo goes stale unnoticed.

### How it works (and how to turn it on)

Add a `fleet.sweep:` block to `fleet.yaml` and give the owner bot the `code-audit-sweep` skill:

```yaml
fleet:
  sweep:
    owner_bot: astrid              # bot whose session runs the audit
    repos: [acme/api, acme/web]    # optional; defaults to owner_bot's scope.repos
    audit_types: [tech-debt, security-audit]  # rotated per run
    # label / schedule / enabled have sensible defaults
```

On a timer, the no-LLM selector `lib/code-audit-sweep.sh` asks GitHub for the most recent issue on each repo carrying the staleness label (default `auto-audit`), picks the **stalest** repo (oldest newest-audit, or never audited), and dispatches `/code-audit-sweep <org/repo> <audit-type>` into the owner bot's session via `lib/bot-sweep-cron.sh`. The `code-audit-sweep` skill runs the corresponding `/claudna:<audit-type>` audit and **guarantees the `auto-audit` label on every issue it files**.

The design's key property: **GitHub is the only ledger.** The labelled issues *are* the staleness record — an audit's own filed issues make its repo look "fresh" for the next run — so there's no local tracker file to maintain and nothing to drift out of sync. (Earlier guidance here described a hand-built `next-audit-target.py` + `audit-tracker.json`; that approach was replaced precisely because a local tracker drifts.)

After `claudlobby generate`, enroll the timer once per host: `lib/install-code-audit-sweep-systemd.sh <fleet>` (Linux) or `lib/install-code-audit-sweep.sh <fleet>` (macOS). Full field reference and the emitted observability events (`audit_selected`, `audit_dispatched`, `audit_completed`, …) are in [fleet-yaml-schema](fleet-yaml-schema.md).

### Gotchas

- The `auto-audit` label is load-bearing. If the audit run can't guarantee it (auth/rate-limit failure), the run reports failure rather than silently leaving the repo looking never-audited.
- Cap new issues per run (the skill targets ~10) so a single audit doesn't flood the tracker.
- Schedule it for off-hours so it doesn't compete with daytime engineering for the owner bot's session; the busy-pane guard in `bot-sweep-cron.sh` will defer a tick if the owner is mid-task, and the next run retries naturally.

---

## 8. Telegram Formatting

Consistent, reliable message output across all bots. The fleet-wide policy is **plain text by default** — and this reverses what older guidance recommended, for a concrete reason.

### The policy: plain text, no `parseMode`

Do **not** pass `parseMode`, and do **not** wrap output in `**bold**`, `_italic_`, or `` `backticks` ``. Send plain text. Technical identifiers (`chart_uuid`, `~/path`) then render correctly with no escaping, and there are no silent failures from a missed escape character. The bash helper `$CLAUDLOBBY_ROOT/lib/tg-post.sh` sends plain text by default; the Telegram MCP `reply` tool should be called *without* `parseMode`.

This is codified as a shipped protocol — `library/protocols/telegram-formatting.md` — which the example fleet composes into every bot via `defaults.protocols: [..., telegram-formatting]`. There's also a shared partial (`library/skills/_telegram-formatting.md`) that skills can reference.

### Why it reversed

On 2026-04-18 the fleet hit "Markdown escape hell": legacy `parseMode: Markdown` treats `_` as an italic delimiter, escaping it with `\_` renders the backslash literally, and `chart_uuid` displayed to users as `chart\_uuid` across nearly every technical reply. The fix, documented in `library/lessons/telegram/plain-text-escape-incident.md`, was to default to plain text fleet-wide. Following the old `*bold*`/`_italic_` advice today would reintroduce exactly that bug.

### When rich formatting is genuinely needed

Use **MarkdownV2**, and only in a skill that has been hardened for it — meaning it escapes all 17 special characters (`_ * [ ] ( ) ~ `` ` `` > # + - = | { } . !`). Missing even one causes the message to fail silently. Use this sparingly; plain text is the default because content carries emphasis and formatting is mostly noise.

### Gotchas

- Very long messages (4096+ characters) get truncated — split long output into multiple messages.
- Telegram renders bullets and dashes as plain text, which is fine. Don't reach for headers or tables; they don't render.

---

## 9. Visual Crawl (Designer Bot)

**Shipped skill — `library/skills/visual-crawl/SKILL.md`.**

Autonomous frontend QA: a bot crawls a deployed web app, screenshots each route at mobile/tablet/desktop viewports, checks against design tokens, exercises basic interactions, and files GitHub issues (with screenshot evidence) for findings.

### Why

Frontend QA is tedious and gets skipped. A designer bot checks every page at every viewport systematically and files issues with evidence — run it after a deploy or on a nightly schedule.

### Enable it

Add `visual-crawl` to a designer/QA bot and run `claudlobby generate`. The skill takes `[--url <base-url>] [--auto] [--output github|session]`. The [Designer / Visual QA Bot archetype](bot-archetypes.md) describes a good persona for the bot that runs it (typically an Opus bot doing visual QA across the fleet's frontends).

### Browser automation

The skill needs a way to drive a browser. There is no `library/mcp/` fragment for this — browser control comes from the separate `claude-in-chrome` MCP server/skill (or, as a fallback, Playwright/Puppeteer invoked via Bash). Wire that into the designer bot, not via a `mcp:` library fragment.

### Scheduling

For a nightly or post-deploy run, dispatch through the socket-aware sweep dispatcher (`lib/bot-sweep-cron.sh <designer-session> "/visual-crawl --url https://staging.example.com --auto"`) wired as a fleet job — not a raw tmux keystroke, which would target the wrong server.

### Gotchas

- Browser automation is memory-hungry (Chromium alone is 300-500 MB). Run visual crawls when other bots are idle, or on a host with headroom.
- A full crawl can discover hundreds of routes. Scope it (curated route list, or `--output session` for a dry run) for targeted checks, or let a full crawl run overnight.
- The skill groups related findings into single issues rather than filing N near-duplicates, and skips known/intentional deviations — quality of the design-token reference drives how noisy the findings are.

---

## 10. Multi-Account Setup (fleet.accounts)

Run some bots under a different Claude account — to get more concurrent sessions than one account allows, or to keep work and personal billing separate. This is a **first-class `fleet.yaml` primitive**; you declare accounts once and reference them per bot, rather than hand-editing generated files.

### The fleet.yaml side

Declare the alternate config directories at the fleet level, then point a bot at one with `account:`:

```yaml
fleet:
  accounts:
    default: ~/.claude
    work: ~/.claude-work
  bots:
    work-bot:
      account: work        # → compositor writes CLAUDE_CONFIG_DIR into this bot's bot.conf
```

When a bot's `account` is not `default`, `claudlobby generate` writes `CLAUDE_CONFIG_DIR=<that dir>` into its `bot.conf`; `lib/start-bot.sh` exports it before launching Claude Code, so the bot authenticates, installs plugins, and stores channel state under that directory. You do **not** hand-write `CLAUDE_CONFIG_DIR` into `bot.conf` — that file is generated and the `accounts:` mechanism manages the value. (`TELEGRAM_STATE_DIR` is likewise always derived and emitted for every bot, multi-account or not; it isn't a separate thing you toggle for multi-account setups.)

### The host side (one-time, per account)

The compositor points a bot at a config directory, but it can't authenticate that account for you. Each additional account needs a one-time host setup:

```bash
# Authenticate the second account into its config dir:
CLAUDE_CONFIG_DIR=~/.claude-work claude auth login

# Plugins are per-config-dir — install into the second account too:
CLAUDE_CONFIG_DIR=~/.claude-work claude plugin install <plugin>@<marketplace>
```

If you want globally-installed skills visible to both accounts, symlink them:

```bash
ln -s ~/.claude/skills ~/.claude-work/skills
```

### Gotchas

- Auth, plugins, and (if not symlinked) skills are all per-config-dir. If auth expires or you add a plugin on the default account, repeat the step with `CLAUDE_CONFIG_DIR` set for the other account.
- Symlinked skills are shared both ways — edits to one are seen by both. If you need account-specific skills, use a real directory instead of a symlink.
- Everything else (which bot uses which account, service naming, Telegram state) is driven by `fleet.yaml` + `claudlobby generate`. Keep account membership there, not in hand-edited runtime files.

---

## 11. Finance/Data Pre-Sync Pattern

Pre-fetch slow or rate-limited data before a scheduled briefing so the briefing reads a snapshot instead of making live calls. Unlike most patterns here, this has no shipped equivalent — it's a genuine build-it-yourself template. (Note: `lib/data-sweep.sh` is unrelated — it's a retention job that *purges* old `data/` files, not a pre-fetch cache.)

### Why

Briefings that make live API calls (portfolio data, order totals, analytics) are slow and occasionally fail on rate limits or timeouts. Pre-syncing fetches the data ahead of time and saves a snapshot; the briefing reads the snapshot and completes in seconds.

### The sync script

A plain bash script (no Claude, no MCP — direct API access) that fetches each source and writes a JSON snapshot plus a small metadata file:

```bash
#!/bin/bash
# data-sync.sh — pre-fetch data for the upcoming briefing.
# Source secrets from the bot's .env (this runs outside Claude, so no MCP).
set -euo pipefail
source "$BOT_DIR/.env"                 # FINANCE_API_KEY, SHOPIFY_TOKEN, ...

SYNC_DIR="$BOT_DIR/data/data-sync"
mkdir -p "$SYNC_DIR"

# One block per source; on failure, log it and keep going so a partial
# briefing is still possible.
if DATA=$(curl -sf "https://api.example.com/portfolio" -H "Authorization: Bearer $FINANCE_API_KEY"); then
    printf '%s' "$DATA" > "$SYNC_DIR/portfolio.json"
fi
# ... repeat for orders, weather, etc.

printf '{"timestamp":"%s","files":["portfolio.json"]}\n' "$(date -Iseconds)" > "$SYNC_DIR/sync-meta.json"
```

Write snapshots under the bot's own `data/` directory (bot-owned persistent state), and `chmod 600` anything sensitive (portfolio values, customer orders).

### Telling the bot to use it

In the briefing skill or the bot's `CLAUDE.md`:

```markdown
## Data Pre-Sync

Before composing a briefing, check `data/data-sync/sync-meta.json`. If the sync is
recent (< 1 hour), read the snapshot files instead of making live calls. If it's
stale or missing, fall back to live data via MCP. Always produce a briefing from
whatever data succeeded — a failed source shouldn't block the rest.
```

### Scheduling

Run the sync ~30 minutes before each briefing. Because it's plain bash with no tmux, either a host cron entry or a fleet `jobs:` timer works; prefer a `jobs:` entry if you want the schedule managed alongside the fleet's other timers rather than in a personal crontab. Secrets must be available to whatever runs it — source the bot's `.env` at the top of the script, as above.

### Gotchas

- The script runs outside Claude, so it can't use MCP servers — you need direct API access (tokens, endpoints) for each source.
- Snapshots are overwritten each run, but logs grow — add them to your log rotation.
- If a source fails, the snapshot for it is simply absent; the bot's instructions should treat a missing file as "fall back to live" rather than an error.
