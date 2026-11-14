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
"$CLI" --root "$DATA" host owner setup --managed-owner
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
terminal open through Preview. `--managed-owner` also asks you to approve a
persistent local connection service in the same browser-created operation.
Select an owned private host-policy file through `CLAUDLOBBY_HOST_SYSTEM_YAML`
before running the guide; its owned parent directory must already exist with mode0700 and trusted ancestors. The guide
does not edit the shared default `~/.config/claudlobby/system.yaml`. It preserves
your other host jobs and namespace, shows the exact ingress configuration and
only writes the owner-service entry after `ALLOW-SETUP`. A conflicting existing
owner or changed file refuses without replacing it. This does not configure
Tailscale or grant browser messages. Omitting the flag retains foreground-only
serving and the manual reconnect procedure.

Follow the terminal prompts to pair the browser locally, prepare connected
storage, confirm the signed-in workspace proposal and approve the exact first-team
scope. Each approval is separate. In the browser, save the private computer
address under **Your computers**, open the workspace, provide the approved goal,
review the exact prepared plan and launch once. The guide then drains admitted
work and exits, releasing its runtime readers so the original updater can proceed.
With managed serving, the updater starts the approved connection at the same
private address; keep this browser view open while it checks the original
receipt and existing session. Connection may return while agents are still
starting. Wait for DONE, empty health errors and actual native readiness before
asking the team to work. Expired or refused access needs explicit reconnection;
the guide never issues new authority or repeats Launch. The printed stable-CLI
status command remains available for local recovery. In foreground-only mode,
after DONE run the printed `host owner serve` command to reconnect.
Do not rerun setup or Launch to restore browser access. A lost browser response
does not discard the retained original identifiers. Reading or downloading instructions
has not performed these steps. Local setup approval does not grant ordinary
messages, task feedback or permission decisions.

If interrupted before launch, rerun the same guide with the same choices; it
retains matching approvals and refuses changed bindings. If any launch or update
request exists, inspect its original receipt and use the returned stable-CLI
`host update status` / `host update continue` recovery. The guide refuses to
replace a retained pending operation. Do not start another setup or replay an
unknown response. After successful launch, observe native readiness and useful
provider behavior separately. Closing the foreground guide disconnects browser
access; it does not stop bots or revoke approvals. Stop a standalone foreground
owner server normally before a later update so it does not retain a runtime reader.

## Local project without a repository connection

The `local-project` template offers the same three roles for an existing local
directory. It needs no GitHub account, Git repository, PAT or MCP connection.
Select one real absolute project directory beneath the native user home locally; do not choose your home,
data/installation directory, native service directory or an ancestor of those.
Symlinked paths, authority/provider directories and wildcard path characters are refused. No project files are created or copied by setup.

After managed preparation, stop at the provider credential gate. Complete the
provider's interactive first-run prompts in the intended private profile before
activation; sign-in status alone does not establish readiness. Native ingress,
website login and exact local approvals remain separate prerequisites. This
option has not yet completed native/browser newcomer acceptance.

For the CLI route, replace each placeholder with its approved value:

```bash
"$CLI" --root "$DATA" --fleet local-team fleet setup \
  --template local-project --project-directory "$PROJECT_DIRECTORY" \
  --goal "Work only on explicitly assigned tasks within this local project" \
  --install-directory "$NATIVE_DIRECTORY" --plan-only --json
```

For a fresh browser-first installation, choose this route **instead** of the
CLI fleet setup above:

```bash
"$CLI" --root "$DATA" host owner setup --template local-project \
  --managed-owner \
  --project-directory "$PROJECT_DIRECTORY" \
  --account-directory "$CLAUDE_ACCOUNT_DIRECTORY"
```

`CLAUDE_ACCOUNT_DIRECTORY` is the real absolute directory of the intended Claude
profile, not a token. Setup checks directory metadata and never reads or copies
credentials. If the option is omitted, the local terminal proposes its
`CLAUDE_CONFIG_DIR` when set; otherwise the conventional default remains unchanged.
Confirm the displayed account path along with project access. The selected path
is bound into the approved local template and its native session identity; the
browser cannot supply or replace it. Existing retained approvals are not silently
retargeted. Provider authentication and useful native work still require observation.

The terminal approves read/write access to that exact local project; the browser
can describe a bounded goal and review the frozen plan, but cannot select a path
or supply configuration. Launch starts native services and may use the provider.
After the team is ready and messages are locally approved, tell the lead what to
do, for example: “Create TRIAL.md in this project with a short description of the
team, then have the reviewer check it.” The lead records that explicit human
request as a canonical task and coordinates implementation and independent
review. The setup goal does not start a task; setup sends no task, message or
feedback and does not grant ordinary messages. Shell/test commands still require ordinary tool approval; these project
permissions are not an operating-system sandbox. Existing host recorder/health
policy remains effective and must be reviewed in the native consumer plan.

Keep the original request/fingerprint/source identifiers across interruption.
Use the same receipt/status/continue procedure above, never a new setup request.
Closing the guide does not stop agents or revoke local access. Stop the exact
fleet and owned host/fleet jobs through supported public lifecycle commands;
cleanup and private network/grant revocation must be scoped before activation.
Revoke only this trial's message/setup/workspace/owner grants while a bound native
anchor (normally the recorder) still exists, then stop the recorder last. After
revocation, stop an enrolled connection service with the same stable CLI:

```bash
"$CLI" --root "$DATA" host supervision stop --host-job plane-owner --json
```

This stops only the committed owner service and proves its private endpoint and
lifetime lock quiet; it does not change Tailscale Serve or your other host jobs.
Retain a refused or unknown result rather than retrying a new action. After
all current native definitions are removed, local mutation admission deliberately
refuses an empty inventory; an idle updater is not a substitute ancestry anchor.
Do not restart workloads or weaken that guard solely to revoke stored offline grants.
