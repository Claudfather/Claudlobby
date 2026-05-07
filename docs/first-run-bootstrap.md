# First-Run Bootstrap

The zero-to-ripping walkthrough. Assumes you've cloned this repo and want to get a fleet running today.

## What this repo gives you (and doesn't)

**Ships in this repo:**
- `bot-common/` — shared lifecycle scripts (start / keepalive / report-back / fleet-state / tg-post / bootstrap-bot / sprint-trigger)
- `manager/` — canonical manager scaffold with 11 orchestration skills
- `examples/worker/` — worker template with Lifecycle Protocol, Context Mgmt, Telegram rules
- `examples/global-skills/` — skills that need to live in `~/.claude/skills/` globally (`mission`, `autonomous-sprint`)
- `examples/optional-personal-skills/` — personal-assistant flavored examples (briefing, triage) for an always-on life bot
- `docs/` — setup guide (Pi), integrations, advanced patterns

**You install separately:**
- clauDNA or equivalent (`<your-clauDNA-repo>`) — the global skills (~50) + agents + hooks that land in `~/.claude/`. This is the single biggest unlock for why the fleet "rips" — skills like `/simplify`, `/review-pr`, `/review-changes`, `/tech-debt`, `/session-handoff`, `/worktree`, `/development-retro` come from here.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — the CLI itself.
- [Telegram channel plugin](https://github.com/anthropics/claude-plugins-official) — `claude plugin install telegram@claude-plugins-official`.
- Your API tokens (GitHub, Notion, Slack, etc).
- Your BotFather tokens (one per bot).

---

## Prerequisites

```bash
# macOS (via Homebrew)
brew install bun uv tmux jq gh
npm install -g @anthropic-ai/claude-code

# Linux (via apt / NodeSource)
sudo apt install -y tmux jq
curl -fsSL https://bun.sh/install | bash
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list
sudo apt update && sudo apt install -y gh
npm install -g @anthropic-ai/claude-code

# Both
claude /login          # OAuth — or export ANTHROPIC_API_KEY
gh auth login          # --with-token <<< "$GITHUB_PAT" for headless
```

### macOS-specific gotcha

Claude Code stores OAuth in the **login keychain**. SSH sessions can't unlock it. LaunchAgents run inside the GUI Aqua session which CAN. So:

1. **Enable auto-login** for your user: System Settings → Users & Groups → Automatic Login.
2. **Disable screen lock password**: System Settings → Lock Screen → "Require password after..." = Never.

This keeps the Aqua session alive after boot so the login keychain stays unlocked for LaunchAgents.

---

## Step 1 — Clone this repo to your fleet host

```bash
git clone https://github.com/<your-username>/claudlobby.git ~/claudlobby
cd ~/claudlobby
```

## Step 2 — Install clauDNA (global skills + hooks + agents)

```bash
git clone <your-clauDNA-repo> /tmp/clauDNA
cd /tmp/clauDNA && ./install.sh
```

Verify:
```bash
ls ~/.claude/skills/ | wc -l   # should be ~50
ls ~/.claude/agents/ | wc -l   # should be ~8
ls ~/.claude/hooks/  | wc -l   # should be ~4
```

## Step 3 — Install the Telegram channel plugin

```bash
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin install telegram@claude-plugins-official
claude plugin list   # should show telegram enabled
```

## Step 4 — Install optional global skills from this repo

Only if you use `/autonomous-sprint`, which requires `/mission`:

```bash
cp -r examples/global-skills/mission ~/.claude/skills/
cp -r examples/global-skills/autonomous-sprint ~/.claude/skills/
```

## Step 5 — Bootstrap your first bot (the manager)

```bash
# Creates ~/claudlobby/<name>/, ~/.claude/channels/telegram-<name>/, seeds trust
~/claudlobby/bot-common/bootstrap-bot.sh <manager-name> manager \
  --telegram-token "<BOT_TOKEN_FROM_BOTFATHER>" \
  --group-chat-id "<GROUP_CHAT_ID>"
```

Edit the scaffolded files:
- `~/claudlobby/<manager-name>/bot.conf` — fill in `TELEGRAM_GROUP_CHAT_ID`, `FLEET_ORG`
- `~/claudlobby/<manager-name>/CLAUDE.md` — persona, fleet roster (after workers exist), scope rules
- `~/claudlobby/<manager-name>/.mcp.json` — tokens for GitHub, Notion, Slack

Install the service unit:

**macOS (launchd)** — see `docs/pi-setup-guide.md` for the plist template; load with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

**Linux (systemd)** — `sudo cp examples/bot.service /etc/systemd/system/<manager-name>.service`, edit paths, then `sudo systemctl daemon-reload && sudo systemctl enable --now <manager-name>`.

## Step 6 — Pair the human

DM the manager bot on Telegram with any message. The bot replies with a pairing code. Inside your manager's tmux session (attach with `tmux attach -t <manager-name>`):

```
/telegram:access pair <code>
```

Or edit `~/.claude/channels/telegram-<manager-name>/access.json` directly — move the pending senderId into `allowFrom`, flip `dmPolicy` to `"allowlist"`.

Test: DM the bot again. It should respond normally.

## Step 7 — Add the manager to your group chat

1. Create a Telegram group chat.
2. @BotFather → `/setprivacy` → pick your manager bot → **Disable** (so it can read all group messages).
3. Add the bot to the group.
4. Promote the bot to admin.
5. Send any message in the group.
6. Find the group chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | jq '.result[].message.chat'
   ```
7. Add the group to `access.json` under `groups` with `requireMention: false` (manager sees all).

## Step 8 — Bootstrap workers

For each worker:

```bash
~/claudlobby/bot-common/bootstrap-bot.sh <worker-name> worker \
  --telegram-token "<BOT_TOKEN>" \
  --group-chat-id "<GROUP_CHAT_ID>"
```

Workers default to `requireMention: true` — they only respond when `@<handle>` mentioned in the group.

Customize `~/claudlobby/<worker>/CLAUDE.md` for the role (engineer / reviewer / designer). Fill in MCP tokens.

Pair each worker (Step 6), install service unit, start.

## Step 9 — Update the manager's CLAUDE.md with the fleet

Now that workers exist, fill in the "Fleet You Manage" table in the manager's CLAUDE.md. Add a Dispatch Routing section (which repos / task types go to which worker).

## Step 10 — First dispatch

DM the manager:
> "Dispatch <worker-name> to do a quick tree-walk of the <repo> repo and report what's there."

If the whole loop works — manager dispatches via tmux, worker acknowledges in Telegram, worker executes, worker reports back — you're live.

## Step 11 — Autonomous operation (optional)

Schedule `sprint-trigger.sh` to fire N times per day:

**Linux (cron)**:
```
15 6,12,18,0 * * * ~/claudlobby/bot-common/sprint-trigger.sh
```

**macOS (launchd)** — see `examples/bot.service` → plist equivalent; use `StartCalendarInterval` with the same hours.

Requires: a `PROJECT_MISSION.md` in at least one target repo (bootstrap with `/mission --bootstrap`).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Not logged in" after launchd/systemd start | (macOS) keychain locked | Auto-login + unlocked screen |
| `tmux new-session` creates session that dies instantly | Claude CLI in non-TTY fallback | Pin `CLAUDE=/opt/homebrew/bin/claude` in `start-bot.sh` (avoid native-install binary in tmux contexts) |
| First launch hangs on "Trust this folder?" | Workspace trust not seeded | `bootstrap-bot.sh` seeds `~/.claude.json` automatically; if you skip it, accept once interactively |
| `**bold**` shows as literal `**` in Telegram | Missing `parseMode: "Markdown"` | See `manager/.claude/skills/_telegram-formatting.md` + worker CLAUDE.md |
| Worker silently drops after long `[FLEET NOTICE]` paste | Very long tmux send-keys payload tripped something | Break long messages into chunks, or write to a file the bot reads |
| Bot's Telegram plugin shows no `bot.pid` | Plugin init flaked | `launchctl kickstart -k gui/$(id -u)/<label>` (macOS) or `systemctl restart <name>` (Linux) |

---

## What's in each directory

```
claudlobby/
├── bot-common/                   Shared scripts, OS-agnostic core
│   ├── start-bot.sh              Launches the tmux + claude session
│   ├── keepalive.sh              Periodic health check / nudge
│   ├── report-back.sh            Worker → manager tmux reporting
│   ├── tg-post.sh                Bash → Telegram API (parse_mode=Markdown baked in)
│   ├── fleet-state.json          Central state ledger
│   ├── fleet-state-update.sh     Updater called by start-bot + report-back
│   ├── sprint-trigger.sh         Schedule-driven /autonomous-sprint nudger
│   └── bootstrap-bot.sh          One-shot per-bot scaffolder
│
├── manager/                      Canonical manager scaffold — copy to your fleet
│   ├── bot.conf                  (template w/ <PLACEHOLDERS>)
│   ├── CLAUDE.md                 Generic manager persona + orchestration rules
│   ├── .mcp.json.template        GitHub + Notion + Slack stubs
│   └── .claude/
│       ├── settings.local.json
│       └── skills/               11 orchestration skills:
│           ├── dispatch/         Structured task dispatch
│           ├── fleet-status/     Fleet health dashboard
│           ├── lifecycle/        implement → review → merge → retro
│           ├── autonomous-sprint/ Mission-driven autonomous cycle
│           ├── data-alert-sweep/ Batch-triage monitoring alerts
│           ├── deploy-status/    Multi-platform deploy health
│           ├── prs/              Cross-repo PR overview
│           ├── restart/          Graceful handoff + self-restart
│           ├── sweep/            Periodic work sweeper
│           ├── status/           Self-diagnostic
│           └── _telegram-formatting.md   Markdown/MarkdownV2 cheatsheet
│
├── examples/
│   ├── worker/                   Worker template (role-agnostic)
│   │   ├── bot.conf
│   │   ├── CLAUDE.md             Full pattern set: Lifecycle, Context, Comms
│   │   ├── .mcp.json.template
│   │   └── .claude/
│   │       ├── settings.local.json
│   │       └── skills/visual-crawl/
│   ├── global-skills/            Ship to ~/.claude/skills/ globally
│   │   ├── mission/
│   │   └── autonomous-sprint/
│   ├── optional-personal-skills/ Personal-life examples (briefing, triage)
│   ├── optional-personal-scripts/  (evening-audit, finance-presync)
│   ├── scripts/                  Generic ops scripts
│   ├── access.json               Telegram access template
│   └── bot.service               Systemd unit template
│
└── docs/
    ├── first-run-bootstrap.md    ← you are here
    ├── pi-setup-guide.md
    ├── integrations.md
    ├── notion-integration.md
    ├── advanced-patterns.md
    └── bot-archetypes.md
```
