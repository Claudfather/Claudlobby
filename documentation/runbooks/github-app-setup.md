# GitHub App installation-token setup

End-to-end guide for giving a fleet its own **GitHub App identity** instead of a shared
personal PAT. The bot commits, pushes, and calls the API as `<slug>[bot]` — an identity
independent of any human account, so it can be held to branch protection **even when the
human operating it is an org admin**, and a leaked token dies in about an hour instead of
living until someone rotates it.

This is App-auth (epic #1270). It is **opt-in and dormant**: a fleet that declares no
`github_app:` composes exactly as before.

## Why installation tokens

- A personal PAT scopes to one human's repo access — it misses org repos that human is not
  on, and it rotates when they rotate it. Every bot action reads as that human.
- A GitHub App installs on the orgs/repos it needs. Its **installation tokens** are stable
  across team changes, scoped to the install, and short-lived (~1 hour). The fleet mints a
  fresh one at use time; nothing long-lived sits in `.env`.
- Because the bot is a distinct identity, a branch-protection ruleset can bind it
  specifically — the admin's bypass does not extend to the App.

## Prerequisites

- `openssl`, `curl`, `jq` on the host. **`openssl` is the one new prerequisite** App-auth
  adds (it signs the App JWT — no Python/PyJWT install); it ships preinstalled on macOS and
  Raspberry Pi OS. `git`, `python3`, `node` are already fleet prerequisites.
- A host already brought up per [`mac-mini-setup-guide.md`](mac-mini-setup-guide.md) or
  [`pi-setup-guide.md`](pi-setup-guide.md).
- Admin access to the GitHub org to create the App and set branch protection.

## Step 1 — create the GitHub App

Go to `https://github.com/organizations/<org>/settings/apps/new` and set:

| Field | Value |
|---|---|
| GitHub App name | A globally-unique slug, e.g. `my-fleet-bot`. This becomes the bot identity `my-fleet-bot[bot]`. |
| Homepage URL | Anything, e.g. `https://example.com`. |
| Webhook | **Uncheck Active** — this App vends tokens, it does not receive events. |

**Repository permissions** (least privilege for a code-contributing bot):

| Permission | Level | Why |
|---|---|---|
| Contents | Read and write | push branches, read files |
| Pull requests | Read and write | open PRs, comment, review |
| Issues | Read and write | file and update issues |
| Metadata | Read-only | mandatory, auto-selected |

Grant nothing else unless a fleet workflow needs it. Then:

1. **Generate a private key** (App settings → Private keys → Generate). A `.pem` downloads.
   Treat it as the crown jewel — it mints tokens indefinitely and never expires.
2. **Install the App** on the target org (App settings → Install App → pick repos or *All
   repositories* per your policy). Installing does not change the Installation ID later.
3. **Record three values** — you will hand them to the setup script:
   - **App ID** — the numeric value at the top of the App settings page (NOT the Client ID
     `Iv23li…` below it).
   - **Installation ID** — `https://github.com/organizations/<org>/settings/installations`
     → **Configure** next to the App → the URL ends in `/installations/<INSTALLATION_ID>`.
   - the **private-key `.pem`** path once it is on the host (Step 3).

## Step 2 — branch protection on bot-accessible repos

This is the layer that mechanism-enforces "the bot cannot push to main" and "the bot cannot
merge its own unreviewed PR", independent of the operator's admin powers. On each repo the
App can write (Settings → Rules → Rulesets → New branch ruleset), or import
[`../examples/prevent-main-push-ruleset.json`](../examples/prevent-main-push-ruleset.json)
and adjust:

- Target: the default branch.
- Rules: restrict deletion, restrict non-fast-forward, require a pull request with **at
  least 1 approving review**.
- **Bypass list: do NOT add the App.** Leave the admin team there for emergencies if you
  must, but keeping `<slug>[bot]` out of bypass is the whole point — the bot is then subject
  to every rule even while humans can override.

## Step 3 — private key + config on the host

Copy the `.pem` to the host, tighten it, and run the setup script. The script writes NO git
config and installs nothing on your PATH (the compositor owns per-bot git routing) — it
**validates the credentials end to end through the real mint path**, writes the operator/cron
config file, and prints the exact fleet values to wire.

```bash
# On the host, with the key already copied to e.g. ~/.config/claudlobby/my-app.pem
chmod 600 ~/.config/claudlobby/my-app.pem

lib/setup-github-app.sh \
  --app-id 1234567 \
  --installation-id 7654321 \
  --private-key ~/.config/claudlobby/my-app.pem \
  --slug my-fleet-bot
```

On success it prints the fleet-`.env` names, the `fleet.yaml` `github_app:` snippet, and the
bot commit identity. On a 401 it prints a troubleshooting tree (wrong key vs wrong App ID vs
revoked key vs clock skew) with a key-fingerprint recipe.

## Step 4 — wire the fleet

1. **Fleet `.env`** (`local/<fleet>/.env`) — names are the contract, values stay out of git:

   ```
   GITHUB_APP_ID=1234567
   GITHUB_APP_INSTALLATION_ID=7654321
   GITHUB_APP_PRIVATE_KEY_PATH=/home/you/.config/claudlobby/my-app.pem
   ```

2. **`fleet.yaml`** — git auth routing and the commit identity:

   ```yaml
   fleet:
     defaults:
       github_app:
         slug: my-fleet-bot
         bot_user_id: 12345678      # the App's BOT USER id, printed by the setup script
         # orgs: [MyOrg]            # optional: route only these orgs via the App
   ```

3. **MCP** — the App-token GitHub server: add `github-app` to a bot's `mcp:` list
   (`mcp: [github-app]`). See [`../../library/integrations/github-app.md`](../../library/integrations/github-app.md).

4. `claudlobby --fleet <fleet> generate`, then restart the bots (composed `.gitconfig`,
   `bot.conf`, and the `tools/gh` shim reach a running bot only at its next restart).

## Step 5 — verify (the proofs that matter)

Run these on the host, as the bot would. `git` and `gh` on an App bot resolve the App
identity automatically (the composed gitconfig routes credentials through the App helper; the
`tools/gh` shim mints per call).

```bash
# 1. The bot can push a branch to a repo the App is installed on.
#    (in a checkout under the bot's projects/, on a NON-protected branch)
git commit --allow-empty -m "app-auth smoke test"
git push origin HEAD:app-auth-smoke      # succeeds silently — helper minted a ghs_ token

# 2. Scoping: a repo NOT in the App's installation is refused.
git ls-remote https://github.com/<org>/<repo-the-app-cannot-see>.git   # 404 / auth failure

# 3. Branch protection blocks the bot from main — even for an admin operator.
git push origin HEAD:main
# Expected: GH013: Repository rule violations found — Cannot push to this protected branch

# 4. The bot cannot merge its own unreviewed PR.
GH_TOKEN=$(lib/mint-github-token.sh) gh pr create --fill --head app-auth-smoke --base main
GH_TOKEN=$(lib/mint-github-token.sh) gh pr merge --squash <PR#>
# Expected: HTTP 405 — the PR is not mergeable (lacks a required approval from a non-author)
```

Then confirm on github.com that the smoke commit shows the **`my-fleet-bot[bot]`** avatar and
identity, not your personal account. `claudlobby --fleet <fleet> creds-reconcile` and the daily
`creds-check` also now probe the App token (an `ok` on `/installation/repositories`).

## The `gh` CLI

The fork this ported from **banned `gh` on the box** because gh's own credential helper (via
the OS keychain) would shadow the App helper. Claudlobby does **not** need that ban: the
composed per-bot `.gitconfig` resets the credential-helper list inside bot contexts, and the
composed `tools/gh` shim (on the bot's PATH ahead of system `gh`) mints an App token per call.
So `gh` works normally on an App bot and runs as the App identity.

Outside a bot session (your own shell, cron), mint per call and never export at boot — the
token dies in about an hour:

```bash
GH_TOKEN=$(lib/mint-github-token.sh) gh pr list
```

## Rotation and revocation

The App private key never expires, so rotate it deliberately (annually, or after any suspected
leak). GitHub supports **overlapping keys**, so there is no downtime:

1. Generate a NEW private key in App settings (both are now valid).
2. Copy it to the host, `chmod 600`, and point `GITHUB_APP_PRIVATE_KEY_PATH` (fleet `.env`
   and the setup config file) at it; re-run `lib/setup-github-app.sh` to validate.
3. `generate` + restart the fleet.
4. **Delete the OLD key** in App settings once every bot is on the new one.

To revoke access entirely, uninstall the App from the org (immediate) or delete the App.

## Common issues

- **`setup-github-app.sh` 401 "A JSON web token could not be decoded"** — the `.pem` belongs
  to a different App than `--app-id`, `--app-id` is wrong (Client ID or Installation ID
  supplied instead), the key was revoked, or the clock is skewed. The script prints the full
  tree with a fingerprint-compare recipe.
- **Commits attributed to you, not `<slug>[bot]`** — `github_app.slug` and `bot_user_id` must
  BOTH be set for the composed identity to arm; the validator warns when only one is. Confirm
  the bot restarted after `generate`.
- **`creds-check` records the App token as 403** — the installation lacks repository scope
  (or a secondary rate-limit). Check the App's repository permissions (Step 1).
- **The bot can push to `main`** — the ruleset added the App to the bypass list, or targets
  the wrong branch. Re-check Step 2.

## File reference

| File | Role |
|---|---|
| `lib/setup-github-app.sh` | one-time validation + config write + prints fleet values |
| `lib/git-credential-github-app` | the git credential helper (mints the token) |
| `lib/mint-github-token.sh` | prints a token for skills / per-call `gh` |
| `lib/github-app-mcp-wrapper.py` | keeps the GitHub MCP server on a fresh token |
| `library/mcp/github-app.json` | the `mcp: [github-app]` fragment |
| `library/integrations/github-app.md` | App-mode integration guidance |
| `documentation/examples/prevent-main-push-ruleset.json` | the branch-protection ruleset |
