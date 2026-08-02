---
name: setup
description: "Bootstrap a claudlobby fleet from scratch. Checks host dependencies, collects credentials, generates the seed fleet, and spins up claudfather."
argument-hint: "[--check-only]"
---

# Setup

Bootstrap a claudlobby fleet end-to-end. Idempotent — safe to re-run.

## Resume Logic

Before each step, check filesystem state. Skip completed steps:

| Check | Skip to |
|-------|---------|
| `claudlobby.composer` imports | Step 1 |
| All host deps present | Step 2 |
| `.env` exists with `TELEGRAM_TOKEN_CLAUDFATHER` filled in | Step 4 |
| `fleet.yaml` exists | Step 4 (validate + generate) |
| `runtime/bots/claudfather/CLAUDE.md` exists | Step 5 |
| tmux session `claudfather` running | Step 5 (setup-fleet is idempotent: converges timer enrollment, skips the healthy bot), then report success |

If `--check-only` was passed, run Steps 0 and 1 only, then exit.

## Step 0: Is claudlobby itself installed?

**Do this before anything else.** Every later step shells out to `claudlobby`, so a
missing package makes the whole flow fail in confusing ways — and the user may
well be here *because* the documented install did not work for them.

```bash
python3 -c 'import claudlobby.composer' 2>/dev/null && echo INSTALLED || echo MISSING
```

Import `claudlobby.composer`, **not** `claudlobby`. The bare package is a plain
directory at the repo root, so it imports from cwd even when nothing is
installed — a false positive that reports success on a host with no
dependencies at all.

If MISSING, create the repo-local venv and install into it:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

Why a venv rather than `pip install -e .`: Homebrew python (macOS) and Debian /
Raspberry Pi system python are both externally-managed under PEP 668 and refuse
the install outright. Note `python3 -m pip`, not `pip` — Homebrew ships `pip3`
only, so bare `pip` is not a command. `lib/setup-system` does exactly this, and
`claudlobby_cli` prefers `$CLAUDLOBBY_ROOT/.venv`, which is what lets supervised
launchd/systemd runs resolve the CLI without an activated shell.

Then re-check the import before continuing. If it still fails, stop and show the
user the real error — do not proceed into Step 1 on a broken install.

**For the rest of this skill:** if the venv exists but is not activated, invoke
the CLI as `./.venv/bin/claudlobby ...` rather than bare `claudlobby`.

## Step 1: Host Readiness

Check that required tools are installed. Run each check and collect results:

```bash
python3 --version        # need 3.10+
git --version
tmux -V
jq --version
curl --version
node --version           # need 18+
claude --version         # Claude Code CLI
```

Also check that the Telegram plugin is installed:

```bash
claude plugin list 2>/dev/null | grep -q telegram
```

If `lib/setup-system` exists, run `lib/setup-system --dry-run` and parse its output instead of individual checks.

For any missing tool, first offer the one-shot host setup (idempotent — it
also enrolls the default host jobs, e.g. the daily Claude Code update):

```bash
lib/setup-system
```

Or install per-tool if the user prefers:
- macOS: `brew install <pkg>`
- Linux: detect package manager (`apt-get`, `dnf`, `pacman`) and suggest the right command

If `claude` is not authenticated, tell the user:
> Run `! claude auth login` to authenticate (the `!` prefix runs it in this session).

If the Telegram plugin is missing:
> Run `! claude plugin install telegram@claude-plugins-official`

If `--check-only` was passed, report results and stop here.

## Step 2: Collect Credentials

Three values needed. Ask for each interactively:

### 2a. Telegram Bot Token

Walk the user through @BotFather:

1. Open Telegram, search for @BotFather, send `/newbot`
2. Choose a display name (e.g., "My Claudfather")
3. Choose a username ending in `bot` (e.g., `my_claudfather_bot`)
4. Copy the token BotFather gives you
5. Important: in BotFather, go to `/mybots` → select your bot → Bot Settings → Group Privacy → Turn off

When the user provides the token, validate it — and check privacy mode in the same call:

```bash
curl -s "https://api.telegram.org/bot$TOKEN/getMe" \
  | jq '{ok, username: .result.username, can_read_all_group_messages: .result.can_read_all_group_messages}'
```

`ok` must be `true`. Take the bot username from the response for `fleet.yaml`.

**`can_read_all_group_messages` must be `true` for a group bot — verify it, do not just
instruct it.** Step 5 above tells the user to turn Group Privacy off, but telling is not
checking, and this is silent when wrong: the bot boots, the bridge reports ready, it can post
perfectly well, and it simply never sees most messages. The seed sets
`telegram.require_mention: false`, meaning claudfather is expected to answer everything in the
group — which privacy mode makes impossible.

If it is `false`, stop and give the user both fixes:

> Group Privacy is still on, so the bot cannot see normal group messages — only ones that
> @mention it.
> - **Make the bot an admin in the group** — takes effect immediately; admins always see
>   everything regardless of privacy mode. Simplest fix.
> - **Or** BotFather → `/mybots` → your bot → Bot Settings → Group Privacy → Turn off, **then
>   remove and re-add the bot to the group.** The change only applies on re-join, so flipping it
>   without re-adding looks like it worked and changes nothing.

Re-run `getMe` after they act, and only continue once it reports `true` (or the user explicitly
chooses to run mention-only).

### 2b. Telegram Group Chat ID

Tell the user:

1. Create a new Telegram group (or use an existing one)
2. Add your new bot to the group
3. Add @RawDataBot to the group — it will print a message containing the chat ID
4. The chat ID is **negative**. Two valid shapes, both fine:
   - **supergroup / channel** — `-100` prefix, e.g. `-1001234567890`
   - **basic group** — plain negative, e.g. `-5556622542`
5. You can remove @RawDataBot after getting the ID

Validate: a negative integer. **Do not require the `-100` prefix** — that rejects every basic
group, which is what you get by default when you create a group and add a couple of members.

If it is a basic group, tell the user this once and let them decide:

> That's a basic group. It works, but Telegram silently migrates basic groups to supergroups
> (on growth, on adding a username, on some admin actions) and **the chat ID changes** when it
> does — a bot pinned to the old ID stops delivering, with no error. For a supervised bot that
> is worth avoiding up front; converting the group now, or creating it as a supergroup, keeps
> the ID stable.

Once the token is known, confirm the real shape rather than guessing from the digits:

```bash
curl -s "https://api.telegram.org/bot$TOKEN/getChat?chat_id=$CHAT_ID" | jq '.ok, .result.type, .result.id'
```

`type` is `group` (basic) or `supergroup`. A mismatch between the id you were given and
`result.id` means the group already migrated — use `result.id`.

### 2c. Human Telegram User ID

Tell the user:

> Send any message to @userinfobot in Telegram. It replies with your user ID (a number like `1234567890`).

Validate: must be a numeric string.

### 2d. GitHub PAT (Optional)

Ask if the user wants claudfather to interact with GitHub repos. If yes, collect a `ghp_...` token. If no, skip.

## Step 3: Write .env

Write the collected credentials to `.env` at the repo root:

```bash
# Seed fleet credentials
TELEGRAM_TOKEN_CLAUDFATHER=<token>
```

If a GitHub PAT was collected, add:

```bash
GITHUB_PAT=<pat>
```

If `.env` already exists, read it first and only add/update the keys above. Do not overwrite existing values for other keys.

## Step 4: Generate Seed Fleet

1. Copy `fleet.yaml.seed` to `fleet.yaml` at the repo root (if `fleet.yaml` doesn't already exist). This uses root mode — all paths resolve under `runtime/bots/`, matching the getting-started guide.
2. Patch the placeholder values in `fleet.yaml`:
   - Replace `telegram_group_chat_id: "REPLACE_ME"` with the collected group ID
   - Replace `human_telegram_id: "REPLACE_ME"` with the collected user ID
   - Replace `handle: REPLACE_ME` under the claudfather bot with the bot username (from the getMe response)
3. Run `claudlobby validate` — explain any warnings to the user
4. Run `claudlobby generate` — this creates `runtime/bots/claudfather/`

Use `sed` or direct file editing to patch values. Do not rewrite the entire file — preserve comments and formatting.

## Step 5: Apply + Enroll (setup backbone)

```bash
claudlobby warm-cache 2>&1 || true   # pre-download npx MCP packages; non-fatal
lib/setup-fleet                      # root mode: enrolls default fleet timers + spins up claudfather
```

`setup-fleet` replaces the old per-bot spin-up loop with one idempotent
call: it enrolls the composed default jobs (keepalive, fleet-pulse,
reload-fleet, creds-check, log-rotation — opt-in jobs stay dormant), then
spins up every declared bot, skipping bots that are already healthy, so
re-running never restarts a working claudfather. warm-cache stays as a
network prefetch so first boot doesn't pay a cold npx download inside the
readiness window.

After `setup-fleet`, confirm claudfather is alive. **Each bot runs on its own private tmux
server** (`-L <socket>` where the socket is `BOT_SERVICE`), so the default-server check finds
nothing even on a perfectly healthy boot:

```bash
tmux has-session -t claudfather          # WRONG — queries the default server
# error connecting to /private/tmp/tmux-501/default (No such file or directory)
```

**Ask the CLI rather than resolving the socket by hand.** `claudlobby status` already resolves
each bot's socket through the shared resolver (`tmux_socket_for_bot`), so it cannot drift from
what the lib scripts do:

```bash
claudlobby status --bot claudfather
```

Poll up to 90 seconds (matching `start-bot.sh` readiness timeout).

**For inbound, ask `bridge_state` — not the log.** It is the classifier `start-bot.sh` itself
gates readiness on, and it verifies a *live, owned* poller process:

```bash
. lib/lib-common.sh
bridge_state runtime/bots/claudfather     # -> up | no_token | no_handle | down | unknown
```

Only `up` means inbound actually works. Do **not** substitute
`grep BRIDGE_READY .../logs/startup.log`: that file is opened append-only and survives restarts,
so a line from a previous boot reads exactly like a live bridge — the check passes while the bot
is deaf. A tmux session existing is likewise not the same as a bot that can receive messages.

Platform service state, if you want it:

```bash
launchctl print "gui/$(id -u)/<service_prefix>.claudfather" | grep state   # macOS
```

Note `state = not running` here is **not** a failure on its own: `start-bot.sh` launches the
detached tmux server and exits 0, so the launchd job finishing is the normal steady state.
`keepalive` is what revives the session if it dies.

If it doesn't come up, report the failure and check `runtime/bots/claudfather/logs/`.

## Step 6: Success

Print:

> Claudfather is running. Check your Telegram group — it should have posted a ready message.
>
> From here you can talk to claudfather on Telegram to:
> - Add more bots to your fleet
> - Check fleet health with /doctor
> - Ask how claudlobby works
>
> This Claude session can be closed — claudfather lives on independently.
