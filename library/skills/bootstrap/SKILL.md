---
name: bootstrap
description: "Guided fleet creation. Resume-aware — checks filesystem state and picks up where the user left off."
argument-hint: "[<fleet-name>]"
---

# Bootstrap

Walk the user through creating and activating a fleet. Resume-aware: inspect the
authored source, sealed release and selected activation before starting. Keep
the data root outside the source checkout. Use the exact release CLI returned
by `host setup`; a fresh host does not have one yet.

## State Assessment

Before starting, check what already exists. Assign each step a status:

| Step | Check | NOT_STARTED | PARTIAL | COMPLETE |
|------|-------|-------------|---------|----------|
| Host release | `host releases` from the bootstrap CLI when installed | No sealed release | Release exists but is not selected | Exact sealed CLI and release ID known |
| Fleet source | Authored manifest and `$DATA/local/<fleet>/fleet.yaml` | No source | Draft or differing source | Intended source is explicit |
| Credentials | Check `$DATA/local/<fleet>/.env` locally without printing values | No .env | Placeholders remain | Required values supplied |
| Activation | `"$RELEASE_CLI" --root "$DATA" host status` | No selected release | Pending activation | Selected activation is active |
| Runtime | `"$RELEASE_CLI" --root "$DATA" --fleet <fleet> fleet status` | Not selected | Native or session state unknown | Exact bots observed running |

**Placeholder detection:** a value is a placeholder if it contains `xxxx`, `AAAA`, `your_token_here`, `REPLACE`, `ghp_xxxxxxxxxxxxxxxxxxxx`, or `8888888:AAAAAAAAAAAAAAAAAAAA`. Report the first incomplete step and offer to resume from there.

If an argument is provided, use it as the fleet name. Otherwise ask.

## Step 1: Fleet Identity

Ask the user:

1. **Fleet name** — lowercase, hyphenated (e.g., `my-fleet`). Used as the directory name under `local/`.
2. **Service prefix** — reverse-domain format (e.g., `com.myname.fleet`). Used for systemd/launchd unit names.

Choose an absolute `$DATA` directory outside the checkout. For a first fleet,
copy the packaged `fleet.yaml.seed` to an authoring file outside `$DATA` and
edit its fleet name, service prefix and bots. Follow
[`documentation/getting-started.md`](../../../documentation/getting-started.md)
for the wheel, dependency lock, wheelhouse and seed paths. `fleet.yaml.example`
is a field reference, not the minimal first-fleet template.

## Step 2: Define Bots

For each bot the user wants to create, collect:

1. **What should this bot do?** — Map to expertise areas. Show available options:
   ```bash
   ls library/expertise/ | sed 's/.md$//'
   ```
2. **Pick a personality** — Show available voices:
   ```bash
   ls voices/ | sed 's/.md$//'
   ```
   Offer to show a preview (`head -5 voices/<name>.md`). Optional.
3. **What repos should it work on?** — GitHub org + repo list for scope.
4. **Model** — Explain the trade-offs: Opus (most capable, highest cost), Sonnet (balanced), Haiku (fastest, cheapest). Default: Sonnet.
5. **Timezone** — Ask: "What timezone are you in? e.g., America/New_York, Europe/London, Asia/Tokyo". Used for human-friendly timestamps in bot output. Set as `env: { TZ: "<value>" }` in the bot's fleet.yaml stanza.

Edit the authoring manifest for the initial bots. After the fleet is active,
`bot create` can author another bot in `$DATA/local/<fleet-name>/fleet.yaml`;
stage and activate that source edit separately:

```bash
"$RELEASE_CLI" --root "$DATA" --fleet <name> bot create \
  --name <bot-name> \
  --expertise <areas> \
  --model <model> \
  --telegram-handle <handle> \
  --yes
```

Explain each concept as it comes up. First-time users won't know what guardrails or protocols are — give a one-sentence explanation and suggest sensible defaults.

## Step 3: Telegram Setup

For each bot that needs a Telegram presence:

1. Guide the user through @BotFather:
   - Send `/newbot` to @BotFather
   - Choose a display name
   - Choose a username ending in `bot`
   - Copy the token
   - Go to `/mybots` → select bot → Bot Settings → Group Privacy → Turn off
2. Collect the token
3. If the fleet doesn't have a Telegram group yet:
   - Create a group, add all bots to it
   - Add @RawDataBot to get the chat ID (starts with `-100`)
   - Remove @RawDataBot after
4. If the fleet doesn't have the human's user ID:
   - Send any message to @userinfobot

Create `$DATA/local/<fleet-name>/` before writing the private fleet-tier
credentials file. Write values to `.env` without printing them:

```bash
TELEGRAM_TOKEN_<BOT_UPPER>=<token>
```

Patch `fleet.yaml` with the group chat ID, human Telegram ID, and timezone (if collected). The timezone goes in each bot's `env:` block as `TZ: "<value>"`.

## Step 4: Seal and activate

If no sealed release exists, run `host setup --json` with the candidate wheel, locked
dependency wheelhouse and bootstrap interpreter as shown in
[`documentation/getting-started.md`](../../../documentation/getting-started.md).
Read `data.cli` and `data.release_id` from its result; set `RELEASE_CLI` to that
reported path. Do not guess an installed CLI or write generated runtime files
from the checkout.

For a first fleet, run the sealed CLI from the operator's user-manager domain:

```bash
"$RELEASE_CLI" --root "$DATA" --fleet <fleet-name> fleet setup \
  --config "$AUTHORING_FLEET_YAML" --install-directory "$USER_UNIT_DIR"
```

`fleet setup` copies the authored manifest, validates and stages all declared
host fleets, then activates the plan. An already present *different* destination
manifest needs an explicit `--replace-config` decision. If activation reports
a pending step, diagnose that journal before another attempt.

For later edits to `$DATA/local/<fleet-name>/fleet.yaml`, use the selected
release ID and review the staged host-wide plan before activation:

```bash
"$RELEASE_CLI" --root "$DATA" --fleet <fleet-name> config validate
"$RELEASE_CLI" --root "$DATA" config plan --release <RELEASE_ID>
"$RELEASE_CLI" --root "$DATA" config diff <PLAN_ID>
"$RELEASE_CLI" --root "$DATA" host activate <PLAN_ID> --install-directory "$USER_UNIT_DIR"
```

If validation fails, read the error and help fix it. Common issues:
- Missing expertise file → suggest an existing one or explain how to create a custom one
- Missing env var → guide the user to add it to `.env`
- Invalid YAML → identify the syntax issue

## Step 5: Warm cache and verify

```bash
"$RELEASE_CLI" --root "$DATA" --fleet <fleet-name> host cache warm
```

`fleet setup` or `host activate` owns enrollment and bot starts. Check recorded
selection and observed bot state before claiming that the fleet is running:

```bash
"$RELEASE_CLI" --root "$DATA" host status
"$RELEASE_CLI" --root "$DATA" --fleet <fleet-name> fleet status
```

If a bot fails to start, check:
- Service logs: `journalctl --user -u <bot> -n 20` (Linux) or `tail local/<fleet-name>/runtime/bots/<bot>/logs/*.log` (macOS)
- Token validity: `curl -s "https://api.telegram.org/bot$TOKEN/getMe" | jq .ok`

## Step 6: Verify + Next Steps

Confirm all bots are alive on Telegram. Suggest:

- Send a test message in the Telegram group
- Run `host doctor` to see host and fleet checks
- Read `documentation/getting-started.md` for the sealed-release bootstrap and
  staged configuration workflow

Remind the user:

> Your `$DATA/local/<fleet>/` directory, including `.env`, is not committed to
> the source checkout. Back it up separately.

## Rules

- Never skip credential validation. A bad token wastes 10 minutes of debugging later.
- Don't rush. Explain concepts as they come up — the user is learning claudlobby.
- If a step fails, diagnose before retrying. Read the error output.
- Keep authoring files separate from the data root until `fleet setup` copies
  them. For later edits, change the selected fleet source and activate a plan.
