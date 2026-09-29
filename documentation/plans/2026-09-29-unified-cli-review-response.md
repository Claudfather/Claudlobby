# Response to the #1985 external review

The [review](https://github.com/Claudfather/Claudlobby/pull/1985#pullrequestreview-5356808449) covers `1c15661`. The fixes live in aggregate #1989; #1985 source stays unchanged. **Read from code:** dispositions below reflect verification against reviewed code and the aggregate changes. **Measured** checks and their limits are recorded in [acceptance evidence](2026-09-29-unified-cli-review-evidence.md). They do not establish deployment, current-head external approval, or full migration completion.

| Finding | Disposition | Change or reason |
|---|---|---|
| [1](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986514) | fixed | Fleet-state deletion requires an exact fleet and refuses foreign rows. |
| [2](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986534) | fixed | Post-stop purge checks nested repositories, untracked work, stashes, local commits and linked worktrees. |
| [3](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986547) | fixed | Library, event and bot authoring selectors use generated fleet context. |
| [4](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986559) | fixed | Teardown preserves diagnostics, distinguishes effects and kills its process group on timeout. |
| [5](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986574) | fixed | Focused declared-bot, authored-bot, fleet-move and purge refusal cases cover the named hazards; no combinatorial test matrix. |
| [6](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986590) | fixed | Recorder initialization/schema failures do not suppress independent fleet notification. |
| [7](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986605) | fixed | Pulse preserves stderr, returns a bounded warning tail and kills its process group after 120 seconds; shared activation lock stays through the effect. |
| [8](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986616) | fixed | Notifications use fleet-events provenance and the reader-compatible detail shape. |
| [9](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986628) | fixed | Converter failures preserve bounded diagnostics and backup location; failed data copy returns failure. |
| [10](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986640) | fixed | Runtime audit returns actionable remedies without dumping potentially secret native detail. |
| [11](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986653) | fixed | Canary guidance uses an independent root and whole-host activation; no single-bot composition shortcut. |
| [12](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986665) | fixed | Baseline and diagnostic exit-code documentation matches the current shared taxonomy. |
| [13](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986679) | fixed | Operator commands use selected native ancestry plus narrow composed denies; trusted local callers remain the boundary. |
| [14](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986693) | fixed | Doctor and credential checks respect generated fleet context. |
| [15](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986709) | partial | Non-activating onboarding ran through network download, hash lock, assembly and seed copies; missing Git commit instruction corrected. First authenticated bot and full cold-host acquisition remain unverified. |
| [16](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986728) | fixed | Current callers/coaching point to supported CLI and source harness paths; dated historical plans are retained. |
| [17](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986752) | fixed | Existing staging test now pins dormant unit enrollment=false; selected activation retires omitted enrollment. |
| [18](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986763) | fixed | Launcher test checks exact expiry argv and enabled/disabled execution. |
| [19](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986773) | fixed | Compose requires a marked disposable data root and a bound source/artifact; ambiguous exports refuse. |
| [20](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986794) | partial | Moved instruments now declare managers, create fleet directories and bind package resources; native enrollment rehearsals were not run by this review watch. |
| [21](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986813) | fixed | Purge refuses ambiguous retained tmux sockets; non-purge cleanup retains prior behavior. |
| [22](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986822) | fixed | Handoff timestamps are bounded on both control paths. |
| [23](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986829) | fixed | Host doctor explicitly reports native browser reaping disabled on macOS; no unsafe replacement classifier. |
| [24](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986838) | fixed | Existing Bash 3.2, supervisor and version scanners include moved harness scripts. |
| [25](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986858) | fixed | Spool reads no longer create storage and refuse absent wrong-root storage. |
| [26](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986875) | declined | No measured expiry bottleneck was supplied; retain transactionally rechecked correctness and defer an additional optimization/query batching design. |
| [27](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986898) | declined | Explicit fleet notify retains manager and configured-channel delivery. No new recipient/rate-limit policy is imposed by this review; newer #1989 evidence is queued separately. |
| [28](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986914) | partial | Event reads now refuse newer schemas. Keep shared exit 4 with distinct downgrade code rather than introduce another exit taxonomy. |
| [29](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986923) | fixed | Unbuilt-resource errors identify the required resource preparation step. |
| [30](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986937) | fixed | Scaffolding escapes YAML scalar values and refuses base-library shadowing. |
| [31](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986949) | declined | Config explain shares key names, states and tiers within the fleet, never values; no additional per-bot metadata secrecy contract is adopted. |
| [32](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986965) | fixed | Onboarding names manager install directories and the required selection. |
| [33](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986974) | fixed | Partial readable logs survive an unreadable source; decode replacements and bounded truncation are disclosed. Existing byte/line bounds remain. |
| [34](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136986992) | fixed | Repo pulls classify ahead/detached/no-upstream as skipped_blocked and retain safe cron skips. |
| [35](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987007) | fixed | Event coaching/grants use matching public shapes. |
| [36](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987029) | fixed | Bot coaching directs operator-only setup/activation to an external operator. |
| [37](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987044) | fixed | Coaching identifies harnesses as source-only developer tools absent from installed wheels. |
| [38](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987052) | fixed | Build preparation is attached only to composing harness tests; pure analyzers run without a built CLI. |
| [39](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987060) | fixed | Removed nonexistent plane daemon CLI admission tuple; native daemon test remains. |
| [40](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987074) | fixed | Recording-alert tmux fingerprint rejects the dash sentinel. |
| [41](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987087) | fixed | Retention/expiry day bounds and event-window overflow return invalid_argument. |
| [42](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987095) | fixed | Pulse result names an observed tick, not a health verdict. |
| [43](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987113) | deferred | Plausible persistent-timer catch-up race is documented; mandatory immediate waits break timers without a due tick. New #1989 activation findings are queued for a cohesive fix-forward pass. |
| [44](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987136) | fixed | Interrupt submits one Escape instead of Ctrl-C; submission is not verified cancellation, with no automatic resend. |
| [45](https://github.com/Claudfather/Claudlobby/pull/1985#discussion_r4136987154) | partial | Cold-move prerequisites and lack of active nesting are explicit. Keep active flat fleets flat; do not remove activation.lock, whose inode coordinates concurrent callers. |

Outside the inline findings, the aggregate removes dead status/uptime/promote handlers and five uncalled native enrollment installers, adds the breaking-change changelog, and restores explicit operator `host channels check/approve` with preserved managed policy. A broad pre-existing manager grant means the old diagnostic canary does not independently prove narrow grants. Optional MCP cache warming is now documented. Retired route hints remain documentation rather than compatibility aliases. #1980 remains an overlapping review reference; merge/closure and a curated eventual merge message belong to the operator.

The later #1989 review is still arriving against `cffa258a` and is tracked separately. Its activation, task and transport findings are not marked resolved by this response. No host settings or running services were changed during this review pass.
