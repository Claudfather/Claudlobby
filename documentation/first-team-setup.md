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

The previous supervised first-team trial is historical evidence, not acceptance of this new installation path. Until the managed Git browser setup path is integrated and independently cold-run, stop at that boundary rather than following old sealed-release commands or fabricating approval values. The [observable-plane reference](architecture/observable-plane.md) describes the owner boundary. The optional Telegram `fleet.yaml.seed` remains an explicit operator choice, not a requirement for this team.
