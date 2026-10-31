# First-team setup

Use this route to create a lead, engineer and reviewer for a bounded goal in an existing approved repository, through your [Claudfather.ai](https://claudfather.ai) workspace. It joins release assembly, local computer approval, browser preview, one explicit launch and the original activation receipt. No Telegram channel is required. The [Telegram seed/direct-CLI walkthrough](getting-started.md#3-author-the-first-fleet) remains an optional operator path.

A supervised trial has completed native launch, an implementation with independent review, and browser task feedback. That does not establish unattended installation or automatic native consent. **The revised newcomer instructions still require a fresh cold run.** Candidate qualification and public availability are separate: a trusted exact release can be assembled before website setup is enabled. If **Create the first team** is unavailable, stop before browser launch at that publication gate rather than substituting seed activation.

## Prepare the installation and access

Use a fresh, dedicated data root with no selected release, activation history, fleet runtime or existing recorder. Do not use this first-team route to repair or replace an existing installation. Keep an interrupted activation's original identifiers and inspect its existing recovery state instead.

Before release assembly, check the noncredential host prerequisites and select trusted inputs:

- The [getting-started prerequisites](getting-started.md#prerequisites), including CPython 3.11 for the release-bundle path, a working native user manager, `tmux`, `jq` and Claude Code. Choose the OS user with its actual permissions in mind.
- A trusted matching release that includes the `revenue-web-app` template, `host owner allow-setup` and the `[plane-ui]` dependencies. Choose either the [trusted local installer](getting-started.md#trusted-local-release-bundle-no-checkout-required), explicitly built with `plane-ui`, or the [source-build alternative](getting-started.md#1-prepare-the-release-inputs-source-build-alternative) with `[plane-ui]` in its wheelhouse. No current hosted installer, latest-release download or compatible artifact is promised by this guide.

For the operator source-build alternative, use a committed revision that includes this setup capability, record its full revision and retain its wheel/lock checksums. These features are merged; a source build still needs qualification with the matching browser publication before it is offered as a tested setup release. Building and assembling a candidate is allowed while website setup publication is gated; it does not qualify or publish that candidate.

Assemble the release with the chosen getting-started path, retaining `WORK/host-setup.json`, `DATA` and its exact returned `RELEASE_CLI`. Source-build users complete sections 1–2 only; **do not copy/activate the seed fleet** for this browser trial. Assembly starts no bots. Read its unsuccessful result before proceeding; do not guess a CLI, release or native directory.

## Continue after release assembly

From the successful assembly result, retain the exact release and native directory:

```bash
RELEASE_ID=$(jq -er \
  'select(.ok == true) | .data.release_id | select(type == "string" and length > 0)' \
  "$WORK/host-setup.json") &&
USER_UNIT_DIR=$(jq -er \
  'select(.ok == true) | .data.install_directories | select(type == "array" and length > 0) | .[0] | select(type == "string" and length > 0)' \
  "$WORK/host-setup.json")
```

## Before connecting: access and publication gates

Assembly is complete; it has not selected a release or started a team. Before owner pairing, workspace approval or browser launch, establish:

- Native Tailscale identity and private HTTPS ingress configured to forward to this host's **private Unix owner socket**, using the [owner-server boundary](architecture/observable-plane.md#private-socket-owner-server-and-source-binding). The ordinary localhost `plane view` forwarding recipe is not this owner server. Provisioning Tailscale/Serve remains a host prerequisite; the commands below do not configure it.
- A signed-in website workspace and a matching operational browser publication containing the same release's first-team setup capability. Saving a computer's exact HTTPS address supplies navigation only. The website issuer and operational browser origin must be the deployed, configured HTTPS origins; do not substitute demo or preview aliases. Browser local-network permission and actual device connectivity need separate verification.
- Human-approved `OWNER/REPO` scope and goal, native provider authentication for the OS user, and separately provisioned host Git/CLI and GitHub MCP access to that repository. A GitHub website login, template name or repository string proves none of that access. This route does not install a GitHub App, create repository credentials or prepare a repository automatically.

**Credential gate:** stop here if provider/repository access, website membership or native Tailscale identity is missing or unverified. Do not borrow a running fleet's credentials, start the owner server or launch a team to discover whether access works. A cold-host validation stops at this gate and records its exploration events; the continuation below is for an operator who has separately established the prerequisites. This route uses no Telegram channel.

Use operator terminals on this computer, outside generated bot or managed-service contexts. Keep the same `DATA`, sealed `RELEASE_CLI` and native user domain throughout. Set `HOST_ORIGIN` to the exact private HTTPS owner-server origin, `TAILSCALE` to the absolute native executable, `WORKSPACE_ISSUER` to the configured website issuer and `WORKSPACE_ORIGIN` to the configured operational browser origin. These are existing deployment values, not URLs generated by setup.

## Pair the owner before the first fleet

In the first terminal:

```bash
"$RELEASE_CLI" --root "$DATA" host owner initialize --before-first-fleet &&
"$RELEASE_CLI" --root "$DATA" host owner serve \
  --origin "$HOST_ORIGIN" --tailscale "$TAILSCALE"
```

Initialization asks for its existing `INITIALIZE` confirmation. With no explicit socket, the fresh-host server creates the private directory and socket at `DATA/state/owner/owner.sock`, outside recorder storage. Configure the private Serve route to that exact socket before accessing it; if its path exceeds the native socket limit, use an explicitly chosen shorter socket in an existing private 0700 directory and retain that same path for both server invocations and Serve. If the native daemon uses a separate control socket, use the existing `--tailscale-socket` option consistently. Never substitute a public Funnel or unprotected TCP backend.

Open `/owner` at `HOST_ORIGIN`, choose **Start pairing**, and compare the displayed native identity with the local confirmation. In a second operator terminal:

```bash
"$RELEASE_CLI" --root "$DATA" host owner confirm
"$RELEASE_CLI" --root "$DATA" host owner status --json
```

Paste the short-lived code at the hidden prompt and inspect its identity before the existing `PAIR` confirmation. Do not put codes in arguments or URLs. Owner pairing establishes identity; before activation, private fleet reads are still unavailable because no recorder exists. Do not initialize a recorder or run `bind-source` to force this fresh route.

Stop the foreground server with Ctrl-C. Prepare its optional workspace/session storage:

```bash
"$RELEASE_CLI" --root "$DATA" host owner initialize-workspace &&
"$RELEASE_CLI" --root "$DATA" host owner initialize-connected &&
"$RELEASE_CLI" --root "$DATA" host owner serve \
  --origin "$HOST_ORIGIN" --tailscale "$TAILSCALE" \
  --socket "$DATA/state/owner/owner.sock" \
  --workspace-issuer "$WORKSPACE_ISSUER" --workspace-origin "$WORKSPACE_ORIGIN"
```

Keep this server running for setup and activation. It remains a foreground operator process; this guide enrolls no second owner server. Preserve its explicit socket path after activation too.

## Approve this workspace and one setup

From the signed-in workspace, open its connected operational browser, using the saved computer address that exactly matches `HOST_ORIGIN`. Under that computer's **Connection options and first-time setup** disclosure, choose **Prepare local approval**. Inspect the displayed workspace, issuer and host. In the second terminal:

```bash
"$RELEASE_CLI" --root "$DATA" host owner confirm-workspace
"$RELEASE_CLI" --root "$DATA" host owner workspace-status --json
```

Paste the retained workspace code at the hidden prompt and review the local preview before its existing `BIND-WORKSPACE` confirmation. If staging was uncertain, inspect the original code locally instead of repeating it. Continue only with an effective active binding for the intended workspace.

Replace `example-org/web-app` below with the approved existing repository and `web-app` with the intended first fleet name. Create only the native directory reported by assembly, then approve the exact release/repository/directory tuple:

```bash
mkdir -p "$USER_UNIT_DIR" &&
"$RELEASE_CLI" --root "$DATA" host owner allow-setup \
  --template revenue-web-app --target-fleet web-app \
  --repository example-org/web-app --release "$RELEASE_ID" \
  --install-directory "$USER_UNIT_DIR"
"$RELEASE_CLI" --root "$DATA" host owner setup-status --json
```

Read the local preview before its existing `ALLOW-SETUP` confirmation. This permits one browser setup and can start native services and consume agreed provider usage. The grant lasts at most 24 hours and supplies no repository credentials or message/task-action grants. `host owner revoke-setup` withdraws retained setup approval; it does not undo already-started activation.

## Preview once, launch once, inspect the original receipt

In that computer's connected panel choose **Connect** explicitly. A fresh computer may be connected for setup only; that is not a ready dashboard. Open **Create the first team**, choose **Check setup approval**, enter the approved repository goal, and choose **Preview team**. Review the team, repository, goal and **Plan changes and recovery identifiers**. Preview authors/stages a canonical plan but starts no bots; it expires after ten minutes or earlier when local approval expires.

Only when ready to start this team, choose **Launch this exact plan** once. The browser saves the original generation, plan ID and request UUID before sending. On a lost response or reload, choose **Check original activation receipt** for those identifiers; never launch again, replan or resume merely because a reply was lost. Keep conflicting identifiers if the UI reports them. After approval expires or is revoked, retain the original IDs and inspect locally:

```bash
"$RELEASE_CLI" --root "$DATA" host status --json
"$RELEASE_CLI" --root "$DATA" config diff PLAN_ID
```

Replace `PLAN_ID` with the original retained value. The corresponding read-only journal is `DATA/state/activations/REQUEST_UUID/activation.json`, using that same original UUID. An unknown receipt is not proof that activation did nothing. Use the [existing activation/refusal guidance](getting-started.md#4-activate-and-diagnose) before deciding any repair; a new setup approval does not recover the previous grant's browser journal authority.

An `active` receipt establishes the canonical activation state only. Check the connection for its dashboard and independently inspect `host doctor`, `host status`, actual bot/provider/repository behavior and the [team's task permission policy](../examples/teams/revenue-web-app/README.md). The lead's recurring check-ins can consume provider usage and continue with the browser closed. A recorded launch does not demonstrate useful work, permission enforcement, restart durability or a business result. The completed supervised trial is evidence for that bounded journey; a new installation still needs its own readiness checks, and the revised onboarding route needs cold-host acceptance. Native trust or tool approvals may require you at this computer. Reading the dashboard does not grant messages, task feedback or permission decisions: those use separate local owner approvals.
