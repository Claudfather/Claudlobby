# The Observable Plane — design (working draft)

**Status:** SUPERSEDED PENDING v2 (2026-08-18) — an external implementation-grade review (reconciled in-session, all factual claims verified) reopened §3–§12 and §14. Verified corrections: this draft was written against a checkout 56 commits behind origin/main (`claudlobby brief` #904 landed; dispatch `--type` gating landed; supersession landed; `vaults/` already gitignored); §12's LangSmith/OTel claims are stale (official LangSmith Claude Code plugin exists; OTel now carries agent/workflow ids + beta distributed tracing, with interactive sessions ignoring inbound TRACEPARENT — the surviving, narrower build case); the comms `outcome`-on-immutable-row contract is not crash-correct and is replaced by intent + transport-attempt events + acknowledgement. Do not implement from this document. v2 lands after the reopened forks are ruled.

Original status: WORKING DRAFT — sections marked **RATIFIED** / **PROPOSED** / **OPEN**. Uncommitted; lands on a branch when the walk completes.
**Session:** `8ad2aa7e-bade-4c55-b3c3-8af5869b7693` ("OBSERVABLE PLANE DESIGN"), 2026-08-08 → present.
**Companion research:** the 2026-08-08 landscape memo (orca deep-dive, 25-product survey, web-terminal feasibility) — published artifact "The Observable Plane"; verdicts summarized in §1.
**Lineage:** #974 (mission epic), #264 (observability compound play), #886 (central logging epic), #904 (bot-facing read door — the plane is its human-facing sibling), 2026-07-30 system review (write-rich/read-starved diagnosis, PR #928 system map).

---

## 1. Context — RATIFIED

Claudlobby runs supervised fleets of Claude Code bots (per-bot tmux servers, systemd/launchd, manager→worker dispatch over `pane_send_verified`, worker report-back, Telegram as the only human surface). The operator's problem: **the fleet is write-rich and read-starved** — rich inter-bot communication exists but is visible only as a thin Telegram emission layer.

2026-08-08 research verdicts (evidence in the companion memo): the "parallel worktree ADE" category is taken (stablyai/orca, ~40k★, has a real coordinator/worker orchestration layer and is converging on headless VPS operation); **nobody ships** live-terminals × visible inter-agent comms × supervised long-running × remote-anywhere in one product; span tracers (LangSmith/Langfuse/AgentOps) cannot see processes, panes, or supervisor state; the ventures nearest "standing fleet with an observability surface" died or pivoted in the last year. The unoccupied cell is exactly claudlobby's ground: **supervised agent fleets on hardware you own, observed from anywhere.**

**The program in one line:** redo the data model underneath the framework, then use it to power the observability plane.

**Success criteria (v1):** the operator stops opening Telegram to *see* the fleet (Telegram remains input/alert); the plane shows at least the union of `status`/`report-back`/`events`/`workstreams` CLI output, live; the gaps counter has filed a concrete read/write-defect list.

## 2. Program shape — RATIFIED

- Four sub-projects: **read plane → library navigator → management verbs → productization/security.** Whole product designed conversationally now; read plane built first; security layer designed after the plane runs tailnet-only.
- **Placement:** inside claudlobby (`plane/` subsystem, `claudlobby plane` subcommand via `commands/`). Extraction stays cheap via contract-first.
- **Stack:** FastAPI daemon importing existing readers (`config.py`, `status.py`, `uptime.py`, `workstreams.py`, `paths.py`) + React/TS/Vite frontend compiled to static assets served by the daemon (no Node runtime on the host). Greenfield answer was TS end-to-end; reconciled on Python because the frontend is identical either way and the daemon inherits ~2,500 lines of battle-tested overlay/nesting resolution while schemas are soft. Every daemon↔UI payload is a versioned Pydantic contract with JSON-Schema export → generated TS types (drift impossible silently). No ORM — DDL in versioned `.sql` migrations (`user_version`); daemon queries via stdlib `sqlite3`, parameterized.
- **Hosting:** Pi first, tailnet-only via Tailscale Serve; VPS-shaped throughout (headless auth documented; ~€16/mo 16GB class runs 20–25 bots; the real scaling constraint is Anthropic weekly rate caps, not RAM).
- **Delivery:** website/PWA, decisively — the fleet is remote so a native app has nothing local to integrate with; away-channel push stays Telegram.

## 3. v1 scope — RATIFIED

**v1.0 (all read):** fleet home + org chart · thumbnail grid (ANSI `capture-pane -e -p` polling ~1–2s; NOT 20 live xterm.js instances — main-thread ceiling) · single-pane live view (ttyd, `tmux attach -r`, read-only by construction) · the comms channel (§8–9) · health strip (supervision state incl. previously invisible `pane_stuck`/`wip_uncommitted`) · workstreams panel · bot equipment view · library browser (read-only) · open-dispatches board (open/overdue/orphaned — the view the system map names as missing) · gaps counter (emissions-parity: what reached Telegram that ledgers couldn't render — every gap is a write-side defect to file) · dormant-emitters panel (no-silent-switches as UI) · utilization strip (consumes the self-described write-only `fleet-utilization.json` + enrolls its timer).

**v1.1 (first write):** the equip verb — attach/detach *known* library items only (validated against `known_values`), flowing **through git** (edit → vault commit → `generate` → snapshot), never around it. Emits `kind: config_change`.

**Later cycles:** emissions diet (Telegram brevity with detail-on-demand, measured via the ab-comms-eval/recoverability machinery); security/auth/multi-host; productization.

**Explicitly out of v1:** any other write, login/auth (tailnet is the auth), public exposure, push notifications, Telegram changes, multi-host federation, raw transcript viewing, token/cost dashboards.

**Standing guardrails:** the plane never *mutates declarations* (observation appends go through the door family like every emitter); fleet-specific values live in `local/`/env at runtime — never in committed code; UI shows aliases (chat IDs etc.) with deliberate reveal; exports are alias-only.

## 4. Terminology rulings — RATIFIED

| Term | Meaning | Consequence |
|---|---|---|
| **Host** | The machine. THE container around fleets. | Containment = **Host → Fleet → Bot**. API shape `/api/hosts/<id>/fleets/<f>/bots/<b>`. `host_id`: `CLAUDLOBBY_HOST_ID` → Tailscale MagicDNS → hostname. |
| **System** | The claudlobby install — the *software facet of the Host card* (versions, system.yaml defaults tier, host jobs, armed emitters). | The word never again means a container. The container-sense of "system" (`local/<system>/<fleet>/`, `migrate-fleet-to-system.sh`, ~15 files) is renamed in a vocabulary-only PR (mechanism is marker-agnostic; L4 rename-map machinery applies). |
| **Fleet** | A team of bots. **Teams collapse: fleet = team** (verified: all live multi-bot fleets declare exactly one team). | `fleet.manager:` scalar replaces `teams:`; `reports_to`/`manages` become the only topology surface; composed roster derives by edge inversion; one-release deprecation shim. #974's "specialist agent teams" gets an exact referent. |
| **Bot** | One supervised Claude Code identity. Actor id `bot:<fleet>/<name>` everywhere (fleet names unique estate-wide). | |
| **Vault** | A Claudron-made repo, *mounted*: exactly one **primary** (mounted AT `local/`, carries fleets) + N **mounted** secondaries (`vaults/<name>/`, gitignored — entry missing today, in scope). | `local/` IS the operator's vault clone; the vault contract gains a **`fleets/`** namespace (migration: one `git mv home fleets` + unit repointing). Identical path shape with or without a vault. Claudron-side confirmation issue: `fleets/` namespace tolerance + multi-vault capability. Federation seam = same vault across hosts. |

**Config tiers:** Tier 0 templates (`*.example`/`*.seed` — OSS git = *schema* history) · Tier 1 declarations (vault git = *intent* history, with a graduation ladder: root-mode works-but-flagged → `claudlobby vault init` (git, no remote) → vault with remote) · Tier 2 secrets (`.env` — no git ever; declarations reference `${VAR}` names only). Follow-on design item: `system.yaml` → vault `hosts/<host_id>/` (host-keyed, estate-declarative).

## 5. Storage architecture — RATIFIED

**SQLite, WAL, one db per host, no server.** Lives outside the vault working tree (bodies never inside a syncing repo). Nightly `.backup`. FTS5 over the channel. Live follow via rowid polling. Rationale: the write load is trivial but the *correctness discipline* (flock, escaping, rotation, bounded reads) is currently distributed across every bash writer — the verified defect classes (#911 escaping, #905 unbounded reads, rotation divergence) move into the substrate. Postgres rejected: a server process buys throughput and notify we don't need.

**The two-lane rule:** *intend in git, observe in SQLite, snapshot the boundary with provenance both ways.* Files = SSOT of intent. Rows = SSOT of what happened (including "a declaration was observed"). Rows never flow back into files; the wizard/equip verb edits files and commits. Litmus for any new field: "if two copies disagreed, which one is wrong?"

**The write spine (one INSERT path):** bash doors → `claudlobby emit` → Pydantic validation → parameterized INSERT; on infrastructure failure, spool to `spool/*.json` for daemon re-ingest (a broken validator must never lose a message; a *contract* violation fails loudly at the caller instead). Three invariants: **no UPDATE path in the observed lane; no derived table is a source of truth; exactly one INSERT path.**

**Transition:** doors dual-write JSONL + db during canary; watchdogs migrate to `sqlite3` queries; JSONL writers retire on evidence.

## 6. `registry_snapshots` — RATIFIED

Append-only **keyframes** of resolved entity state (full canonical payloads, NOT diffs): git-style snapshot-store-derive-diffs-at-read. Hash-gated (no change → no row; estate writes tens of rows/week; year ≈ single-digit MB). Canonical serialization (sorted keys, Pydantic-controlled field order) is **mandatory** — the hash gate and minimal diffs depend on it.

```sql
CREATE TABLE registry_snapshots (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, host_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,      -- host | vault | fleet | bot  (equipment folded into bot)
  entity_id TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
  cause TEXT,                     -- generate | probe | equip | migration
  vault_rev TEXT, schema_version TEXT NOT NULL
);
-- SCD2 = a VIEW: valid_from = ts, valid_to = LEAD(ts) OVER (PARTITION BY entity_id ORDER BY ts)
```

Humans read the **diff view** (`registry_changes`: field-level deltas between consecutive snapshots), never the blob; hot fields promote via generated columns; typed SCD2 dimension tables are derivable later as Lane-C marts (raw→staging→marts, deferred to where it is reversible). What it uniquely answers: composition changed with **no vault commit** (package-owned `system.yaml` defaults moved under a claudlobby update — changed payload hash, unchanged `vault_rev`); incident joins ("what changed 13:00–14:00" across declared+probed on one time axis); estate history (roster moves, fleet births); equipment×behavior correlation; hand-edit drift via `composed_hashes` anchors.

## 7. Model catalog — RATIFIED

**Lane A — declared (files, git):** fleet.yaml (per fleet) · system.yaml (per host) · .env (per scope) · templates (per software version). Updated by hand/wizard/equip; each is its own SSOT.

**Lane B — observed (append-only tables; all rows carry ts/host_id/emitter/schema_version; ids minted never inferred):**

| Table | Grain | Written by |
|---|---|---|
| registry_snapshots | entity × observed change | generate / probe / equip via emit |
| communications | transmitted message (`msg_id`) | the `comms-send` door (every carrier) |
| task_contracts | dispatched task (`task_id`) — **no status column, ever** | dispatch path of the door |
| task_events | report against a task (progress/blocked/done) | report-back door |
| lifecycle_events | machinery event (restart/heal/stuck/teardown…) | keepalive, pulse, spin-down, start-bot |
| session_digests | ended session | transcript-digest hook (armed per fleet) |
| session_usage / utilization_windows | session / bot × window | transcript-usage, utilization job |
| spool | failed emit (transient files) | any door; daemon re-ingests |

**Lane C — derived (rebuildable; never authoritative):** `reg_hosts/vaults/fleets/bots` current-state projections (hash-verified vs files; delete anytime) · SCD2 `*_history` views · `task_status` view (open/overdue/closed/orphaned + misroute flags = joins) · presence (in-memory only: supervision, last-pane-change, busy) · meta counters (malformed rows, `raw` count, spool depth, reconcile drift — the trust layer).

## 8. Comms architecture — RATIFIED

**"Communication is the base class; task is a decorator."** One universal door — `comms-send` — is the ONLY serializer of communication rows: transport + formatting contract + write-time validation (misroute detection at the door: e.g. `kind: report_back` addressed to a Telegram alias while the joined dispatch sits unclosed — the observed failure class where a worker "reported back" to a human channel and the dispatching manager never saw it). Task machinery (mint `task_id`, contract row, deadline, vault enrichment, envelope) is an optional module the door invokes for task kinds. Two ledgers on purpose — transmissions (append-only forever) vs contracts (open/close lifecycle) — one pathway mints both.

**Migration without touching the spinal cord:** `dispatch-task.sh`/`dispatch.sh`/`report-back.sh`/`tg-post.sh` become thin shims over the door (identical interfaces, canary-able per bot). `pane_send_verified` (#763's single choke point) becomes the **enforcement net**: any injection reaching the primitive without door provenance is ledgered as `kind: raw`; the raw-counter trending to zero is the evidence that retires the shims; physical script merges are evidence-gated.

**Row schema:** `ts, host, fleet, msg_id, from, to, to_chat_id?, carrier (tmux-inject | telegram), kind, task_id?, workstream?, reply_to?, carrier_ref?, body, truncated, body_bytes, body_sha256?, refs[], outcome (delivered|rejected|failed), emitter, schema_version`. Recipient ≠ carrier (the distinction that makes misroutes expressible). Telegram recipients stored as alias **and** raw chat id; alias-first rendering, deliberate reveal, alias-only exports. `outcome` makes swallowed delivery failures visible (dead bridge = red rows, not silence). **The bridge is the second Telegram carrier** — outbound (MCP reply path) and inbound (operator messages) both emit in v1.0, so the operator appears in their own stream. Backfill from existing dispatch/report JSONL history at first boot.

## 9. Communication open items — PROPOSED (recommendations on the table)

1. **Kind vocabulary (closed enum, validation failure ≠ coercion):** `dispatch, report_back, question, answer, alert, notice, briefing, nudge, ack, chat, config_change, raw` — 1:1 with message classes the fleet already sends (alert/notice mirrors FLEET ALERT/NOTICE). `decision` reserved for the emissions-diet cycle, not in v1.
2. **Threading:** `task_id` = thread spine; `reply_to: msg_id?` for question→answer and operator chat; `carrier_ref` = carrier-native id (Telegram `message_id`) enabling mechanical reply-mapping and dedupe. Channel renders task threads first-class, reply chains within, flat timeline otherwise.
3. **Body bounds:** 16 KB cap, UTF-8, ANSI stripped at the door; over-limit → truncate + `truncated=true` + `body_bytes` + `body_sha256` (full-content hash). No overflow blob store in v1 (transcripts/panes remain archival) — deliberate YAGNI, revisit on gaps-counter evidence. No retention policy until the first gigabyte (then DELETE policy, not rotation scripts).

## 10. Registry payloads — RATIFIED (compact)

- **Host:** probed hardware (RAM/disk/load/boot/tailscale/health_flags incl. under-voltage/thermal/SD-stall) + `system` facet (claudlobby/claude/node versions, host jobs, armed emitters, defaults tier) + **declared** fleet placement (manifests, never process inference).
- **Vault:** `vault_id, role (primary|mounted), mount_path, remote (aliased), freshness {behind, ahead, last_fetch}` (the notify-behind two-distances distinction rendered), `compat {floor, ok}`, `carries_fleets`, `gitignore_safe` (doctor rung: runtime/ledger/.env patterns covered — a vault missing them would sync message history to a remote).
- **Fleet:** `mission` verbatim (all live fleets declare one) + `mission_file {path, content_hash}` (charter drift snapshots via daily probe) + `manager` + `org_edges` + `roster` + `defaults_summary` (scalars + per-tier hashes, bounded) + `env_keys` (names only) + `jobs` + `vault_binding` + `declared_hash`/`vault_rev`. Ratified extras: **goal-blind nudge** (missionless fleet flagged, never enforced) and the **declared-vs-observed org overlay in v1.0** (declared edges from config; observed edges from dispatch volume in task_contracts; divergence is the insight).
- **Bot:** `bot_id` (the actor join key) + identity (account, service/socket, model, effort) + `org` + `equipment` (verbatim short lists; each id links into the library browser) + `posture` (permissions_mode — `dangerously_skip` badged loud — tool allow/deny, sandbox, grants hash, hooks inventory, env names, RC state, telegram alias, git profile name) + `schedule` + `vault_binding` + `composed_hashes` (CLAUDE.md/bot.conf/.mcp.json/settings — the drift anchors).

## 11. Robustness commitments — RATIFIED

Bounded reads everywhere (tail-N + follow; the 235MB naive-read lesson) · rotation-aware tailing during the JSONL transition · tolerant parsing with malformed-row counters feeding the gaps surface · startup self-audit (ledgers exist where `config.py` resolution says; nested/flat split-brain = loud banner, not empty panels) · provenance badges on every metric (known-bad numbers ship badged "under repair (#891)" or not at all — the plane never launders) · clock-skew tiebreak (RTC-less hosts: ts + per-file line order) · the plane is supervised (`Restart=always`) but emits nothing into fleet alert paths — its death is a browser that won't load, never a fleet event · thumbnails not live terminals (xterm.js main-thread ceiling; one live pane at a time via ttyd) · scrollback via static `capture-pane -S` render.

## 12. Build-vs-adopt: the data model vs existing stacks — OPEN, framed for external review

**The question:** is the bespoke data model (§5–10) worth building, versus adopting an existing LLM-observability or agent-framework stack?

**Candidates and what each would actually cover:**

- **LangChain (framework):** an application framework for *building* LLM apps that call model APIs. It does not wrap, observe, or instrument Claude Code CLI sessions — adopting it means **rewriting the agents as LangChain programs**, replacing the runtime rather than observing it: loss of the Claude Code harness (skills, MCP wiring, permission modes, subscription auth/Max-plan economics — the fleet's cost basis), in exchange for LangSmith-ecosystem tracing. That is a different product, not an observability layer for this one.
- **LangSmith / Langfuse / AgentOps (span tracers):** verified in the 2026-08-08 survey — they trace API calls/spans and cannot see tmux panes, processes, supervisor restarts, or keystroke-injected dispatches. They observe *inside* a model call; this design's subject is *between* the agents and *around* the processes.
- **OpenTelemetry (the serious adopt candidate — verified against current docs 2026-08-17):** Claude Code ships **native OTel export** on every auth path including subscription (`CLAUDE_CODE_ENABLE_TELEMETRY=1` + `OTEL_METRICS_EXPORTER`/`OTEL_LOGS_EXPORTER`): metrics incl. `claude_code.token.usage`, `claude_code.cost.usage`, `claude_code.session.count`, `claude_code.active_time.total`, `claude_code.commit.count`; events incl. `user_prompt`, `assistant_response`, `tool_result`, `api_error`; content redacted by default but exportable via `OTEL_LOG_USER_PROMPTS=1` / `OTEL_LOG_ASSISTANT_RESPONSES=1` / `OTEL_LOG_TOOL_DETAILS=1` (tool content truncated at 60 KB; thinking always redacted); distributed tracing is beta (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`). Docs: code.claude.com/docs/en/monitoring-usage. An OTel collector + Grafana delivers per-bot usage/cost dashboards with near-zero build — and with content flags on, even the *text* of a dispatch would appear (as an ordinary `user_prompt` event in the receiving session). **The verified limitation, which is the load-bearing fact:** export is strictly per-session, correlated only within a session (`prompt.id`/`message.uuid`) — **no inter-session semantics exist**. A dispatch is indistinguishable from any other prompt: no from/to, no kind, no task identity, no contract/closure join, no org topology, no misroute detection. OTel sees inside each session; this design's subject is *between* them.
- **Likely synthesis (recommendation to stress-test):** not either/or — OTel can *feed the Usage lane* (concept #8 could consume Claude Code's native export instead of parsing transcripts — a real simplification candidate inside this design), while the comms/task/registry semantics have no off-the-shelf substitute. The honest cost of bespoke: schema stewardship and zero ecosystem — mitigated by Pydantic-versioned contracts and the one-door write spine, but not eliminated.
- **Conditions under which adopt-wins:** if the fleet were being rewritten as API-based agents anyway; if per-model-call tracing were the actual need; if hosted UIs were acceptable for private fleet data.

**Reviewer asks:** (a) challenge the build-vs-adopt reasoning above, especially the OTel synthesis; (b) challenge any Lane-B schema against "will this be regretted at migration N+1"; (c) is the two-lane (git-intent / SQLite-observation) split the right long-term substrate, or is there a simpler standard we're missing?

## 13. Remaining walk — OPEN

Communication ratification (§9) → task contracts/events confirmation → lifecycle event vocabulary (delegates to #903) → digests/usage confirmation → Lane-C confirmation → **channel UX design** (the Twitch-like surface) → security-layer sketch (login, exposure beyond tailnet, per-viewer authz) → error handling & testing/validation approach (validate-bot-change extension, canary plan, cold-start impact) → spec finalization on a branch → writing-plans for the read-plane implementation.

## 14. Pre-plane PR series (settled, sequenced ahead of plane code) — RATIFIED

1. Vocabulary/layout PR: "system"-container rename · `home/` → `fleets/` (`git mv` + unit repointing) · `vaults/` gitignore entry · normative vocabulary table into CLAUDE.md.
2. Teams-collapse PR: `fleet.manager:` + deprecation shim + composed-output byte-parity check.
3. The substrate series (headless, before any UI): emit spine + db + doors-as-shims + snapshot emitter + bridge emitters — canaried per bot, accumulating history while the UI is built.
