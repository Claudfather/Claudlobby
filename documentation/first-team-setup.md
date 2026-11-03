# First-team setup

Create a lead, engineer and reviewer for a bounded goal in an existing approved repository, using your [Claudfather.ai](https://claudfather.ai) workspace. This is the manual terminal/browser route; it does not invoke the `/setup` skill. No Telegram channel is required. The [Telegram seed/direct-CLI walkthrough](getting-started.md#3-author-the-first-fleet) remains an optional operator path.

## Prepare the computer and trusted installer

Choose a fresh, dedicated data directory with no selected installation, activation history, fleet runtime or recorder. For an interrupted existing launch, keep its original identifiers and follow recovery instead of creating a replacement installation.

Complete the [host prerequisites and Python check](getting-started.md#prerequisites): CPython 3.11 for the supplied bundles, `tmux`, `jq`, Claude Code and a working native **user** manager (launchd on macOS or `systemctl --user` on Linux). Keep the proven absolute `PYTHON` executable. The bots inherit this OS user's permissions; read the [safety model](../README.md#safety-model) before choosing that user. The guide does not install prerequisites, change permissions or enable Linux user lingering.

Obtain a trusted exact installer supporting `--guided`, the `revenue-web-app` template and `plane-ui`, for your host platform. Use the [trusted installer verification](getting-started.md#trusted-local-release-bundle-no-checkout-required) and independently published checksum, then use the guided command below instead of the assembly-only command. Public installer links are not automatically updated with this guide; do not substitute an older release or guess a download. If no compatible trusted installer was supplied, stop or explicitly choose the [operator source-build alternative](getting-started.md#1-prepare-the-release-inputs-source-build-alternative), completing its assembly only and returning to [the local guide](#continue-after-release-assembly). Do not activate the seed fleet for this browser route.

## Run the guided installer

Use a local operator terminal on the computer where the team will run, outside generated bot or managed-service contexts. Set `INSTALLER` to the verified local file and `DATA` to your chosen new absolute directory. `PYTHON` is the absolute executable already proven by the prerequisite check.

```bash
INSTALLER="/absolute/trusted/claudlobby-install.pyz"
DATA="/absolute/new/claudlobby-data"
"$PYTHON" -I -c 'import sys; assert sys.version_info[:2] == (3, 11), "This bundle requires CPython 3.11"' &&
"$PYTHON" -I "$INSTALLER" --root "$DATA" --guided
```

Assembly starts no team and grants no owner access. On success, the installer prints **Save this command to resume local setup:** followed by the exact sealed CLI command, shell-quoted for your data directory. Copy and save the complete command before continuing. Read any assembly error before retrying.

The guide asks for non-secret configuration. You may assemble and reach these prompts without provider credentials or owner approval. If an exact value is unavailable, stop at its prompt; do not fabricate a value to reach the next step. Credentials do not belong in these prompts.

| Prompt | Where the value comes from |
| --- | --- |
| This computer's exact private HTTPS origin | The host operator's intended private Serve origin, with no `/owner` path. It is this computer's value, separate from the website. |
| Absolute native Tailscale executable | The actual executable provisioned for this OS user by the host operator. |
| Exact website issuer | In your workspace's **Prepare it** step, expand **Local approval values** and use the identically labeled value. |
| Exact operational browser origin | Use the identically labeled value in that same disclosure. Do not infer it from the current page or substitute demo/preview origins. |
| Private owner socket | The exact backend selected for the private Serve route. The displayed default is `DATA/state/owner/owner.sock`, outside recorder storage. A shorter custom path requires an existing private 0700 directory. |
| Native Tailscale control socket | Supply it only when the native daemon requires one; blank uses its native default. This is separate from the owner socket. |
| First team name and approved existing `OWNER/REPO` | Human-approved team name and repository scope. You enter the goal later in the browser. |
| Native installation directory | Choose from the user-manager paths observed and displayed by the guide. |

If you cannot open **Local approval values**, the deployment operator can supply those same exact non-secret values. If configuration is missing or unavailable, stop rather than infer it. Configured origins, repository names and socket paths do not prove credentials, connectivity or access.

## Before approving local access

The guide displays the chosen configuration before asking for `INITIALIZE`. **Do not type `INITIALIZE`** if any of the following is missing or unverified; stop with Ctrl-C at that prompt:

- Native provider sign-in plus separately provisioned Git/CLI and GitHub MCP access to the approved repository, for this OS user. Website login or a repository string establishes none of that access.
- Verified native Tailscale identity/access and a host-operator-prepared private HTTPS Serve route to the selected **private Unix owner socket**. Entering an origin/socket does not verify ingress. Follow the [owner-server boundary](architecture/observable-plane.md#private-socket-owner-server-and-source-binding); do not substitute localhost `plane view` forwarding, a public Funnel or unprotected TCP backend.
- Signed-in workspace membership and a compatible operational browser with **Create the first team** available. A saved computer address is navigation only; browser local-network permission and actual device connectivity require verification when connecting.

Do not accept authority or launch a team to discover whether these prerequisites work, and do not borrow a running fleet's credentials. The guide does not provision accounts, repository credentials or Tailscale/Serve. A credential-free acceptance run can exercise assembly and configuration, then stop before this first approval and record its exploration events.

Once access is independently established, review each local preview before its requested typed confirmation. The same terminal guides you through:

1. `INITIALIZE` for local owner identity, then browser **Start pairing** at the displayed `/owner` address. Paste the short-lived code at the hidden terminal prompt, compare the native identity and approve it with `PAIR` only if it matches.
2. Separate `INITIALIZE-WORKSPACE` and `INITIALIZE-CONNECTED` confirmations for workspace/session storage. The guide manages its foreground server transition; no second preparation terminal is needed.
3. In the computer's connected browser panel, choose **Prepare local approval** under **Connection options and first-time setup**. Paste its code at the hidden terminal prompt, review the workspace/computer and confirm with `BIND-WORKSPACE`.
4. Review the exact release, team, repository and native directory before `ALLOW-SETUP`. This permits one later explicit browser launch, which can start native services and consume provider usage. It grants no repository credentials or ordinary team actions. Approval lasts at most 24 hours; `host owner revoke-setup` withdraws it without undoing already-started activation.

Codes belong only in hidden prompts. Retain an uncertain step's original code or identifiers before inspecting it. Before activation, fleet reads are unavailable because no recorder exists; do not initialize a recorder or run `bind-source` to force access.

When local access is prepared, **keep this terminal open** for browser preview and launch. The owner server remains a foreground process; this guide installs no owner service or second server. Ctrl-C stops the browser connection and waits for already-started work to finish. It does not withdraw approvals or stop a team that has started.

## Preview once, launch once, keep the original receipt

In the computer's connected panel, choose **Connect**, then **Create the first team** and **Check setup approval**. A fresh computer may initially be connected for setup only.

Enter the approved repository goal and choose **Preview team**. Review the team, repository, goal and **Plan changes and recovery identifiers**. Preview stages a canonical plan and starts no bots. The plan expires after ten minutes or earlier when local approval expires.

When ready, choose **Launch this exact plan** once. Keep the original generation, plan ID and request UUID. If a response is lost or the page reloads, use **Check original activation receipt** for those same identifiers. Keep conflicting identifiers if reported. An unknown receipt does not mean nothing happened; do not launch again, replace the plan or seek a new approval to recover it.

An active receipt records activation. Independently check `host doctor`, `host status`, the dashboard and meaningful bot/provider/repository behavior, such as a bounded task with a reviewed result. Native trust or tool approvals may still need you at this computer. Recurring lead check-ins can consume provider usage with the browser closed. Read the [team's permission policy](../examples/teams/revenue-web-app/README.md); a recorded launch does not prove restart durability or useful work.

## Continue after release assembly

After an interruption, run the complete resume command saved from the installer, unchanged, in a local operator terminal. It already names the validated sealed executable and the same data directory. No release-path extraction or directory search is needed. Its form is:

```bash
"$RELEASE_CLI" --root "$DATA" host owner setup
```

For the examples here, set `RELEASE_CLI` to that exact executable path from the saved command and keep the original `DATA`. The installer prints the command; it does not create an additional receipt file. If you explicitly chose source-build or assembly-only instructions, use their exact returned `data.cli` as `RELEASE_CLI` and enter the same local guide with the command above.

Re-enter the same configuration. Matching retained preparation and approvals are reused; a different/inactive binding stops, renewal needs explicit consent, and launches are never replayed. A completed matching installation can return to foreground serving. For an unresolved launch, inspect its original browser receipt or read local state:

```bash
"$RELEASE_CLI" --root "$DATA" host status --json
"$RELEASE_CLI" --root "$DATA" config diff ORIGINAL_PLAN_ID
```

Use the original plan ID and request UUID. Its journal is `DATA/state/activations/REQUEST_UUID/activation.json`; it survives setup approval expiry/revocation. Follow [activation/refusal guidance](getting-started.md#4-activate-and-diagnose) before recovery. A new setup approval does not restore the previous grant's browser journal authority.

## Approve team actions separately

Reading the team grants no messages, nudges, feedback or permission decisions. After launch, select the team and open **Action access help** beside **Check action access**. An unavailable check may reflect the connection/session rather than missing local approval.

To approve ordinary actions, use the same sealed CLI locally. Replace `TEAM` and `human:ALIAS`, choosing only wanted capabilities; this example selects all three:

```bash
"$RELEASE_CLI" --root "$DATA" host owner allow-actions \
  --target-fleet TEAM --actor human:ALIAS \
  --capabilities messages nudges feedback --register-actor
```

Review the owner, team, actor and capabilities before `ALLOW-ACTIONS`. A new actor also requires the separate `REGISTER` confirmation; registration alone grants no actions. Approving one team or human does not approve another. Task permission decisions require their own local `host owner allow-permissions` approval; ordinary-action approval grants none of them.

Return to **Check action access** in the browser to read existing approvals. It sends/replays no action and does not inspect a saved request's receipt. Use each original receipt control separately.

These revised manual instructions require a fresh cold run before publication. Mechanical installer checks and unit tests do not establish newcomer acceptance.
