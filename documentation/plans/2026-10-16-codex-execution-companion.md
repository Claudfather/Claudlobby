---
title: Launch and supervise a scoped Codex worker in a mixed fleet
type: plan
status: draft
owner: Claudlobby maintainers
created: 2026-10-16
---

# Launch and supervise a scoped Codex worker in a mixed fleet

## Summary

Implement [Claudlobby#2149](https://github.com/Claudfather/Claudlobby/issues/2149) as a bounded execution adapter: one Codex worker receives canonical assignments from a Claude coordinator, accepts and reports through the existing CLI, survives its own restart, and appears with correct session joins in the Plane and clauDNA store. The first Codex worker has **no native channel, Remote Control, coordinator role or unmeasured MCP equipment**. The Claude coordinator retains its declared human channel and relays the worker's recorded results. This is an explicit supported topology, not a claim that Codex implements Claude's channels.

Use `agent_cli: codex`; `runtime:` is retired and cannot select execution. Preserve the current Claude path. Required behavior is gated on real native CLI evidence; desktop collaboration, fixture tests and a running process are distinct proof categories. The issue stays open until the full mixed-fleet week, including actual summary and harvest flow, is observed.

Planning may proceed now. **Do not begin this companion's source implementation by bypassing Half B.** Its reviewed candidate is still draft/unmerged and has native-service and complete 48-hour BEFORE/AFTER gates. Wait for that dependency's actual accepted merge, then branch from fresh main. No source change, test, native launch, model/provider request or new server was performed by this planning audit.

## Evidence and current state

Frozen source: Lobby P4 merge `94088c2bdf61c9daa72561e3d34ef6a28383068a`, tree `588f69d933634641a93efa9029a851ce1d0b3019`. The parent supplied green exact-head CI and post-merge 39/197/944 checks, each exit 0; this planning audit did not rerun them. The parent reports published Claudron v0.10.0 and clauDNA v0.29.0. clauDNA#404's behavior source subsequently merged as `3d6bfc60`; its v0.30.0 release is `4e4a5401116a7407b12f80c90a4437546cca4f56` with public native installation proof for all 438 files, 36 skills and six untrusted handlers. This clears the behavior-release prerequisite; execution source and the mixed week remain gated.

- The current issue snapshot describes the original `runtime` spelling and older line numbers. Actual source `config.py:729-731,1910-1915` and `validator.py:1300-1331` supersede those details: `agent_cli` is canonical, and Codex execution is deliberately refused today.
- Epic `documentation/plans/2026-10-04-runtime-neutral-observability-plan.md:711-752` owns the companion boundaries and C1–C5/C7–C9 prerequisites. Its P4 section `:1798-1842` separates owner adapters from this composition/launch work. Preserve its amendments and the canonical run-log corrections.
- The P0 final-report hash is `4d13c914c1227807d736293573b24e10ac9f055a504f298c6955288369bc9cb7`. Native Codex **0.162.0-alpha.17.2 on macOS** supplied startup/resume/compact hook sources, SessionStart stdout delivery, an owner process observation, explicit hook command arguments, private marketplace installation, tmux submission/queueing and exact-ID resume. It did not prove Linux/floor-host operation, native channels, native plugin-agent registration, subagent execution or filesystem permission enforcement.
- Saved P0 launch metadata shows interactive `--no-daemon --strict-config --no-alt-screen -a never -s workspace-write`. The separate read-only smoke is not evidence that the interactive run was read-only. Saved `exec --help` documents `--profile`, read-only/workspace-write modes, config overrides and separate hook-trust bypass. Exec help does not establish an interactive profile carrier; measure that surface before using it.
- P0's native `permission_mode: bypassPermissions` enum is not a filesystem-sandbox verdict. PermissionRequest and subagent hooks were wired but unobserved. The one-level owner-PID relationship is version/platform evidence, not a permanent process-depth rule.
- P0 C5 measured JSON log export to loopback, `environment` becoming resource `env`, matching foreground `conversation.id`, two API records without that ID, and another unmatched OTel conversation ID. It measured zero metrics/traces while logs flowed. It did **not** prove disabled logging from `metrics_exporter="none"` alone, universal one-to-one joins, exhaustive egress confinement or the shipping P2 receiver's complete Codex mapping.
- P4 clauDNA's corrected native golden is 20 records; its reader keeps aligned `user.text` content items and commentary/final assistant phases. The earlier 17-record report description is superseded by that released owner implementation. Do not create a competing rollout parser here.
- Half B's authorized literal native **Claude** `/clear` test **passed** at exact head `5f9798810034209a21c6f84ffebd857184bf34e6`, Claude Code 2.1.301, clauDNA 0.28.1 and sealed Claudron 0.9.0. The new native session replayed the same acceptance/report request IDs, retained one accepted event and one communication under the first committed UID, and left the second UID lookup empty. Both sessions closed; six fixed-localhost fake-provider requests involved no real Claude inference. This is bounded native CLI/receipt protocol proof, not real service/supervision or elapsed-window acceptance, and establishes no Codex `/clear` support. Public proof labels/hashes: `halfb-native-literal-clear-report` SHA256 `9496ce862e76b5197a7a362343b2c90bff191a73c66162c07545afaf9f445891`; `halfb-native-literal-clear-result` SHA256 `7b1cd34e236e763dba2010be609eef9e913e40923eff957b6d604242ca870c6a`; `halfb-native-literal-clear-pr-summary` SHA256 `6b3bac4f5892ddfc4859972d7a74c9a0e83dcd50f73aa1da8ef9bb6feaf12d5e`.

All private locators are kept in the audit receipt. Public promotion uses logical evidence labels and hashes; no personal absolute path, raw prompt/tool/rollout/auth content or real identifier is promoted.

## Counted source audit

The reproducible private inventory scans **170 tracked files** at the frozen object: 32 targeted execution/composition/transport files, the complete fleet-skill tree (**55 skill directories, 78 files**), protocols, templates, instruction files and governing docs. Counts are lexical matching lines/files, not unique defects or an exhaustive review of all repository code. Full path/line hits and source hashes remain in the private audit.

| Core category | Matching lines / files | Concrete consequence |
|---|---:|---|
| Claude CLI flags | 33 / 7 | Separate typed Codex argv; do not pass Claude flags, effort, channels or display-name options. |
| Claude config paths / keys | 74 / 10 | Isolated Codex home/config carrier; no Claude consent/trust or settings grants on that branch. |
| Claude binary/auth/session assumptions | 41 / 4 | Resolve and verify the selected executable; keep auth and session identities runtime-specific. |
| Claude plugins / Telegram tools | 31 / 9 | Host-specific install/readback and explicit no-channel topology; no copied tool matcher. |
| Runtime/session vocabulary | 27 / 6 | Reuse `agent_cli`/join contracts; add measured Codex caller env after Half B, not a second identity system. |
| Pane and lifecycle predicates | 63 / 7 | Native TUI adapter plus positive evidence; Claude glyphs, menus and reload commands cannot classify Codex. |

Fleet-skill matching-line counts are separately recorded: three flag lines/two files, 12 config-path lines/five files, eight binary/auth lines/four files and 13 plugin/tool lines/11 files. The first supported composed operation guide is **fleet-ops**. The other 54 canonical fleet skills are not silently advertised as supported on Codex; engineering skills come from the actual #404 marketplace release. Additional fleet skills require an explicit capability disposition before equipping a Codex bot. Canonical Claude content is retained.

| Current source seam | Observation and required change |
|---|---|
| `validator.py:1300-1305` | Keep the refusal until the narrow adapter is complete; replace it with capability/config validation, not unconditional acceptance. |
| `composer.py:1058-1082` | Always builds `CLAUDE_FLAGS`, including default acceptEdits; Codex gets a separate typed launch description and origin-aware validation. |
| `start-bot.sh:115-162,266-335` | Global Claude consent, trust, binary and plugin startup are unconditional. Branch before these writes and use the Codex-native preflight/launcher. |
| `composer.py:598-630`; `config.py:912` | Account lookup defaults to a Claude config directory. Codex auth/home is not derived from this fallback. |
| `composer.py:3268-3337`; `config_staging.py:78-107` | Runtime rendering emits CLAUDE.md/.claude settings and stages only Claude skill links. AGENTS parity in repository instruction tests is not composed-bot discovery proof. |
| `path_audit.py:84-89,267-272` | Wiring enumeration omits future Codex files; extend stage/install/rollback/path scans together. |
| `lib-common.sh:3570-3587,4131-4178` | Claude prompt/menu patterns and cache/slash-command checks are not Codex capabilities. Unknown is not idle or a successful invocation. |
| `start-bot.sh:393-427`; `keepalive.sh:686-712` | Channel-less currently means no poller to await; that is not initialized Codex. Claude reload and bridge-heal paths must be inapplicable explicitly. |
| `system.yaml:395-480` | Current default hooks include Claude Telegram, permission/guard and StopFailure contracts. Codex gets a distinct declared pack; mandatory restrictions cannot be dropped by string filtering. |
| `message_transport.py:147-181` | One submission is not receipt/delivery; its isolated native environment currently carries no destination runtime. Carry the frozen recipient `agent_cli` to the correct native pane adapter without ambient inference. |
| `library/protocols/worker-lifecycle.md` | Requires a direct Telegram tool and named Claude controls; add an explicit channel-less-worker/coordinator-relay branch, preserving the canonical CLI transitions and Claude behavior. |
| `composer.py:2531-2589` | Inline Claudron snippet is the Claude shape. Add exact released Codex snippet parity, including host selector, timeout and replacement identity. |
| `pyproject.toml` / `contracts/claudna.ref` | Current pins are Claudron v0.9.0 and clauDNA v0.28.0. Published owner releases do not update these consumers automatically. |
| Half B `5f979881.../operation_context.py:33-48` | Pending dependency maps only `CLAUDE_CODE_SESSION_ID`. Add measured Codex env selection after it merges; preserve NULL and immutable-replay behavior. Current main lacks that caller-session path. |

## Architecture and supported surface

### Runtime selection, typed configuration and launch

Keep the existing two-value `agent_cli` vocabulary. Introduce only a small execution adapter boundary for binary/version preflight, artifact rendering, argv, TUI operations and lifecycle capability checks; avoid a generic agent framework or alternate task loop. Keep shared native supervision, per-bot sockets, activation locks and selected-release ownership.

Add a strict Codex execution submapping through the existing strict-mapping/default registry. Its first public fields are sandbox mode and approval behavior, restricted to the policy profile that passes native enforcement tests; initial candidate is `workspace-write` with approval `never`, matching the measured interactive invocation. Home is derived per bot rather than supplied through a Claude account fallback. Do not expose arbitrary native config dictionaries or free-form extra flags. Unknown fields, wrong types and unsupported combinations are errors before staging. Preserve the current Claude schema and defaults.

Validate the **effective** model, effort, channels, Remote Control, permissions, accounts, integrations, hooks, skills and inherited defaults with source origins. Reject inherited Claude model aliases, `model_strategy`, nonempty `extra_flags`, Claude permission modes/bypass flags, unsupported custom grants/hooks and unmeasured effort settings. An authored override or valid supported configuration is required; do not silently strip them. An explicit Codex model ID or deliberately selected native default must pass same-host preflight; a configured ID is not proof of the served model. Do not translate opus/sonnet/haiku or Claude effort enums into invented Codex names.

Resolve an explicit Codex executable beside the Claude resolver, without falling back to Claude or an unrelated PATH binary. Initial supported native floor is the measured `0.162.0-alpha.17.2` feature set; record executable hash, complete version and OS/architecture. Reject older or incompatible feature sets. Higher version strings and another platform do not inherit acceptance automatically: run strict-config and the affected native conformance cells before admitting an upgrade/platform. Lobby's Python floor is **3.10**, shell compatibility includes macOS Bash 3.2; clauDNA's separate 3.9 floor remains its owner's gate.

Render a structured launch description with an argv array, not interpolated shell fragments. A small owned launcher consumes the validated array and uses exec with the selected environment. Preserve argv tokens containing spaces, quotes and Unicode. Scrub inherited top-level Claude/Codex session markers immediately before native exec, then let the new native session set child markers. No model prompt, auth value or user input is evaluated as shell code. Resume uses a validated exact native session ID tied to this bot/launch; never `--last`, a directory-wide rollout scan or another bot's ID.

### Native home, configuration, trust and authentication

Derive a private per-bot CODEX_HOME inside the owned bot tree. Directory mode 0700, auth files 0600. Do not copy the operator's Codex config, plugins, trust state, memory or production authentication automatically. Existing private auth-copy authority applies to **test-only isolated canaries**. Production enrollment requires an explicit operator provisioning path for that bot/home using the native login flow or an explicitly authorized credential source; login absence is a named preflight failure, not a fallback to API billing or another account. Remove ambient provider overrides from the native child as required to preserve the declared subscription/provider choice, while retaining separately declared service credentials only for their intended equipment.

Separate mutable native configuration/auth/trust/cache state from compositor-owned artifacts. **The tested immutable `fleet.config.toml` profile candidate failed:** across the actual interactive probe sequence, its mode changed from 0400 to 0600 and native TUI preference content was added. The receipts do not isolate which individual arm wrote that preference. Native help supports the profile grammar and the strict unknown-field control rejected its input, but the authenticated positive arm exited 1 during workspace-routing discovery before settings observation or a model prompt. This disproves the tested carrier's immutability; it does not disprove every possible isolated carrier. **No alternative carrier is selected or proven.** Reopen F2149-5 and measure a replacement's effective interactive settings and unknown-field control before source admission; do not substitute ignored configuration, overwrite mutable native state, or relax policy. Stage and reconcile individual managed files only; never regenerate or delete the whole native home.

Required plugin release/ref/source/layout must match the selected sealed release before startup. Use measured native marketplace grammar, exact released refs and readback of installed version/tree/cache contents. Do not reuse Claude cache paths, grep Claude's known-marketplaces file or run an unpinned latest update during Codex boot. Missing/incorrect required plugin or unresolved trust blocks readiness rather than producing a healthy-looking worker.

Hook trust is distinct from workspace trust and filesystem policy. Use the native review/trust mechanism on exactly the inspected generated/installed definitions; changed hashes require new trust. Never copy the P0 hook trust hashes or add the hook-trust bypass to production defaults. Any automation of native trust must be a separate explicit policy decision, grounded in measured native controls and selected-release hashes. Headless startup reports trust-required without a restart storm when operator provisioning is incomplete.

### Files, instructions, hooks and MCP

Render the bot's CLAUDE.md and AGENTS.md from the same approved host-aware instruction body, preserving byte parity between those two files. Host-specific procedure choices are explicit; neither file promises tool grants from frontmatter. Include complete fleet-ops loading instructions and actual installed #404 tool-adapter/root pointers; do not invent a native slash registry. For the first worker, load the composed fleet-ops procedure explicitly from its known file before acting, and record that read. Native automatic skill discovery is separately measured by #404; no blind port of all 55 fleet skills.

Measure what the **native CLI** actually loads in a fresh bot and assigned checkout, including parent instruction ancestry, a nested AGENTS file, a context-budget control and an unrelated parent guide. Use distinct synthetic markers and actual load/context evidence, not file existence or a desktop session. The first canary root has no production ancestor instructions. If supported instruction isolation cannot be established for a proposed deployment root, reject that layout rather than assume a Claude exclude setting works on Codex. Add all managed Codex files and skill links to render, staging, install, path ownership, rollback, wheel/sdist and cleanup tests together.

The Codex hook pack has explicit owners:

- The actual released #404 marketplace supplies clauDNA store/startup behavior. Compose no duplicate store handlers.
- Claudron v0.10.0 supplies the complete Codex session-loop snippet with explicit `--agent-cli codex`. Update the consumer pin/capability table/human reference and compare the entire rendered snippet to the pinned owner output in tests. Keep the owner's non-injecting PreCompact identity and SessionStart(compact) prompt behavior, including its once-per-session marker semantics and native SessionEnd timeout 3/per-Git budget. Do not reimplement knowledge logic or import `claudron.*` into the compositor outside the repository's allowed seam.
- Lobby supplies actual measured vitals and tracked-prompt receipt hooks, plus any narrowly required launch/session receipt. Existing P4 field parity does not prove actual tracked delivery after Codex paste framing; native wire-hash/byte receipt equality is mandatory.
- The package-owned default hook configuration has a separate Codex pack selected by `agent_cli`; keep the Claude pack unchanged. Unsupported operator-declared restrictions are validation errors. Do not wire Telegram in/out, RC relay transcript readers, Claude StopFailure/usage-limit logic, unobserved PermissionRequest/subagent events or Claude permission decision hooks onto Codex.

Keep all hook input untrusted, stdout silent except the owner's documented context-injection handlers, and telemetry/event failures bounded and nonblocking where their contracts require it. A native per-handler timeout does not prove an entire multi-handler or multi-Git shutdown completes within that timeout.

The first Codex worker declares **no MCP servers**; reject inherited/effective MCP equipment until supported explicitly. Do not claim `.mcp.json` or enabledMcpjsonServers configures Codex. A subsequent bounded stdio-MCP cell may add support only after inspecting the actual native config grammar and observing a disposable known tool call, declared env resolution, working directory, missing executable, timeout, disabled server and trust behavior. HTTP transports, OAuth, channels and Claudron's parked/unbuilt MCP door remain unsupported unless separately shipped and measured. Never install an unmeasured MCP server as a prerequisite merely to reach knowledge already available through the Claudron CLI.

### Policy enforcement prerequisite

Prompt instructions, a hook permission enum, Claude allowed-tools lists, read-only/workspace-write labels and successful config parsing are not enforcement proof. Before admitting a Codex worker, run a native dummy-file matrix on the exact host/binary/profile: own workspace read/write positive controls, forbidden sibling read/write through direct commands and Python, outside-workspace writes, secret/auth paths, symlink escape, operator-only CLI actions and exact permitted assignment/report doors. No real secrets or production files are used. Preserve the existing fleet policy, vault writer topology and native operator-ancestry checks; canonical CLI permission checks remain authoritative for their own operations.

If the available native policy cannot express a declared restriction, **reject that bot configuration** and keep the affected route unavailable. Do not grant broad roots, expose the Plane ledger for arbitrary writes, enable danger-full-access/bypass, or strip restrictions to make a canary green. A different isolation model or relaxed policy requires an explicit owner decision and a revised plan. This prerequisite may block execution even if source plumbing is ready; no confinement claim is made today.

### First mixed-worker topology and lifecycle

Use one Claude coordinator and one Codex worker in an independent canary root, with distinct labels/unit prefix, separate Plane state, declared throwaway channel for the coordinator and isolated accounts. Codex has `channels: []`, Remote Control false, no Telegram token/state/plugin, no coordinator check-in or direct human channel. Validate those effective settings; an inherited channel is an error, not a hidden omission. The coordinator alone performs its declared human relay. Update the worker-lifecycle protocol's two-channel instruction explicitly for this topology: the worker writes the canonical linked report; the coordinator may relay that recorded result through its own authorized channel.

The manager uses task admit → task assign → assignment deliver; the worker inspects and accepts that exact assignment before progress or terminal reports. Retain UUIDs; unknown delivery never authorizes a fresh-ID resend. Carry the frozen recipient runtime through message context/transport to its pane adapter. Require receiver-side wire hash/byte equality and the canonical received receipt; a successful send, idle glyph or model reply alone is not delivery. Unlinked communication still uses the existing message/fleet-report doors, never a second task ledger or invented channel server.

Derive caller session UID from the measured `CODEX_SESSION_ID` after Half B lands. When CODEX_THREAD_ID is present, require consistency; malformed/conflicting/missing input records unknown/NULL according to the existing contract rather than borrowing a Claude ID. `(agent_cli, native_session_id)` remains the session join. Session environment labels are attribution inputs, not authentication or permission grants; retain the existing context/identity and native operator-ancestry checks. Preserve receipt format, first committed attribution and replay identity; fresh sessions and supported restart/reset cases must not duplicate effects. Retain the passed Claude literal-clear regression without inferring that Codex implements that command. Register keyframes and join visible brief/Plane/store readbacks through existing owners; do not write the database directly.

Codex readiness requires a live owned process, its current native SessionStart identity and an actually usable native input state; channel absence alone cannot mark it ready. Use versioned native idle/busy/held/menu parsing grounded in live captures. P0's `›` observation is a starting fixture, not a universal regex. Menus/auth/setup, a held partial prompt and unknown frames receive no automatic Enter/reload. Queue and submission are separate supported operations, each with actual receipt evidence; never reuse Claude menu selection or Claude reload/checkin commands.

Keepalive restarts only a dead owned session through canonical supervision, with the current crash-loop/activation guards. No Codex Telegram bridge-heal or Claude usage-limit auto-resume. Unknown pane state stays unknown; marker recency does not prove idle. Plugin changes require the supported restart/activation path until native reload is observed. Graceful stop invokes the installed #404 session handoff through its measured Codex procedure and verifies the owned artifact; preserve `.claude/session.md` compatibility. Native exact-ID resume uses a current, bot-owned session receipt; validate ownership and recency, and distinguish resuming a thread from loading a handoff into a new session. Never create two live sessions/writers for the same bot. If no verified resume target exists, a new session and explicit handoff recovery is a named degradation, not a claim of exact resume.

### Telemetry and summaries

Reuse the released P2 receiver/mapping and strict fleet telemetry schema; do not fork them in this adapter. ON mode requires the shipped Codex mapper, actual loopback JSON-log export, per-bot `environment`/env carrier, prompt logging policy and unknown/missing conversation-ID cases preserved as unknown. Metrics and traces remain disabled as measured unless separately authorized. Do not synthesize joins for unmatched IDs or silently redirect to hosted exporters. Native analytics is explicitly disabled through a tested config carrier.

OFF mode requires its own actual strict-config and receiver-silence control; P0's metrics-none setting did not prove logs were off. Do not guess a TOML exporter value or call an omitted block silent. If an OFF configuration or an ON mapper is unproven, reject that telemetry mode. The first bounded launch can use a proven OFF mode; it cannot claim ON telemetry acceptance or satisfy P2's separate seven-day floor-host gate. Full declared telemetry support stays blocked on that owner's shipped artifact and evidence.

Session summaries/harvest remain clauDNA-owned. No transcript-digest replacement, direct rollout parser, new summarizer or LangChain layer is added. Source verification may disable summary/harvest to avoid unintended inference, but such a short canary cannot prove their flow. The final mixed week must show actual owned summary and harvest records for both runtimes under the existing provider/spend policy. Current source reviews and protocol tests remain Codex-only or fixed-local-provider; real Claude model uptake is not inferred from a fake-provider trace.

## Decision Forks

| ID | Options | Selected choice / rationale | Ratifier / status |
|---|---|---|---|
| F2149-1 | New runtime vocabulary / agent_cli | Existing agent_cli, unchanged joins and retired-key refusals. | Delegated source scope; locked. |
| F2149-2 | Invent Codex channels / coordinator relay / no fleet work | Channel-less Codex worker plus declared Claude coordinator, canonical assignments/reports and explicit relay procedure. | Delegated engineering choice; locked. |
| F2149-3 | Reuse global auth/config / per-bot native home | Per-bot home, operator production provisioning, test-only private auth reuse; no automatic production copy. | Auth boundary retained; locked. |
| F2149-4 | Translate Claude grants / bypass / prove native policy | Native enforcement matrix and fail-closed unsupported configurations; policy relaxation is outside this choice. | Fleet policy owner authority retained; locked. |
| F2149-5 | Overwrite native config / managed profile / another isolated carrier | Tested immutable-profile candidate failed; no replacement selected. Measure actual effective settings and mutation boundaries for a replacement. | Reopened on measured feasibility failure; source admission blocked. |
| F2149-6 | Port all fleet equipment / scoped first worker | fleet-ops guide and #404 marketplace workflows; no unmeasured MCP or remaining 54 fleet skills. Reject incompatible inherited equipment. | Delegated source scope; locked. |
| F2149-7 | Reparse rollouts / exact owned receipt / resume latest | Exact bot-owned native session receipt and owner store interfaces, never last/global scans; named handoff degradation if absent. | Delegated engineering choice; locked. |
| F2149-8 | Duplicate telemetry / consume P2 | Consume shipped mapper/schema, separate actual OFF/ON gates; no waiver of P2 observation. | Existing owner split; locked. |

## Implementation plan

### Dependencies and blocks

| Dependency | State / required evidence before proceeding |
|---|---|
| P1 Half A and Lobby P4 | Merged; verify fresh main includes actual 94088c2 and P1 contracts. |
| P1 Half B #2360 at reviewed 5f979881 | **Draft/unmerged. Blocks this companion's source start.** Literal native Claude `/clear` passed with the bounded fixed-localhost proof above. Remaining blocking acceptance: real selected-release service/supervision and complete 48-hour BEFORE plus 48-hour AFTER windows, then actual accepted merge. No duplicate caller-session/replay implementation. |
| Claudron v0.10.0 | Parent reports published; verify release/tag/tree and codex-session-loop capability, then bump consumer pin/docs/parity. |
| clauDNA v0.30.0 / #404 | Actual behavior release published and public installation verified: 438 matching files/modes, 36 skills, six untrusted handlers. Native action evidence and final packaging are separately qualified; catalog selection retains 17/18. Consume this actual version only after Half B acceptance; live mixed-runtime acceptance remains open. |
| Native carrier/policy | Tested profile immutability failed and positive settings delivery remains unproven. Select and measure a replacement, then complete instruction-loading, native-policy and auth preflights before Codex admission. No same-host subagent support is assumed. |
| P2 telemetry | ON support depends on its released Codex mapping; its floor-host seven-day gate remains separate and unwaived. |
| Final mixed week | Blocked on accepted source/releases, exact live root/fleet/channel authority, provisioned accounts/policy and elapsed observation. |

### Ordered source slices

1. **2149-A: contract and admission, S/M.** After Half B's actual accepted merge, branch from fresh main. Land the sanitized audit/plan, explicit supported topology and capability matrix. Add strict Codex config parsing/origins, runtime-aware model/flag/equipment refusals and runtime-aware binary/version preflight. Keep Codex validation closed while its required capabilities are unavailable. Run the small prerequisite native config/profile and dummy-policy cells in private test state before choosing unsupported native syntax in source. Reforge only the affected choice on a concrete failure; do not ask for routine owner review permission.
2. **2149-B: artifacts and owner consumption, M.** Add host-selected render/stage/install/path/rollback files, per-bot home/profile/hooks/argv and parity AGENTS. Bump Claudron to actual 0.10.0 and clauDNA to the actual released #404 version, update the compatibility reference/contract fixture and test entire owner snippet/cache identity. Add the finite Codex default-hook pack and explicit fleet-ops/worker-lifecycle adaptation. Keep unsupported hooks/MCP/skills rejected and the Claude render unchanged except required owner-pin contract updates.
3. **2149-C: native launch and task loop, M.** Implement typed exec, isolated environment/auth preflight, actual native readiness and recipient-runtime-aware pane transport/classification. Add measured Codex caller env to Half B's resolver with consistency/null controls. Drive actual canonical assignment receipt → accept → progress → complete plus message/report replay. Implement native own-process stop/restart/exact-ID recovery and handoff fallback; do not import desktop subagent tools into the CLI. Any delegation-dependent #404 route must cite its same-host capability proof or remain unavailable.
4. **2149-D: telemetry/profile and native integration, M.** Consume P2's released Codex mapper when available; measure actual OFF and loopback ON, no prompt leakage, missing/unmatched join controls and no metrics/traces. Verify packaged startup context delivery, all intended hooks exactly once, actual installed skill reads and a plain-utterance native routing arm as required by #404. Run real macOS and Linux/floor-host service/tmux/activation/restart cells for each claimed supported platform. No stub-only protocol result substitutes for service acceptance.
5. **2149-E: convergence and release, M.** Fresh separately prepared before/after exports, full required/default/harness checks, Python 3.10 floor, Bash 3.2/Linux shell checks, installed wheel/sdist and affected parity tests. Compare names, counts and exit codes with interpreter/dependency/source-byte/mode proof. Independent Codex simplify, source/spec/evidence reviews fold findings, followed by exact-head CI. Open/attach draft-first PRs, land only eligible reviewed source, seal a release and read back artifact/tag/tree/pins; source merge is not activation or issue closure.
6. **2149-F: independent mixed-fleet week, external gate.** An authorized operator stages/inspects/activates that exact release in the independent root. Both actual runtimes must accept and complete assignments with correct first-attribution/replay joins, visible brief/Plane/store sessions and actual summary/harvest records. Observe at least **604800 seconds** with dated UTC endpoints, heartbeat/pulse coverage, owned restart causes and recovery results. Missing/unreachable evidence is unknown, never zero incidents. Keep #2149 and #404 open until their criteria pass; do not borrow Half B's 48-hour windows, P2's seven-day gate or a shorter protocol canary.

### Sequencing

Critical path: Half B accepted merge → A prerequisite/admission → B owner releases/artifacts → C native execution → D integration evidence → E reviewed release → F actual week. P2 observation and #404 source work can advance independently under their owners. A/B may be one coherent PR if bounded; C should stay independently reviewable because transport/liveness changes affect current Claude bots. Do not stack on the parent's dirty ledger or the unmerged Half B branch.

## Test and canary plan

- **Parser/config:** missing/unknown/null/falsy values, inherited Claude defaults, retired runtime spelling, unsupported effective equipment, model aliases, native floor/version-feature mismatch, absolute binary/argv quoting, mode/permission refusals and unchanged Claude output. Static tests must exercise public config plan/diff, not only new helper functions.
- **Artifact lifecycle:** generated AGENTS parity and budget, paths with spaces, poisoned cwd names, installed CLI-less render, source/pin/cache parity, wheel/sdist files, stale/foreign hook replacement, changed hash trust-required, activation rollback and preservation of native auth/trust/cache/notes. A rendering test cannot establish native loading/trust.
- **New-user setup:** any change to host setup, onboarding or the documented native provisioning commands requires the repository's cold-host procedure: history-free export, scrubbed environment, documented commands verbatim, counted exploration events and a stop at the credential gate. An already-authenticated developer checkout or a parser test is not that evidence.
- **Policy/auth:** the native dummy-file matrix above, positive and negative controls, no actual secrets. Test auth absent/expired and unintended provider overrides without printing credentials. Confirm the test-only auth copy leaves original metadata unchanged; production provisioning is not exercised without its separate operator authority. A rejected/refused observation is not retried by scanning other state.
- **Instructions/workflow:** mandatory actual native AGENTS/context marker proof, complete fleet-ops read, real #404 startup stdout delivery and handoff artifact. Explicit skill-name probes, unhinted automatic routing, model catalog selection and desktop dispatch are separately labeled; only actual same-host spawn/wait plus known-bug finding proves a delegated route.
- **Transport/TUI:** actual native idle/busy/held/menu captures, full/chunked/Unicode/large synthetic assignment, receipt hash/length equality, queued turn, concurrent sends, missing receiver hook, partial/unknown sends, timeout, wrong runtime, lost Enter and refusal controls. No blind resend or menu Enter. Claude's current transport regressions remain in the affected set.
- **Identity:** equal native SID in two runtimes yields distinct session UIDs; native hook/store SID matches the main Codex shell env; optional THREAD consistency; missing/malformed/conflicting/Claude-only env records NULL. Repeat actual assignment/report/message requests across new and resumed sessions; preserve Half B's first attribution and one committed effect. Retain the passed native Claude literal-clear regression. A Codex clear/reset command remains unproven and needs its own native observation before support is claimed. Unobserved native subagent env remains unproven.
- **Services:** actual selected-release host setup → config plan/diff → operator activation → native service/tmux startup → assignment loop → graceful stop → dead-session recovery. Kill only uniquely owned canary sessions/services; preserve WIP and unresolved delivery. Observe session-end owner budgets without promising whole-shutdown deadlines. Linux/floor-host conformance is mandatory before claiming that deployment platform.
- **Telemetry:** strict config accepted and setting reaches actual native runtime; OFF receiver silence, ON loopback JSON accepted, wrong/protobuf protocol refusal, env carrier, prompt privacy, unattributed/missing conversation IDs and no forced joins. No new intake or hosted dependency. P2's independent observation remains pending until its owner supplies it.
- **Final week:** real events and summaries/harvest for both runtimes, retained request IDs, restart causes, heartbeat coverage and dated duration. The current Codex-only review constraint is preserved in source work; fake Claude replies or disabled summary/harvest do not clear this final live criterion.

Use synthetic public fixture content, private HOME/TMPDIR, distinct bot/unit/channel identities and a per-run budget/owned process list. Source tests run unsandboxed as the repository requires, in separate once-prepared history-free exports. Do not run from a live fleet root. Cite actual CLI/service/native observations in each runtime PR body; report source/unit, fixed-provider protocol, native model and real service/live evidence separately.

## Verification checklist

- [ ] Half B's genuine merge gates are met and its actual merge is in the fresh implementation base.
- [ ] agent_cli is the only selector; unsupported effective flags/models/grants/hooks/equipment fail explicitly.
- [ ] Target native binary/version/platform, interactive profile and settings delivery are measured; no exec-help/desktop substitution.
- [ ] Native policy matrix preserves declared restrictions; production auth is operator-provisioned, with no automatic copy/billing fallback.
- [ ] AGENTS/context, required plugin/pin/cache/trust, owner hooks/parity and installed workflow actions are observed on the actual native CLI.
- [ ] Canonical receive/accept/report and first-attribution/replay joins work without guessed channels or a second ledger.
- [ ] Real native readiness/queue/held/refusal/stop/restart/resume and supported platform service gates pass.
- [ ] Telemetry OFF/ON support is separately proven; owner P2 and summary/harvest boundaries remain intact.
- [ ] Fresh full/floor/harness/CI/source-mode proof and independent Codex reviews converge at the released head.
- [ ] Public promotion uses logical evidence labels, sanitized conclusions and fresh exact-byte document receipts; private receipts/raw files are not published.
- [ ] The complete mixed-fleet week and actual summary/harvest flow are observed before #2149/#404 closure — **currently blocked and unperformed**.

## Policy/auth decisions versus operational inputs

Routine choices above are selected under delegated source authority: scoped worker, no native channels/MCP, fail-closed policy validation, isolated home and existing canonical doors. No additional source-design permission is requested.

Genuine owner decisions remain only where authority would change: production authentication provisioning/reuse, any weaker/different isolation policy if native enforcement fails, automation that bypasses native hook trust, additional outbound human channels, or a change to summary/provider spend policy. None is silently selected here. The safe first route requires no such weakening or trust bypass; missing production credentials simply blocks enrollment.

Operational inputs for the final gate are the specifically authorized live host/OS, independent data root, fleet/bot labels/unit prefix, coordinator canary channel, exact release, provisioned account availability and dated observation schedule. Choosing these values is not owner code review. Existing test-private auth authority is sufficient for the bounded private Codex prerequisite cells; it does not authorize copying credentials into production or activating a live fleet.

## Risks

| Risk | Mitigation / consequence |
|---|---|
| Default Claude settings leak into a nominal Codex worker | Origin-aware effective-config refusals and separate render/launch branches. |
| Managed native profile does not exist or is ignored interactively | Mandatory native positive/negative settings proof before carrier implementation; revise that fork if unavailable. |
| A policy label passes while sibling/auth paths remain accessible | Native dummy-file controls; keep the bot unavailable on unsupported policy, no broad bypass. |
| Catalog/desktop proof is mistaken for native action/delegation | Pin execution host and require actual read/action/spawn/wait receipts; preserve unsupported route labels. |
| No-channel status hides a failed native boot | Session/input readiness independent of poller absence; auth/trust/setup errors do not become ready. |
| Repeated sends/restarts duplicate work or switch attribution | Receiver proof, retained UUIDs, Half B replay semantics and exact owned session identity. |
| Native trust/cache changes collide with sealed configuration | Separate native mutable state and individually managed artifacts; actual install/rollback readback. |
| Short source/protocol proof is used to close the epic | Separate full service, 48-hour dependency, P2 and mixed-week gates with actual UTC evidence. |

## Companion plans

- #2145 canonical epic §5/§5.1, P1 Half B #2360 and its session-join observation runbook.
- Lobby P4 `documentation/plans/2026-10-16-runtime-neutral-p4-lobby-hook-conformance.md`.
- Claudron v0.10.0 session-loop owner contract and capability floor; no duplicated knowledge logic.
- clauDNA P4 store adapter and #404 behavior/marketplace plan; runtime CLI and desktop capability claims remain separate.
- P2 local OpenTelemetry pipeline and Codex mapping, with its own floor-host elapsed gate.

## What NOT to do

Do not merge/implement around Half B's blockers, change the retired runtime key into an alias, launch Claude for a Codex declaration, pass Claude flags/models/permissions to Codex, strip unsupported restrictions, invent native channels/agents/MCP/config fields, copy production auth under test authority, trust hashes globally, fork the rollout reader/intake/task ledger, infer confinement from enums, or call static/desktop/fake-provider/short-canary results a real mixed-fleet week. Do not mutate the parent's ledger, owner release worktrees or #404 proofs while preparing this private plan.

## Context

Area: composition, policy and supervised execution. Overall effort: L across bounded source slices plus external elapsed gates. The current planning audit supplies no new runtime evidence. Source readiness, release publication, independent activation and complete live acceptance remain separate states.

## Native profile prerequisite readback (reviewed 2026-10-19)

The actual native CLI was 0.162.0-alpha.17.2. The strict negative arm exited 1 and explicitly rejected its unknown profile field. The anonymous retry exited 0 only after Ctrl-C; the authenticated positive arm exited 1 at workspace-routing discovery, with no prompt submitted. Their owned process groups were absent after wait. The first anonymous observer had no native exit receipt; a separate exact-PID check reported that process absent. Its sandbox/ps failure cause is reported by the operator, not independently reproduced from the retained archive.

The authorized private auth copy was deleted; controller exit 0 is separate from native positive exit 1. Only original auth/config/binary **before** metadata was retained; a final original-input/binary readback was unperformed, so this cell supports no global-unchanged or binary-after claim. Independent review verified all 52 retained evidence/config/schema/PTY members and reproduced the original profile digest by removing exactly the added TUI preference. The tested immutable-carrier assumption fails; positive effective settings, native confinement and a replacement carrier remain unproven. This preparation does not start the source phase around Half B or satisfy live acceptance.
