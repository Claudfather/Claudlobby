# Revenue web-app team candidate

This single-file candidate composes a lead, a web-app engineer and an independent reviewer from shipped roles. It is intended for a bounded improvement to an existing approved GitHub repository, with tested task-branch PRs and a human deciding whether to authorize a merge or deployment. It has not demonstrated a business outcome, a selected reference deployment or a cold-host launch.

The lead uses `orchestration`, the engineer uses `software-engineering`, and the reviewer uses `code-review`. Existing clauDNA workflow plugins remain the package defaults; this example adds no plugin or role. The reviewer retains the shipped role's ability to write review scratch and memory rather than applying a misleading blanket Write/Edit deny.

The selected release packages this candidate; no repository-file fetch is needed. Template setup requires an explicit root, fleet name, approved `OWNER/REPO` and single-paragraph goal (1–2000 characters). It derives `com.claudlobby.<fleet>` as the service prefix and sets all three bots' repository scope while retaining the fixed policy. The example contains no credentials. Its account points to native Claude configuration; the operator must establish native authentication separately. GitHub MCP uses the shipped `github` fragment and its credential contract. Git/CLI access is a separate requirement: a GitHub browser login or a configured repository name is not verified host access. No Telegram channel, native remote-control connection, Vercel/Neon connection or private integration is selected.

The selected human path is core Plane/direct-owner access for input and visibility, after the operator independently establishes access and the relevant explicit action grants. The candidate does not initialize that authority or establish browser/ingress readiness. Bots coordinate through canonical task, assignment/report and message operations. Shipped orchestration and worker-lifecycle text still contains Telegram-specific instructions: composition omits channel flags, Telegram plugin selection and channel-derived tool grants, but does not rewrite that text. The lead's inherited `status`/`checkin` skills still contribute a Telegram reply allowance; this does not load the disabled plugin. The common mission explicitly skips Telegram posts/tools when no channel is enabled while retaining canonical reports. This does not supply unattended human alerts.

Follow the maintained [getting-started instructions](../../../documentation/getting-started.md) for release/prerequisite setup and the [fleet schema](../../../documentation/fleet-yaml-schema.md) for configuration. After the operator has established the required native/provider access and selected a compatible sealed release, the existing `fleet setup` command accepts either an authored `--config FILE` or this installed template. Using the release CLI, data root and native install directory obtained there, a template invocation is:

```bash
"$RELEASE_CLI" --root "$DATA" --fleet web-app fleet setup \
  --template revenue-web-app --repository example-org/web-app \
  --goal "Improve the approved checkout with tested task-branch PRs" \
  --install-directory "$USER_UNIT_DIR"
```

To prepare for inspection without activating, add `--plan-only` and omit `--install-directory`. This still installs the authored declaration and stages the whole host configuration through the sealed release owner; it does not change native supervision, select a release or start bots. Inspect the returned exact `plan_id` with `"$RELEASE_CLI" --root "$DATA" config diff PLAN_ID`. If approved, use `"$RELEASE_CLI" --root "$DATA" host activate PLAN_ID --install-directory "$USER_UNIT_DIR"` for that same plan, rather than rerunning setup and silently creating another plan. Activation's existing freshness check refuses changed declarations, project inputs or release bytes. Preparation does not establish runtime/provider/GitHub readiness, owner access or approval to launch.

To attach an existing named GitHub connection, add `--github-config connections.yaml --github-connection team-app`. The local file uses the existing `fleet.github.connections` [schema](../../../documentation/fleet-yaml-schema.md#fleetgithub--github_assignments--named-github-connections); it contains environment reference names, never secret values. Only the selected connection is copied, and all three bots receive explicit assignments for the chosen repository. Other repositories allowed by the installation remain unassigned. Git, CLI and MCP then use the same named connection. Omitting these flags retains the template's existing credential requirements. Selection does not verify the App installation or provider credentials.

Replace the example repository and goal with the human's approved choices. This activates the fleet and can start supervised processes and consume agreed provider usage; it is an operator action, not template validation. An equivalent manifest is reused, and canonical activation handles an already active plan. Different existing configuration still requires explicit `--replace-config`; template mode refuses any adjacent `projects.yaml`, including redirected or non-file declarations, rather than silently adopting project policy. Existing authored `--config` behavior is unchanged. Template selection alone grants neither GitHub access nor browser launch authority; named connection assignment is an explicit additional selection.

The common mission requires human permission for merges, deployment, new spending beyond agreed provider usage and destructive data changes. The `task-scoped-effects` guardrail wires the [canonical task permission request/check flow](../../../documentation/architecture/task-permissions.md) into every bot. The requesting bot waits for an exact recorded decision and checks the current permission immediately before acting. The lead's mission explicitly gives that policy precedence over inherited auto-merge/re-merge guidance. A human can decide through the host CLI or separately approved shared dashboard; an ordinary message or reviewer verdict is insufficient. No permission is pre-created by template selection.

The new guardrail retains the own-PR, reviewer-merge and admin-bypass prohibitions directly. This candidate therefore omits `no-merge-own-pr` and `no-merge-admin`, whose current text also forbids all manager merges even with permission. Those existing guardrails remain unchanged for other fleets. `no-push-main` and `no-destructive-git` remain selected, and native permission bypass is disabled. Approval does not override those restrictions, required review/CI, repository scope or provider credentials.

The action's task and assignment stay open and unchanged while awaiting its outcome; PR-ready work is reported as progress, and review uses separate work. Already closed work needs a new assigned follow-up task and a new permission. Bots preserve request inputs and IDs in their own persistent data before submitting and inspect the current decision when resuming, even if no notification arrived. The permission check executes nothing and is not a technical gate on arbitrary shell commands; the bot must enforce the exact constraints through the existing operation owner or report that it cannot proceed. The new composed policy still requires a live-bot observation in the isolated native trial; composition checks alone do not establish that a bot requests, waits and resumes correctly.

Without an adjacent `projects.yaml`, the existing project owner derives review-tier projects from repository scope. `fleet setup` installs only `fleet.yaml`; it does not install adjacent project or mission files. Revenue-facing preview/acknowledgment policy requires a separately authored [project declaration](../../../projects.yaml.example), using the authored config/configuration-owner path rather than template mode. This candidate uses an inline fleet mission and does not pretend to include that richer bundle. Revenue metrics are not supplied.

Static configuration/composition proof does not establish runtime behavior, provider readiness or useful work. Independent canary validation and the later cold-host/business trial remain required before those claims. Browser-guided team creation, repository preparation, credential delivery and launch authorization remain separate setup capabilities; this recipe does not complete that journey.

## Prepare working copies

After activation with a named connection, prepare each bot's assigned repository:

```bash
claudlobby --root /path/to/data --fleet web-app bot prepare-repository lead --repository example-org/web-app
claudlobby --root /path/to/data --fleet web-app bot prepare-repository engineer --repository example-org/web-app
claudlobby --root /path/to/data --fleet web-app bot prepare-repository reviewer --repository example-org/web-app
```

This clones into each bot's `projects/REPO` using its selected GitHub
connection. A matching checkout is reused without fetching or changing work.
Application scripts and dependency installation remain a separate task for the
team. In guided setup, a newly approved named connection enables the equivalent
**Prepare repository** step after launch. Then open the team dashboard and give
the lead its first bounded task using the existing approved message flow.
