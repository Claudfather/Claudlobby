# Visible gates: audit and implementation boundary

Audit date: 2026-11-03. This inventories the connected workspace, website entry,
canonical CLI reads and the shipped switch registry. It is an audit, not a new
permission registry or a claim that every deployment has been upgraded.

A blocked view must name its known prerequisite, explain why content is absent,
and give the next supported action. A successful empty read is different from a
refused, unavailable or unfinished read. The explanation must use the decision
already returned by the gate; it must not reconstruct authority in the renderer.

## Workspace and website

| Gate family | Disposition | Decision and presentation source |
| --- | --- | --- |
| No connected computer; expired or lost session | Fixed: Rooms/Tasks carry each source's known reason and connection guidance. Readable peers remain visible. | `plane/ui/connected-read-client.js` snapshot and `two-source-read.js` admission/result projection. |
| Connect challenge, website assertion, session issue | Fixed: failures before issue say no session issue was sent; a lost issue result retains uncertainty. No automatic resend. | `connected-read-client.js` current request stage and existing session invalidation. |
| Owner pairing and workspace binding | Existing explicit owner states retained: pairing required, waiting for local approval, sign-in required, expired and refused. First-time workspace guidance points to computer access. | `plane/owner_browser.py`, `plane/ui/owner-entry.js`, `workspace-enrollment.js`. |
| Pairing allegedly disabled | Deferred: the connected protocol does not advertise a typed `pairing_disabled` state. A generic denial/404 cannot establish it. Show the known connection failure and owner-access door instead. | No inferred state or new unauthenticated authority disclosure. |
| Website account admission, changed workspace generation or retired document | Existing explicit account-check/reopen guidance retained; no private clients admitted on account refusal. | `connected-workspace.js` account validation and retirement. |
| Unsupported connected tools | Fixed: visible text explains unavailable Team details, Recorder and search instead of relying solely on hover text. | `connected-workspace.js` existing supported-view allowlist. |
| First-team approval, goal and preview prerequisites | Fixed: setup displays the current missing approval, goal, preview or receipt step alongside the existing disabled controls. | `guided-setup.js` current state and existing eligibility predicates. |
| Setup receipt-identifier storage | Fixed: failed retention explains the storage prerequisite; launch remains unsent. Original identifiers and uncertain receipts are retained. | The existing `guided-setup.js` storage guard. |
| Read timeout, partial coverage, truncated history or genuinely empty data | Existing separate states retained; per-source explanations now survive aggregation. Older hosts cannot be described as complete task history. | `panel-state.js`, `two-source-read.js`, `app.js`, canonical task-page result. |
| Message, nudge, feedback and permission-decision grants | Existing per-capability status/help retained. Read access does not imply action access. Permission decisions remain separate from ordinary action grants. | `plane/owner_access.py`, action context, `work-loop.js`. |
| Invalid task action, absent current recipient or unknown action receipt | Existing disabled controls and explanations retained; no guessed manager, replacement request or replay. | `work-loop.js`, `action-state.js` and canonical action context. |
| Website sign-in configuration | Website companion fix: the same `enabled` value disables Google sign-in and explains the missing website configuration and administrator remedy. | Website `authConfigured()` → `GoogleLogin`. No configuration values exposed. |
| Website session pending versus ended | Website companion fix: pending stays loading; ended session hides private content and gives sign-in guidance while existing navigation proceeds. | Website `WorkspaceSession` and `WorkspaceEntry`. |
| Website workspace publisher configuration | Website companion fix: missing/invalid configuration names the website-side remedy; it does not send users to change host pairing. | Website `workspacePlaneConfig()` → `ConnectedWorkspace`. |
| Website storage, saved computers and address validation | Existing failure/uncertainty/reload guidance retained. Successful absence alone offers creation; saving an address is not proof of connection. | Website workspace/computer validators and request-state components. |
| Document ticket and child startup | Existing account-expired, changed-context, timeout and explicit-reopen guidance retained. Neither retry nor host pairing is automatic. | Website `openWorkspaceDocument`, current-attempt checks and startup state. |
| Installer, platform, repository/provider and setup publication prerequisites | Existing explicit unsupported/unavailable and versioned-instruction states retained. Installer assembly is not provider authorization, native consent or team readiness. | Website setup release lock/config and `SetupGuide`; core setup observations. |

## CLI, reports and operational switches

| Gate family | Disposition | Decision and presentation source |
| --- | --- | --- |
| Task recheck held by repeat interval, escalation or unknown delivery | Fixed: plain previews/results report held, waiting and uncertain counts/IDs. A replay says it has no fresh selection. These cases cannot print a false empty queue. | `task_recheck.py` selection and `commands/task_recheck.py`. |
| Armed opt-in switch described as off | Fixed: tables describe the shipped default separately from resolved on/off/unknown state. Summary counts use the same resolved rows. | `switches.py` `SwitchState`, table and summary. |
| Structured switch action guidance | Fixed: JSON includes the same derived label/arm/disarm properties as plain output, including optional-package overrides. | `commands/host_doctor.py` and `SwitchState` properties. |
| Unknown/unreadable switch observation in fleet status | Fixed: unknown rows and resolver failure remain visible with an inspection remedy. Native bot observations stay separate; a single-bot status does not gain a switch probe. | `commands/status_read.py`, `status.py`. |
| Owner-service prerequisites in switch arming guidance | Fixed: the host enrollment carrier includes the registry's existing extra configuration requirements. No enrollment or grant is performed. | `switches.py` carrier formatting and `config_extra`. |
| Disabled host job declaration/run | Included: show effective declaration separately from native execution and explain the existing disabled gate and inspection path. | `commands/host_jobs.py`, existing `host_job_operations.py` admission. |
| Recorded interrupted host activation | Included: plain status retains recorded interruption/recovery information and distinguishes an unsupported recovery step. It does not infer safe resume authority. | `commands/host.py`, canonical release/status result. |
| Withheld, truncated or unsupported report content | Included: plain output retains safe decoder reasons and explains future-capture limits; unknown storage remains unavailable, not empty. | `commands/report_read.py`, `report_payload.py`. |
| Malformed authored configuration | Included: safe file/line/column and validation guidance; no authored scalar, credential or arbitrary exception text is printed. | Config validation/explain adapters and existing parser metadata. |
| CLI root/fleet/generated identity and active frozen configuration | Existing refusal-versus-empty contract retained. **Deferred:** several adapters collapse distinct scope/schema failures to generic unavailable/conflict. Preserving finer causes safely needs a typed cause/remedy contract; forwarding arbitrary exception text can expose configuration or SQL. | `context.py`, `active_config.py`, `runtime_admission.py`, read-command adapters. |
| Plane absence, schema, pending/uncommitted queue | Existing source/status results preserve absent/unreadable/null and staged-versus-committed. No read initializes or migrates a database. Generic adapter cause loss is deferred above. | `plane/schema_state.py`, source state and plane status commands. |
| Native operator/interactive approval | Existing explicit refusals retained. No bypass, same-UID security claim or newly granted access. | `commands/operator_context.py`, `commands/host_owner.py`. |
| Reports, acknowledgements and task/assignment reads | Existing scope/cursor/identity refusal results retained; only a successful read can be empty. | Report/task read/write adapters and canonical reducers. |
| Degraded brief work/alerts/workstreams | Existing degraded/unknown sections retained instead of empty work or zero unacknowledged reports. | `brief.py`. |
| Explicitly disabled scheduled export/recheck | Existing OFF/no-work consequences retained. A successful no-op is not reported as performed work. | Session export and task recheck command gates. |
| Shared runtime opt-out flags | Existing central guard prints the flag, consequence and restore-default action. | `_runtime_scripts/lib-common.sh` `switch_is_on`. |
| Recording silencer in hot hooks/harnesses | Intentionally retained: repeated hook notices would flood output, and harness recording suppression must stay silent. Operator visibility is provided by switch registry/status rather than each suppressed sample. | Recording silencer and switch observation. |
| Credentials and environment tiers | Existing absent/empty/unknown/tier states retained without values. Stored credentials do not establish provider readiness. | `credentials.py`, config explain and doctor. |
| Optional sibling/package/platform capabilities | Existing missing-extra, parked/not-built, unprobeable and platform-disabled explanations retained. No probes for sibling contracts that are not shipped. | Switch registry, compatibility floor and doctor. |

## Shipped switch coverage

All 32 switches below use the existing registry/resolver and shared table/JSON
presentation. This list is an audit index, not a second definition of defaults,
arming steps or runtime state. Those remain in `claudlobby/switches.py`.

| Area | Switches audited |
| --- | --- |
| Session and context | `session-export`, `claudna-session-summary`, `claudna-harvest`, `claudna-precompact-reflect`, `boot-brief` |
| Work loop | `task-recheck`, `manager-checkin`, `worker-unassigned`, `code-audit-sweep` |
| Plane and recording | `plane-expire`, `plane-recording`, `plane-daemon`, `plane-view`, `plane-owner`, `plane-host-probe`, `plane-prune`, `plane-prune-system-events`, `registry-scan` |
| Lifecycle and transport | `pane-send-chunking`, `spindown-receipt`, `claude-staged-update`, `boot-capture`, `boot-capture-stamp`, `keepalive-limit-resume`, `weekly-worker-restart` |
| Integration and policy | `mcp-package-probe`, `vault-sync`, `update-siblings`, `shared-config-isolation`, `mcp-direct-launch`, `heavy-slot`, `public-write-guard` |

## Validation and limits

The changed gate families extend existing focused tests: connected client,
workspace and multi-source renderer; task recheck; switch resolution/doctor;
fleet/host/job status; report reading and configuration diagnostics. Website
tests distinguish configuration refusal, no saved addresses, pending sign-in
and an ended session. Shared switch tests exercise each registered carrier and
the on/off/unknown rendering contract without creating 33 independent harnesses.

The actual default Rooms view and Tasks view are also inspected in an isolated
browser fixture with one unconnected source, no known fleets and no host action.
Known expiry and readable-peer behavior have controller integration coverage.
The real hosts keep their existing settings and runtime versions. This source
audit does not certify native cold start, grant changes, activation, message
delivery or every possible deployment-specific failure. Those require their own
bounded acceptance evidence; unknown outcomes are never resent to obtain it.
