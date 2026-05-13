# GitHub App Installation Token Setup

End-to-end guide for setting up a GitHub App that uses **installation tokens** to authenticate from a headless box. The bot has its own identity (`artemis-infra-botfarm[bot]`) that is independent of your GitHub user account, so it can be excluded from bypass lists, restricted from protected branches, and prevented from inheriting admin powers — even when the human operating it is an org admin.

## Overview

```
Laptop (one-time)                   Headless box
─────────────────                   ────────────
0. Set up the box first  ─────►    See mac-mini-setup-guide.md
   (SSH, Tailscale SSH,             (you should be able to `ssh mini`
    Homebrew, deps)                  over Tailscale before continuing)
1. Register GitHub App
2. Generate private key (.pem)
3. scp .pem to box           ─►    ~/.config/botfarm/private-key.pem
4. scp setup script to box   ─►    ~/setup-git-creds-app.sh
5. Run setup                       Configures git + credential helper
                                          │
                                          ▼
                                   git operations transparently mint
                                   fresh ghs_ tokens via the helper

```

**Key properties of this setup:**

- Bot identity is `artemis-infra-botfarm[bot]`, distinct from any human user. Commits are attributed to the bot.
- The App's private key is the master credential, shared across all boxes that act as the bot.
- Tokens are **minted on demand** (1-hour lifetime), not stored. The credential helper signs a JWT with the private key and exchanges it for a fresh `ghs_` token whenever git needs auth.
- Branch protection rules apply to the bot independently of the human operator's privileges. The bot is *not* an admin and is *not* in any bypass team — even if the human operating it is.

> **Naming note.** This guide uses `artemis-infra-botfarm` as the example **bot slug** — i.e., the GitHub App's slug, which determines the bot's identity (`artemis-infra-botfarm[bot]`). Substitute your own slug throughout. Separately, the **tool itself** is named `botfarm`: the helper script is `git-credential-botfarm`, its config lives at `~/.config/botfarm/`, and it reads `BOTFARM_*` env vars. Those tool names are literal and don't change per bot.

## Why installation tokens

This setup uses GitHub App **installation tokens** (`ghs_...`) for bot authentication. The alternative — user access tokens (`ghu_...`) minted via OAuth device flow — has a structural limitation that makes it unsuitable here: the bot inherits all of the human operator's permissions, including admin status and bypass actor membership.

Since bot operators at Artemis are admins, a user-token-based bot would inherit admin powers and bypass branch protection rules — defeating the security boundary entirely. Installation tokens give the bot a separate identity (`artemis-infra-botfarm[bot]`) that can be excluded from bypass lists, restricted from protected branches, and held to all branch protection rules independent of the human operator's privileges.

## Prerequisites

> **Do the headless-box setup first.** This guide assumes you already have a reachable, properly-configured headless box. If you're starting from a fresh Mac mini, work through [`mac-mini-setup-guide.md`](mac-mini-setup-guide.md) first — it enables Remote Login, installs Tailscale SSH (so you can reach the box from anywhere on your Tailnet via a stable `mini` alias), and installs Homebrew, Node, and Claude Code. Come back here once `ssh mini` works and the box has `git`, `python3`, `curl`, `jq`, and `uv` on it.

- GitHub account with permission to create GitHub Apps (one-time, by app admin)
- Local machine with `python3`, `ssh`, and `scp`, on the same Tailnet as the headless box
- Headless box already set up per [`mac-mini-setup-guide.md`](mac-mini-setup-guide.md) (or the Linux equivalent):
  - Reachable over Tailscale SSH from your laptop as `mini` (or whatever alias you chose)
  - `git`, `python3`, `curl`, `jq`, and `uv` installed
  - Python `pyjwt` and `cryptography` packages installed via `uv` (Step 4 below)
- **`gh` CLI must NOT be installed on the headless box** (it shadows the bot's credential helper via the macOS Keychain — see "Important: no `gh` CLI on the box")

## Step 1: Configure the GitHub App (one-time, by app admin)

If you've already created a `artemis-infra-botfarm` App (or equivalent) and want to reuse it, skip to Step 1b.

### Step 1a: Create the App

For each new bot app you need to create (per-person or per-project), go to **github.com/organizations/Artemis-xyz/settings/apps/new** and configure:

| Field | Value |
|---|---|
| GitHub App name | A globally-unique name following the `artemis-<scope>-botfarm` convention, e.g. `artemis-infra-botfarm`, `artemis-data-botfarm`, `artemis-frontend-botfarm` |
| Homepage URL | Anything (e.g., `https://artemis.xyz`) |
| Callback URL | Leave blank |
| Webhook → Active | **Uncheck** |
| Repository permissions → Actions | **Read-only** (lets the bot inspect workflow runs and statuses) |
| Repository permissions → Checks | **Read-only** (lets the bot read check-run results without authoring them) |
| Repository permissions → Contents | **Read and write** (push branches, edit files) |
| Repository permissions → Issues | **Read and write** (open, comment on, label, and close issues) |
| Repository permissions → Metadata | Read-only (required, auto) |
| Repository permissions → Pull requests | **Read and write** (open PRs, comment, label, merge — gated by branch protection; see "Permission decisions" below) |
| Repository permissions → Workflows | **Read and write** (lets the bot add or edit files under `.github/workflows/`) |
| All other permissions | No access |
| Where can this be installed? | Only on this account (the org) |

Click **Create GitHub App**, then install on the org with the specific repos the bot should access.

### Step 1b: Generate a private key

On the App settings page (e.g., github.com/organizations/Artemis-xyz/settings/apps/artemis-infra-botfarm):

1. Scroll to **Private keys**
2. Click **Generate a private key**
3. A `.pem` file downloads automatically — save it somewhere on your laptop, e.g., `~/.config/botfarm/private-key.pem`. **This file is the master credential** for the App. Treat it like an SSH private key.

```bash
# On laptop
mkdir -p ~/.config/botfarm
chmod 700 ~/.config/botfarm
mv ~/Downloads/botfarm.*.private-key.pem ~/.config/botfarm/private-key.pem
chmod 600 ~/.config/botfarm/private-key.pem
```

### Step 1c: Record the App ID and Installation ID

You'll need both values verbatim in Step 5 — write them down somewhere you can copy-paste from (password manager, sticky note, scratch file). They're not secret, but if you mistype one or use the runbook's placeholder by accident the setup script returns a cryptic 401 from GitHub.

| Value | Where to find it | Example format |
|---|---|---|
| **App ID** | Top of the App settings page (`https://github.com/organizations/<org>/settings/apps/<bot-slug>`), labelled "App ID" — *not* the Client ID `Iv23li…` directly below it | `3647284` (6-8 digit integer) |
| **Installation ID** | `https://github.com/organizations/<org>/settings/installations` → click **Configure** next to the bot's installation; the resulting URL ends in `/installations/<INSTALLATION_ID>` | `94217635` (7-9 digit integer) |

Record them now:

```
App ID:           ____________
Installation ID:  ____________
Bot slug:         ____________   (the App's URL slug, e.g. artemis-infra-botfarm)
Private key path: ~/.config/botfarm/private-key.pem
```

**Do not** carry over the example values from Step 5's code block (`1234567`, `7654321`) — those are placeholders. Substitute yours.

### Permission decisions

**Pull requests: write** is a single grant covering PR creation, commenting, labeling, *and* merging — GitHub does not offer create-without-merge at the App level. The merge restriction must come from branch protection rules instead (see Step 2). If the bot doesn't need to open PRs at all (e.g., it just pushes branches and humans handle PRs entirely), leave this off and the bot has no merge capability via the API regardless of branch protection.

**Workflows: write** is required if the bot ever needs to add, edit, or delete files under `.github/workflows/` — without it, a `git push` containing workflow changes will be rejected with `refusing to allow a GitHub App to create or update workflow without `workflows` permission`. If the bot will never touch CI definitions, you can downgrade this to no access; pushes that don't change workflow files are unaffected.

**Actions: read** and **Checks: read** are read-only by design — they let the bot inspect CI state (e.g. wait for a green build before merging, or comment with a failing job's log link) without granting it the ability to re-run workflows, cancel jobs, or author check runs. Upgrade to write only if you have a specific use case.

**Issues: write** lets the bot file, triage, and close issues. Drop it if the bot has no issue-management responsibilities.

## Step 2: Configure branch protection on bot-accessible repos

This is the layer that mechanism-enforces "bot can't merge unreviewed code" and "bot can't bypass review." The bot is `artemis-infra-botfarm[bot]`, a distinct identity, so rules can target it specifically.

For each repo in the bot's installation, go to **Settings → Rules → Rulesets** (or **Branches → Branch protection rules** on older UI) and create/edit a ruleset for `~DEFAULT_BRANCH`:

- ✅ **Require a pull request before merging**
- ✅ **Required approving review count: 1** (or more)
- ✅ **Dismiss stale pull request approvals when new commits are pushed**
- ✅ **Require approval of the most recent reviewable push**
- ✅ **Require review from Code Owners** (if you use CODEOWNERS)
- ✅ **Block force pushes**
- ✅ **Restrict deletions**

For **Bypass list**: do *not* include the bot. If admins need bypass for emergencies, leave the admin team there but keep `artemis-infra-botfarm[bot]` out of the bypass list. The bot is now subject to all rules even when humans bypass.

For org-scale management, prefer **org-level rulesets** (Settings → Repository → Rulesets at the org level) over per-repo rules, so adding new repos to the App's installation automatically gets them protected.

After this configuration, the bot's effective capabilities on `main`:

- Push directly to `main`: ❌ (require PR)
- Open PR to `main`: ✅ (if `Pull requests: write` granted)
- Approve own PR: ❌ (GitHub forbids self-approval)
- Merge own PR: ❌ (no approval from a non-author exists; bot can't be the approver)
- Bypass any of the above: ❌ (bot not in bypass list)

## Step 3: Important — no `gh` CLI on the box

**Do not install `gh` on the headless box.** If it's installed:

```bash
brew uninstall gh 2>/dev/null || true
which gh   # should be empty
```

`gh` registers itself as a credential helper and writes a `gho_` token to the macOS Keychain. That token shadows the App's installation token from the credential helper, silently. Operations succeed with `gh`'s token (full account access) instead of the bot's token (App-scoped, no admin).

Wipe any existing GitHub credentials in the keychain:

```bash
while security delete-internet-password -s github.com 2>/dev/null; do :; done
while security delete-generic-password  -s github.com 2>/dev/null; do :; done
```

The setup script in Step 5 includes a sanity check that surfaces this problem at install time.

## Step 4: Install the private key and dependencies on the box

### 4a: Copy the private key

```bash
# On laptop
ssh mini 'mkdir -p ~/.config/botfarm && chmod 700 ~/.config/botfarm'
scp ~/.config/botfarm/private-key.pem mini:~/.config/botfarm/private-key.pem
ssh mini 'chmod 600 ~/.config/botfarm/private-key.pem'

# Verify
ssh mini 'ls -l ~/.config/botfarm/private-key.pem'
# Should show: -rw------- 1 akan staff ... private-key.pem
```

### 4b: Install Python dependencies

The credential helper uses `pyjwt` to sign App JWTs. Install it on the box with `uv`:

```bash
ssh mini 'uv pip install pyjwt cryptography --break-system-packages --system'
```

### 4c: Copy the setup and helper scripts

From a fresh clone of this repo:

```bash
scp ./lib/setup-git-creds-app.sh mini:~/
scp ./lib/git-credential-botfarm mini:~/
ssh mini chmod +x setup-git-creds-app.sh git-credential-botfarm
```

## Step 5: Run the setup script on the box

Substitute the **App ID** and **Installation ID** you recorded in Step 1c — `1234567` and `7654321` are placeholders, not real values. Running with the placeholders verbatim is a common foot-gun and produces a misleading 401 ("JSON web token could not be decoded") from GitHub.

```bash
ssh mini "./setup-git-creds-app.sh \
  --app-id <APP_ID_FROM_STEP_1c> \
  --installation-id <INSTALLATION_ID_FROM_STEP_1c> \
  --private-key ~/.config/botfarm/private-key.pem \
  --bot-slug <YOUR_BOT_SLUG>"
```

Example with concrete (but **fake**) values, for shape reference only:

```bash
ssh mini "./setup-git-creds-app.sh \
  --app-id 1234567 \
  --installation-id 7654321 \
  --private-key ~/.config/botfarm/private-key.pem \
  --bot-slug artemis-infra-botfarm"
```

The script:

1. Validates the private key file is readable and PEM-formatted
2. Mints a test installation token to confirm the App ID, installation ID, and key are all correct
3. Looks up the bot's numeric user ID via the GitHub API (used for the noreply email)
4. Installs `git-credential-botfarm` to `/usr/local/bin/` or `/opt/homebrew/bin/` (the helper must be on PATH for git to find it)
5. Writes config to `~/.config/botfarm/config` (App ID, Installation ID, PEM path) — the helper reads from this file at every git auth call, so no shell rc edits are needed
6. Configures git: credential helper, `user.name = artemis-infra-botfarm[bot]`, `user.email = <id>+artemis-infra-botfarm[bot]@users.noreply.github.com`, the `git@github.com:` → `https://github.com/` rewrite
7. Runs `git credential fill` to confirm the helper returns a valid `ghs_` token

If `/usr/local/bin` and `/opt/homebrew/bin` aren't writable, the script will request sudo to install the helper. Run with `sudo` upfront if you want to avoid the password prompt.

Expected output:

```
✓ Test token minted successfully
✓ Bot user: artemis-infra-botfarm[bot] (id: 12345678)
✓ Helper installed: /usr/local/bin/git-credential-botfarm
✓ Helper config written to /Users/akan/.config/botfarm/config
✓ Git credentials configured
  App ID:           1234567
  Installation ID:  7654321
  Private key:      /Users/akan/.config/botfarm/private-key.pem
  Bot identity:     artemis-infra-botfarm[bot]
  Bot email:        12345678+artemis-infra-botfarm[bot]@users.noreply.github.com
```

If the sanity check fails (helper doesn't return a valid token, or another helper shadows it), the script exits non-zero with the specific cause.

## Step 6: Verify

### Test git operations

```bash
ssh mini

# In a repo the App has access to
cd /path/to/Claudlobby
git pull
# Should succeed silently — helper minted a fresh ghs_ token under the hood

# Push a test commit (to a non-protected branch)
git checkout -b test-bot-identity
git commit --allow-empty -m "test: verify bot identity"
git push -u origin test-bot-identity
```

Then check on github.com that the commit shows the **artemis-infra-botfarm[bot]** avatar and identity, not your personal account.

### Verify scoping

```bash
# Repo NOT in the App's installation — should fail with 404
cd /path/to/some-other-repo
GIT_TERMINAL_PROMPT=0 git ls-remote >/dev/null 2>&1 \
  && echo "✗ leaked access — scoping is not working" \
  || echo "✓ correctly denied"
```

### Verify branch protection blocks the bot

```bash
# Try pushing directly to main — should fail
git checkout main
git commit --allow-empty -m "should be rejected"
GIT_TERMINAL_PROMPT=0 git push origin main
# Expected: error: GH013: Repository rule violations found
#           - Cannot push to this protected branch
```

### Verify the bot can't merge an unreviewed PR

Open a PR via the API using the bot, then try to merge:

```bash
ssh mini

# Get a token directly via the helper
token=$(printf 'url=https://github.com\n\n' | git credential fill | grep '^password=' | cut -d= -f2-)

# Open a test PR (assumes test-bot-identity branch from earlier exists)
curl -fsS -X POST \
  -H "Authorization: Bearer $token" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/Artemis-xyz/farm-artemis/pulls \
  -d '{"title":"test","head":"test-bot-identity","base":"main","body":"test"}'

# Try to merge it (replace <PR_NUMBER>) — should fail
curl -i -X PUT \
  -H "Authorization: Bearer $token" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/Artemis-xyz/farm-artemis/pulls/<PR_NUMBER>/merge

# Expected: HTTP 405 Method Not Allowed, with body explaining the PR is not mergeable
# (because it lacks the required approval from a non-author)
```

If all four checks pass, the bot has scoped access, a separate identity, and is genuinely restricted by branch protection independent of your admin privileges.

## Common issues

### `setup-git-creds-app.sh` returns 401 "A JSON web token could not be decoded"

GitHub couldn't verify the JWT signature against any public key registered for App ID `<your --app-id>`. Diagnose in this order — stop at the first failure:

1. **PEM matches the App.** Compute the public-key fingerprint and compare to the App's settings page (under **Private keys** — GitHub shows a SHA-256 fingerprint per key):

   ```bash
   ssh mini 'openssl rsa -in ~/.config/botfarm/private-key.pem -pubout 2>/dev/null \
             | openssl rsa -pubin -outform DER 2>/dev/null \
             | openssl dgst -sha256 -binary | openssl base64'
   ```

   Mismatch ⇒ the `.pem` on the box is for a different App, or the key was rotated and the old `.pem` is stale. Re-`scp` the correct file from your laptop (step 4a).

2. **`--app-id` is correct.** It's the numeric App ID at the top of the App's settings page — *not* the Installation ID and *not* the Client ID. They look similar but each is wrong in its own way.

3. **PEM line endings are LF.** Verify with `ssh mini 'file ~/.config/botfarm/private-key.pem'`. If it says "with CRLF line terminators", strip them:

   ```bash
   ssh mini "tr -d '\r' < ~/.config/botfarm/private-key.pem > /tmp/pk && \
             mv /tmp/pk ~/.config/botfarm/private-key.pem && \
             chmod 600 ~/.config/botfarm/private-key.pem"
   ```

4. **`cryptography` is installed.** PyJWT alone cannot sign RS256 and will silently produce a token GitHub rejects:

   ```bash
   ssh mini 'python3 -c "import cryptography, jwt"'   # must not raise
   ```

   If missing: `ssh mini 'uv pip install pyjwt cryptography --break-system-packages --system'`.

5. **The private key wasn't revoked.** If steps 1-4 all pass but the error persists, check the App's settings page — if the key was deleted there, generate a new one and re-deploy.

### Helper returns no credential

The credential helper's JWT minting failed. Common causes:

- `pyjwt` not installed: `uv pip install pyjwt cryptography --break-system-packages --system`
- Wrong path to PEM in `~/.config/botfarm/config` (check `BOTFARM_PRIVATE_KEY_PATH` value)
- App ID or Installation ID in `~/.config/botfarm/config` is wrong (returns 404 from GitHub)
- Private key has been revoked in app settings
- Config file missing entirely (check `ls ~/.config/botfarm/config`; re-run setup if absent)

Test the helper directly to see the underlying error:

```bash
git-credential-botfarm get
# (will read config from ~/.config/botfarm/config and attempt to mint a token)
```

To override config for testing without editing the file, set env vars:

```bash
BOTFARM_APP_ID=1234567 \
BOTFARM_INSTALLATION_ID=7654321 \
BOTFARM_PRIVATE_KEY_PATH=/tmp/test-key.pem \
git-credential-botfarm get
```

### `git credential fill` returns a `gho_` token

The `gh` CLI's OAuth token is being served by the macOS Keychain and shadowing the App helper. See Step 3 — uninstall `gh`, wipe the keychain, then re-run setup.

### Commits are attributed to me, not to `artemis-infra-botfarm[bot]`

`user.name` / `user.email` aren't set correctly. Verify:

```bash
git config --global user.name    # should be artemis-infra-botfarm[bot]
git config --global user.email   # should be <id>+artemis-infra-botfarm[bot]@users.noreply.github.com
```

If wrong, re-run the setup script. If you have repo-local overrides, check `.git/config` in each repo.

### Bot can push to `main` directly

Branch protection isn't configured correctly. Verify:

```bash
curl -s -H "Authorization: Bearer <ghs_token>" \
  https://api.github.com/repos/Artemis-xyz/farm-artemis/rules/branches/main \
  | jq
```

Look for the `pull_request` rule with `required_approving_review_count >= 1`. If `artemis-infra-botfarm[bot]` appears in any `bypass_actors`, remove it.

## Rotation

### Routine private key rotation (annually, or after any suspected leak)

1. In app settings → **Private keys** → **Generate a private key**. Save the new `.pem`.
2. (Don't delete the old key yet — you need both temporarily for zero-downtime rotation.)
3. scp the new PEM to every box, overwriting the existing file at the path in `~/.config/botfarm/config` (typically `~/.config/botfarm/private-key.pem`). The helper picks up the new key on the next git operation.
4. Once all boxes are using the new key, return to app settings and delete the old key.

If you accept brief downtime, just delete the old key first, then deploy the new one — simpler but the bot can't auth in between.

### Adding repos to the App's installation

No box-side action needed. Adding repos at **github.com/organizations/Artemis-xyz/settings/installations** takes effect immediately — the next time the helper mints a token, it has access to the new repo automatically.

### Adding new permissions to the App

Edit the App, add the permission. The org admin (and only the org admin, since this is an org-installed App acting on its own behalf) approves the permission update. After approval, the next minted token has the new capability.

## File reference

| File | Location | Purpose |
|---|---|---|
| `setup-git-creds-app.sh` | `lib/` (this repo) → `scp`'d to headless box | One-time setup: validates inputs, installs helper, configures git |
| `git-credential-botfarm` | `lib/` (this repo) → installed to `/usr/local/bin/` on the box | Credential helper invoked by git; mints fresh `ghs_` tokens via JWT exchange |
| `~/.config/botfarm/config` | Headless box | Helper config (App ID, Installation ID, PEM path), mode 600 |
| `~/.config/botfarm/private-key.pem` | Headless box | App's private key, mode 600. Master credential for token minting. |
| Shared GitHub App | github.com/organizations/Artemis-xyz/settings/apps/&lt;bot-slug&gt; (e.g., `.../apps/artemis-infra-botfarm`) | Defines repo access and permissions. Identity is `<bot-slug>[bot]` (e.g., `artemis-infra-botfarm[bot]`) |

## Appendix: Example branch protection ruleset

A working ruleset that implements the Step 2 requirements (require PR + 1 approval, block force-push, restrict deletions) is checked in at [`examples/prevent-main-push-ruleset.json`](../examples/prevent-main-push-ruleset.json). You can fetch the current ruleset for any repo via the API and replay it (after editing the two deployment-specific fields) into another repo, or apply it via Terraform/`gh api`.

```json
{
  "id": 13540503,
  "name": "prevent-main-push",
  "target": "branch",
  "source_type": "Repository",
  "source": "Artemis-xyz/artemis-invest-frontend",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "exclude": [],
      "include": [
        "~DEFAULT_BRANCH"
      ]
    }
  },
  "rules": [
    {
      "type": "deletion"
    },
    {
      "type": "non_fast_forward"
    },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": false,
        "required_reviewers": [],
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": [
          "merge",
          "squash",
          "rebase"
        ]
      }
    }
  ],
  "bypass_actors": [
    {
      "actor_id": 16266619,
      "actor_type": "Team",
      "bypass_mode": "pull_request"
    }
  ]
}
```

**Two fields are example-specific** — change them when applying this ruleset to another repo:

- `source` — the `<org>/<repo>` this ruleset is attached to. The example uses `Artemis-xyz/artemis-invest-frontend`; replace with whichever repo you're protecting.
- `bypass_actors` — the team(s) allowed to bypass under `pull_request` mode. The example's `actor_id: 16266619` is one specific Artemis team; substitute the `actor_id` of the team you want to grant emergency-bypass to (or leave empty to allow no bypass at all). The bot itself must **not** appear in this list — that's the whole point of Step 2.

Everything else (`rules`, `conditions`, `enforcement: active`, the `~DEFAULT_BRANCH` include) is the reusable core.
