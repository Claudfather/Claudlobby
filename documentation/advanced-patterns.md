# Advanced Patterns

Patterns that extend a running claudlobby fleet beyond basic dispatch and briefings. Each section is self-contained — implement whichever ones fit your setup.

Prerequisites: a working fleet with at least a manager bot and one worker, supervised services (systemd user units on Linux, launchd LaunchAgents on macOS), and the shared `lib/` scripts. See [getting-started](getting-started.md) if you're not there yet.

Two mechanics run through most of these patterns. Read them once here:

- **Every bot runs on its own tmux server.** A bot's session lives on a *private* tmux server addressed by `-L <socket>`, where the socket name is the bot's `BOT_SERVICE` (also written to `bot.conf` as `TMUX_SOCKET`). One server's death drops only that bot, never the fleet. Use the canonical task, assignment, message, and report commands for work; Section 5 covers that model. Legacy lifecycle scripts that inspect or restart a pane must address its private socket.

- **Several patterns below ship as library skills, not recipes.** Where a pattern is a real skill, add its name to a bot's `skills:` list in `fleet.yaml`, stage the source with `config plan`, and activate the reviewed plan. The compositor links `library/skills/<name>/` into that bot's `.claude/skills/<name>/`. Never hand-author a skill in a generated bot directory. For those patterns, this doc gives the *why*, the wiring and scheduling; the skill file itself owns the steps.

---

## 1. Lifecycle Orchestration (/lifecycle)

**Shipped skill — `library/skills/lifecycle/SKILL.md`.**

A full development pipeline run by the manager bot: dispatch an engineer to implement, dispatch a reviewer, route the review (merge / send back for mechanical fixes / flag a human on ambiguous concerns), then run a retro and file follow-up issues — pulling a human in only when there's a real judgment call.

### Why

Without this, you manually dispatch the engineer, wait, dispatch the reviewer, wait, read the review, decide, merge, and forget to capture learnings. `/lifecycle` automates the chain and only interrupts you when a human decision is genuinely needed.

### Enable it

Add `lifecycle` to the manager bot's `skills:`, then stage and activate the reviewed configuration:

```yaml
bots:
  lead:
    skills: [dispatch, lifecycle, ...]
```

The example fleet already wires this on its `lead` bot. The phase-by-phase decision table (approve → merge, mechanical → send back, ambiguous → flag human, 3+ cycles → flag human) lives in `library/skills/lifecycle/SKILL.md`; that file is the source of truth, so it isn't restated here.

### How it dispatches

Lifecycle hands tracked work to the engineer and reviewer through fleet-owned task admission, assignment, and delivery. Workers accept their exact assignment and report linked progress or completion; the manager reads fleet reports. See Section 5 for the commands.

### Gotchas

- Read `fleet reports list` or the fleet inbox for recorded results; a committed report and manager notification are distinct outcomes.
- "Mechanical fix" vs "ambiguous concern" is a judgment the manager makes by reading the review. Bias toward flagging a human early — a false escalation is cheaper than a bad merge.

---

## 2. Alert Sweep (/data-alert-sweep)

**Shipped skill — `library/skills/data-alert-sweep/SKILL.md`.**

Batch-process alerts from a monitoring channel: pull recent alerts, read each thread, check whether a PR already exists, investigate independently, then approve the existing fix, comment with a differing conclusion, or open a new PR — never a competing one.

### Why

Monitoring channels (Slack, Datadog, etc.) accumulate alerts faster than anyone triages them. This pattern works through them in bulk and, critically, refuses to create a second PR for a problem someone already has a PR for.

### Enable it

Add `data-alert-sweep` to the bot that owns the monitoring integrations (typically the manager, or a business bot with Slack + GitHub MCP), then stage the source with `config plan` and activate the reviewed plan with `host activate`. The skill's core principle — *always investigate independently; existing PRs and team comments are inputs, not conclusions; never open a competing PR* — is defined in the skill file.

### Scheduling

To run it on a cadence, declare the fleet job in `fleet.yaml`, stage it with `config plan`, and activate it with `host activate` (see [fleet-yaml-schema](fleet-yaml-schema.md)). The private scheduled dispatcher handles the bot's socket and busy-pane check; do not call it as a public operation or add a personal crontab.

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

Add `triage` to a bot with the right integrations — the manager, or a business bot with Gmail + Notion + Shopify MCP — then stage and activate the reviewed source configuration. The source-to-enrichment mapping and the dedup-before-create rule are in the skill file.

### Scheduling

Same as the alert sweep: declare and activate the fleet job so its composed timer owns the schedule and private delivery path.

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

- **In-session restart (the `restart` skill).** When a bot restarts itself — `/restart`, or `/restart --auto` from an automated caller — the skill captures the handoff in that session before requesting the supervised restart. The public lifecycle route is `claudlobby bot restart BOT`; the skill owns the in-session sequence. See `library/skills/restart/SKILL.md`.

- **External restarter (`lib/pre-stop-handoff.sh`).** When something *outside* the session bounces the bot and can't invoke a skill directly, it calls `lib/pre-stop-handoff.sh <bot-dir>` first. The canonical caller is `lib/weekly-worker-restart.sh`, which bounces worker bots weekly to pick up a staged Claude Code binary; it runs the handoff, then `spin-up-bot.sh`.

### What `pre-stop-handoff.sh` actually does

Given a bot directory, the script:

1. Loads the bot's config and resolves its tmux session name and **private socket** (`tmux_socket_for_bot`) — the handoff is sent on the bot's own server, not the default one.
2. Checks `<bot-dir>/.claude/session.md` (the handoff artifact — written by whatever session-handoff capability is installed, or by the `restart` skill itself when none is). If a handoff was written in the last 5 minutes, it's fresh enough — skip and exit.
3. Otherwise, if the session is live **and a session-handoff capability resolves**, sends the configured handoff command and waits up to ~30 seconds for a new `session.md` to land. If no such capability is installed it skips the send, says so on stderr (the `ExecStop` journal) and emits `handoff_skipped` — a silent skip here would lose the handoff at the one moment it matters.
4. Always exits 0. The handoff is best-effort and never blocks the restart: a timed-out or unreachable bot just proceeds to the bounce. (It also cleans up the transient `.tmux-env` secrets file on every exit path.)

### Resume on the next start

Resume needs no separate wiring in the common case. `lib/start-bot.sh` injects the configured session-resume command as the new session's first keystroke, behind two gates: the checkpoint must be fresh (~24h, else clean-start) **and** the command must actually resolve. The command is configuration — `SESSION_RESUME_COMMAND`, set empty to disable — so a fleet running without a session plugin is not fed an unresolvable keystroke on every boot. When either gate closes, `start-bot.sh` logs `RESUME SKIP` with the reason and emits `resume_skipped`. This does not depend on the bot's `STARTUP_PROMPT` carrying a resume instruction.

### Gotchas

- **Don't add `ExecStop=.../pre-stop-handoff.sh` to a bot's `.service` file.** Bot units are generated by reviewed configuration activation (the file header says *do not hand-edit*). The handoff belongs at the restarter, per the two entry points above.
- Best-effort by design: if a bot is deeply stuck (unresponsive MCP, tight loop), the handoff times out and the restart proceeds anyway. That's the intended behavior.
- The `--auto` flag matters — it runs the handoff non-interactively so the bot doesn't stop to ask for confirmation.

---

## 5. Inter-Bot Communication and Reports

Use `/fleet-ops` for the current Task, assignment, message, and report
contracts. Fleet work is admitted before it is assigned; assignment is not
delivery, and delivery is not worker acceptance. Retain a distinct request UUID
for each operation and inspect recording and notification separately.

```bash
# Manager: admit, assign, then deliver the exact assignment from a UTF-8 file.
claudlobby --json task admit --title "Implement X" --request-id ADMIT_UUID
claudlobby --json task assign TASK_ID --bot WORKER --request-id ASSIGN_UUID
claudlobby --json assignment deliver ASSIGNMENT_ID --file INSTRUCTIONS_FILE --request-id DELIVER_UUID

# Worker: accept, then report against that current assignment.
claudlobby --json assignment accept ASSIGNMENT_ID --request-id ACCEPT_UUID
claudlobby --json assignment progress ASSIGNMENT_ID --summary "Refactoring auth" --percent 40 --request-id PROGRESS_UUID
claudlobby --json assignment complete ASSIGNMENT_ID --summary "Rate limit landed" --pr https://github.com/org/api/pull/87 --pr-role authored --request-id COMPLETE_UUID
```

A blocker that leaves the worker owning the assignment uses `assignment
block --reason`; a worker yielding it uses `assignment return --reason`.
Terminal unsuccessful work uses `assignment fail --reason`. With no current
assignment, use an explicitly unlinked `fleet reports submit --status STATUS
--summary "..." --request-id UUID`; that report does not transition a task.
No report may borrow a historical display task ID as an assignment ID.

Read reports with `claudlobby --json fleet reports list`, optionally filtering
with `--bot`, `--status`, or `--since RFC3339_CUTOFF`. The cutoff must be a
real RFC3339 instant with an offset, derived for the intended window. Inspect
`ok` and follow `data.next_cursor` with `--cursor` until null. An error or
unreadable page is unknown, not zero reports. The manager can use `fleet inbox`
for a concise view; an unfiltered `--unacknowledged` report page supplies a
separate ACK cursor. See `/fleet-ops` for that exact acknowledgement protocol.
A request replay is for the same intended operation and never automatically
resends an uncertain message.

---

## 6. Git Pull Scheduler

Keep cloned repos fresh so bots aren't creating PRs against stale code. The selected bot's public one-shot route is `claudlobby --fleet FLEET host repos pull --bot BOT`.

### What the operation does

For an explicit operator request against one declared bot, run
`claudlobby --fleet FLEET host repos pull --bot BOT`. The result names each
repository as updated, unchanged, skipped (dirty or redirected), or failed. It never chooses a
generic directory or grants this source mutation to a bot by default.

It checks for local changes, then runs `git pull --ff-only` on each clean immediate Git repository, logging results to `git-pull.log` **one level above** the target dir. A dirty repo is skipped; a branch that diverged from upstream fails without a merge commit. It does **not** inspect the branch name or skip non-`main` repos.

The CLI targets a selected bot's projects directory and refuses an undeclared bot. The private owner may have other callers; those are not a public generic-directory API.

### Scheduling

The old arbitrary-directory cron recipe has no public CLI equivalent. For a
one-shot selected bot, use `host repos pull --bot BOT`; configure scheduled
work through the supported authored job and activation path.

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

Stage the `fleet.sweep` source with `config plan` and activate the reviewed plan with `host activate`; activation owns timer enrollment on Linux and macOS. Full field reference and the emitted observability events (`audit_selected`, `audit_dispatched`, `audit_completed`, …) are in [fleet-yaml-schema](fleet-yaml-schema.md).

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

Add `visual-crawl` to a designer/QA bot, then stage and activate the reviewed configuration. The skill takes `[--url <base-url>] [--auto] [--output github|session]`. The [Designer / Visual QA Bot archetype](bot-archetypes.md) describes a good persona for the bot that runs it (typically an Opus bot doing visual QA across the fleet's frontends).

### Browser automation

The skill needs a way to drive a browser. There is no `library/mcp/` fragment for this — browser control comes from the separate `claude-in-chrome` MCP server/skill (or, as a fallback, Playwright/Puppeteer invoked via Bash). Wire that into the designer bot, not via a `mcp:` library fragment.

### Scheduling

For a nightly or post-deploy run, declare the selected bot's fleet job and activate the reviewed plan. The private dispatcher handles its socket; it is not a public scheduling command.

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

Pre-fetch slow or rate-limited data before a scheduled briefing so the briefing reads a snapshot instead of making live calls. Unlike most patterns here, this has no shipped equivalent — it's a genuine build-it-yourself template. (Note: `lib/data-sweep.sh` is unrelated — it's a retention job that *purges* old ephemeral `data/` files, not a pre-fetch cache.)

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
