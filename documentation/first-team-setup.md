# First-team setup

The package's `revenue-web-app` template creates a lead, engineer and reviewer for a human-approved goal in an existing repository. No Telegram channel is required. This page describes the operator CLI route; newcomer browser setup on the managed Git runtime has not completed cold-host acceptance. Do not substitute the old browser launch or guided installer for these commands.

Complete [Getting started](getting-started.md) through managed installation preparation and its credential gate. Keep the stable `CLI`, explicit `DATA`, and native directory returned by `host setup`. Provider sign-in, Git/CLI and MCP repository access must be verified for this OS user; a repository name or website login proves none of those.

## Prepare one exact team

Use the exact approved repository and a bounded goal, replacing the example values. This stages the same retained update operation used by authored `--config` setup. Choose this command or the authored-config command, not both.

```bash
"$CLI" --root "$DATA" --fleet example fleet setup \
  --template revenue-web-app --repository example-org/example-repo \
  --goal "Implement the approved issue with review and the repository's required checks" \
  --install-directory "$NATIVE_DIRECTORY" --plan-only --json
```

Review the original request ID, target revision, configuration fingerprint, team, repository, goal and native consumers. Preparation starts no agents and grants no owner/browser access. Stop before continuing until credentials, repository access and the exact native scope are approved. Starting agents can consume provider usage.

```bash
"$CLI" --root "$DATA" host update status --json
"$CLI" --root "$DATA" host update continue --json
```

If a response is lost, inspect and continue the original operation. Do not rerun fleet setup, invent replacement identifiers, or resend an unknown action. Separately observe `host doctor`, `host status` and a bounded reviewed task; a committed configuration or launch receipt is not proof of useful work or restart durability.

## Browser connection remains a separate gate

The existing workspace/owner boundary requires native identity and private ingress, explicit pairing, workspace binding and exact local launch/action grants. Managed Git preparation grants none of those. Browser reading does not grant messages, task feedback or permission decisions, and those grants do not authorize repository merges or deployments.

The previous supervised first-team trial is historical evidence, not acceptance of this new installation path. The managed Git guide is available for a bounded operator trial below; independent Mac/Linux cold acceptance remains open. Do not follow old sealed-release commands or fabricate approval values. The [observable-plane reference](architecture/observable-plane.md) describes the owner boundary. The optional Telegram `fleet.yaml.seed` remains an explicit operator choice, not a requirement for this team.


## Managed Git browser trial

This trial reuses the existing managed installation, owner pairing, workspace
binding and first-team update operation. It is not an accepted newcomer path.
Complete the native checks and credential gate in Getting started first. The
website sign-in does not configure private ingress, provider credentials or Git
and MCP access on this computer.

In an interactive local operator terminal, keep the same `CLI` and `DATA` from
preparation and run:

```bash
"$CLI" --root "$DATA" host owner setup
```

The guide asks for this computer's exact private HTTPS origin, the absolute native
Tailscale executable, the website issuer, and the operational browser origin.
Use the exact issuer and browser origin displayed by the deployed website's
**Local approval values**. They are distinct from this computer's private HTTPS
origin. Tailscale and Serve must already route that private origin to the guide's
private Unix socket; the guide neither configures ingress nor changes OS policy.
If these prerequisites are missing, stop before approving anything and complete
that native configuration separately.

Choose the approved existing `OWNER/REPO`, a team name and an observed native
installation directory. The default is the persistent user directory only when
the native catalog observes it. Review the displayed configuration. Keep this
terminal open: owner serving stays in the foreground and does not enroll a
persistent owner service.

Follow the terminal prompts to pair the browser locally, prepare connected
storage, confirm the signed-in workspace proposal and approve the exact first-team
scope. Each approval is separate. In the browser, save the private computer
address under **Your computers**, open the workspace, provide the approved goal,
review the exact prepared plan and launch once. Reading or downloading instructions
has not performed these steps. Local setup approval does not grant ordinary
messages, task feedback or permission decisions.

If interrupted before launch, rerun the same guide with the same choices; it
retains matching approvals and refuses changed bindings. If any launch or update
request exists, inspect its original receipt and use the returned stable-CLI
`host update status` / `host update continue` recovery. The guide refuses to
replace a retained pending operation. Do not start another setup or replay an
unknown response. After successful launch, observe native readiness and useful
provider behavior separately. Closing the foreground guide disconnects browser
access; it does not stop bots or revoke approvals.
