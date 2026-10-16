---
title: "Run log — runtime-neutral observability (#2145): canaries and build-all delivery"
type: run-log
status: build-all in progress; orders 2–6 previously merged and activated; remaining phases and evidence gates tracked in the 2026-10-15 ledger
created: 2026-10-05
epic: 2026-10-04-runtime-neutral-observability-plan.md
issue: Claudfather/Claudlobby#2145
pr: Claudfather/Claudlobby#2144
---

# Runtime-neutral observability run log

Epic §6 P0 and §10 order 1. Instrument: `harness/runtime-neutral-canary.py` (stdlib; `receiver` is a raw
OTLP/HTTP-JSON receiver on `127.0.0.1` that answers 200 first, the shape the `plane-otel` intake takes; `hook`
logs hook payloads; `c10` probes tmux env inheritance; `report` prints the verdicts). It records metric names,
temporality, event names, attribute *keys*, and the values of a short allowlist of id/enum keys. Every other
value is kept as its type and length. Prompts, tool inputs and responses never reach a log
(`tests/test_runtime_neutral_canary.py` pins that).

The historical P0 measurements follow. Current implementation, review and integration state is in the [2026-10-15 sprint ledger](#2026-10-15--build-all-sprint-ledger).

## Summary

| Canary | Answer so far | Decides | Still owed |
|---|---|---|---|
| **C6a** confirmation leg | **Passes off the floor host.** Claude Code 2.1.289 exports OTLP/HTTP-JSON logs and metrics straight to a local endpoint under the plan's env block; the resource attributes land verbatim on every row; temporality is delta by default; `session.id` = hook `session_id` across `--resume`. | F4 stays locked on this evidence; **F4's gate is the floor host**, so P2-a1 still waits on the operator leg. | **Answered (legs 2–3):** PASS on the Pi; RC ×2, exact overlap (63/63), ≈ 3.3 MB per busy hour, receiver ≈ 21 MB — on a borrowed worker over 21 min, not 24 h. Rare day-long events move to C6b. |
| **C6a** side legs | Log events emitted while the endpoint is down are **dropped, not retried** (35 s outage). Summarizer `claude -p` children **export full sessions** under their own random `session.id`, carrying the bot's resource attributes. Every event carries `user.email`, `user.account_uuid`, `user.id`, `organization.id`. Prompt/response attributes are the literal `<REDACTED>` at default settings. The prefixed event name is the log **body** (`claude_code.api_request`); the `event.name` attribute is bare (`api_request`). | Plan 4 (`records()` reads the body; the 6/8 line counts; the restart risk); §14 Q15, Q16 for the operator. | — |
| **C10** | **Leaks — all of it.** A tmux server started from a Claude Bash tool hands the pane `CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDECODE=1` *and the caller's* `CLAUDE_CODE_SESSION_ID`. But a `claude` started with those leaked markers **replaces the id with its own and sets the marker itself** on every child (headless). | Plan 2 Task 7b as written is a no-op: unsetting the marker changes nothing a hook or tool sees. Task 7b is re-scoped to the open interactive question below. | **Answered (operator leg 1):** it boots, but the leaked marker **turns transcript saving off** — Task 7b ships (unset the three markers before exec). |
| **C11** | **Yes, from both (headless).** Main-thread Bash, an Agent subagent's Bash and every hook (including the subagent's `PostToolUse`) carry the SessionStart `session_id`. **Every one of them also carries `CLAUDE_CODE_CHILD_SESSION=1`**, the main thread included. The hook payload's `agent_id`/`agent_type` is what tells a subagent's hook from the parent's. | `SUBAGENT_SHELL_SHARES_SESSION_ID = True` (plan 2 Task 7), and the marker branch is dropped: it discriminates nothing. **Plan 5 Task 5 is struck**: a guard that records nothing under the marker would record nothing for every session. | **Answered (operator leg 1, interactive tmux):** same readings — closed; Half B's gate is met. |

No fork reopens. F4's confirmation is provisional until the floor-host leg; the other findings are plan facts.

## Environment

**Measured:** a cloud container (x86_64 Linux, 4 vCPU, 16 GB), Claude Code 2.1.289, tmux 3.4, Python 3.11. This
is **not** the floor host and **not** a tmux-hosted bot. Every `claude` ran headless (`claude -p`) under
`env -i` with only `HOME`, `PATH`, `TERM`, the proxy and CA variables and the plan's telemetry block. Without
`env -i`, the container's own session leaked into the nested `claude`: it reported the container's session id
(this is why the first `c11` arm was discarded). An interactive arm was attempted in tmux and stopped at first-run
login, and driving it past that by accepting an API-key prompt for a nested agent was refused in this
environment. The interactive legs are the operator's (below).

### 2026-10-05 19:04 UTC — C10: the tmux leak

**Measured:** `runtime-neutral-canary.py c10`, run from a Claude Code Bash tool (the shape of a manager bot or an
operator session running `start-bot.sh` by hand). It starts a throwaway server with `tmux -L <socket> new-session -d`, as
`bot_tmux` does (`lib-common.sh:2078-2089`; `start-bot.sh:325`). Bare arm: the pane saw `CLAUDE_CODE_CHILD_SESSION=1`,
`CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT=remote` and the caller's `CLAUDE_CODE_SESSION_ID`. Scrubbed arm (`unset
CLAUDE_CODE_CHILD_SESSION` before the command, Task 7b's shape): the marker was gone and the other three still
arrived. A baseline from a shell without the markers passed nothing.

**Read from code:** nothing on the launch path scrubs env (`grep` for `CLAUDE_CODE_CHILD_SESSION`, `CLAUDECODE` and
`env -i` in `claudlobby/` finds only an unrelated docstring). Supervised starts don't inherit a caller's env: the
unit's `ExecStart` / launchd plist runs `start-bot.sh` under the service manager's environment
(`supervision.py:95-123`). The paths that do inherit are a direct `start-bot.sh` (`spin-up-bot.sh:52-54`'s
unsupported-host fallback, the harnesses, an operator or agent shell). `keepalive.sh:228` runs under its timer's
environment.

### 2026-10-05 19:04–19:06 UTC — C11 and C10's consequence (headless)

**Measured (clean arm):** one `claude -p` session with hooks on SessionStart, PostToolUse, PostToolUseFailure,
SubagentStart, SubagentStop and SessionEnd. The main thread ran `env | grep ^CLAUDE_CODE_ | grep -E
'SESSION_ID|CHILD'` into a file, then an Agent (general-purpose) subagent ran the same command into a second file. Both
files held `CLAUDE_CODE_SESSION_ID=<the SessionStart session_id>` **and `CLAUDE_CODE_CHILD_SESSION=1`**. Each of the
seven hook processes, the main thread's and the subagent's `PostToolUse` alike, carried the same `session_id` in its
payload, the same `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION=1` and `CLAUDECODE=1`. Only the payload told
them apart: the subagent's `SubagentStart`, `PostToolUse` and `SubagentStop` carried `agent_id` and
`agent_type: general-purpose`, and the parent's carried neither.

**Measured (leaked arm, C10's consequence):** the same session started with the three markers C10 found leaking
(`CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDECODE=1`, `CLAUDE_CODE_SESSION_ID=<a fixed foreign uuid>`). It got a fresh session id. Every
hook payload, both Bash files and every hook's `CLAUDE_CODE_SESSION_ID` named that fresh id, and the foreign id
appeared nowhere. The marker was `1` everywhere, as in the clean arm.

**Consequence:** the marker means "a process Claude Code spawned", not "a nested session" or "a subagent". A reader
cannot tell a top-level session's hooks from a nested one's by it, and a leaked marker is indistinguishable from
the one Claude Code sets anyway. Scrubbing it (Task 7b as written) changes nothing downstream.

### 2026-10-05 19:08–19:15 UTC — C6a: direct export to a local receiver

**Measured, confirmation leg:** the env block of epic §6 P2 (`CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`OTEL_{METRICS,LOGS}_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:<port>`, and `OTEL_RESOURCE_ATTRIBUTES` exactly as the spec
composes it, `claudlobby.fleet=canary,claudlobby.bot=bot:canary/c6a,claudlobby.content=metadata,agent.runtime=claude`).
Every request was `application/json`, unencoded, to `/v1/logs` or `/v1/metrics`, and nothing went to `/v1/traces`.
The resource carried the four keys verbatim (the `:` and `/` in the bot value survive) plus `service.name=claude-code`,
`service.version`, `os.type`, `os.version` and `host.arch`. Metrics: `claude_code.session.count`, `token.usage`,
`cost.usage` and `active_time.total`, with temporality **delta** and no temporality variable set. Datapoints carry
`session.id`. Events (prefixed log body / bare `event.name` attribute): `user_prompt`, `api_request`,
`assistant_response`, `tool_decision`, `tool_result`, `hook_registered`, `hook_execution_start`,
`hook_execution_complete`, `plugin_loaded`, `managed_settings_resolved` and `subagent_completed`. The log export lag
was 5.2–5.4 s at most (the 5 s interval), and metrics arrived at the 60 s interval or at exit.

**Measured, `session.id` = hook `session_id`:** session A (`startup`) and then `--resume A` gave one id in both
SessionStart payloads (`source` `startup` then `resume`), in the Bash env and in every exported datapoint and event.
No exported id lacked a hook.

**Measured, content at default settings:** `user_prompt.prompt` and `assistant_response.response` are the literal
`<REDACTED>`. `tool_result` carries `tool_use_id`, `tool_name`, `success`, `duration_ms` and the input and result
sizes, with no input. **Every event carries `user.email`, `user.account_uuid`, `user.account_id`, `user.id` and
`organization.id`** (plus `terminal.type`, `prompt.id`, `event.sequence`). Plan 4's `CONTENT_KEYS` don't name them,
so the raw files would hold the account email on every line (0700, local; §14 Q16).

**Measured, outage:** the receiver was down for the first 35 s of a 79 s session (one 75 s foreground tool call).
None of the log events emitted in that window (`user_prompt`, the first `api_request`, `tool_decision`, the hook
events) ever arrived; the earliest event received occurred at +78 s. The metrics survived: the first 60 s export
fired after the receiver was up and carried `session.count` and the first tokens. A session that **ended** during
the outage (17 s, two runs) delivered nothing at all. So, for logs, the exporter's buffer covers less than 35 s.
An intake restart drops the log events of its window, and a session ending inside it loses its tail.

**Measured, summarizer children:** clauDNA's exact `run_claude` shape (`summarize.py:73-84`: `-p --session-id
<uuid4> --no-session-persistence --setting-sources "" --tools "" --strict-mcp-config --model haiku`, with
`CLAUDNA_SESSION_CHILD=1`) run under the bot's inherited telemetry env. It exported a full session under its own
`--session-id` (metrics `session.count`, `token.usage`, `cost.usage`, `active_time.total`; events `user_prompt`,
`api_request` with `model=claude-haiku-4-5-20251001`, `tool_decision`, `tool_result`) with the **bot's** resource
attributes. No hook ever saw that id. The epic's phrase "no exporter env of their own" is right, but under `set -a`
the bot's exporter env *is* theirs: every summary appears at the intake as an extra session of the bot.

**Measured, volume (one short session, not a bot-hour):** 30 requests, 575 KB in total (37 KB at most); a
three-turn session sends about 5 log requests and 2 metric requests. The per-bot-hour figure needs a real bot
(operator leg).

**Read from code:** plan 4's mapping keys on `claude_code.tool_result`, `claude_code.api_request` and
`claude_code.api_error` (`…-p2-otel-pipeline.md:409-411`), and `records()` names an `event_name` without its
source field (`:415`). Only the log body carries those spellings.

### 2026-10-05 ~20:06 UTC — operator leg 1: interactive C11 and C10 in tmux (macOS, Claude Code 2.1.289)

**Measured (Run A, clean):** an interactive `claude` in a throwaway tmux server (`tmux -L rnc`), hooks from
`runtime-neutral-canary.py setup`. Main-thread Bash, an Agent subagent's Bash and every hook (four subagent
`PostToolUse` among them) carried the SessionStart `session_id`; every one also carried `CLAUDE_CODE_CHILD_SESSION=1`,
`CLAUDECODE=1` and `CLAUDE_CODE_ENTRYPOINT=cli`. The receiver was not running, so this run has no OTel leg (the
headless runs above carry it). The subagent's first write to `~/rnc` was refused by the operator's Claude Code
sandbox (writes outside the working directory); its retry wrote to `/tmp` inside the sandbox — a harness detail, not
a finding.

**Measured (Run B, leaked markers):** the same session started with `CLAUDE_CODE_CHILD_SESSION=1`, `CLAUDECODE=1` and
a foreign `CLAUDE_CODE_SESSION_ID` exported before `claude` — the shape C10 found reaching a bot's pane. It **booted
to the input box** and took a fresh id (`405d…`) that every hook, both Bash files and every exported datapoint and
event named; the foreign id appeared nowhere. OTel exported normally (`application/json`, delta, resource attributes on
every row, log lag 5.0 s). But the footer read: **"⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION
marker · restart with CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 to keep future transcripts"**. A bot started from inside
a Claude session would run without a transcript: no `--resume`, nothing for clauDNA's store or any transcript reader.

**Consequence:** C11 is closed (Half B's gate). C10's leak is not harmless after all — not for ids, but for
persistence — so **Task 7b ships** in Half A: `start-bot.sh` unsets `CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION` and
`CLAUDE_CODE_SESSION_ID` before `exec $CLAUDE`. Removing the cause is preferred to setting
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`, which would mask it.

### 2026-10-05 20:23–21:53 UTC — operator legs 2 and 3: the Pi (F4's gate) and a borrowed production worker

Run by a Claude Code session on the operator's Mac mini over SSH (`pi-canary-runbook`). Full report kept by the
operator (`~/rnc-results.md`). The instrument was copied alone to the Pi, with sha256 checked on both ends: the Pi's
`~/claudlobby` is the live root on `main` and was only read.

**Measured, leg 2 (F4's gate) — PASS on the floor host.** Pi `aarch64`, Debian 12, Claude Code **2.1.281**, Python
3.11.2, 16 GB (5.2 GB available). A headless session (`--setting-sources "" --strict-mcp-config`, so as not to load
the production user-level plugins, Telegram servers and clauDNA summarizer) exported to the raw receiver on
`127.0.0.1:14319`. Result: every request `application/json`; `/v1/logs` 2, `/v1/metrics` 1; resource attributes on
every row; the one exported `session.id` matched the hook's, none without; delta; log lag 5.5 s. Receiver RSS
**21 MB**. **F4 stays locked, confirmed on the floor host.**

**Measured, leg 3 — a borrowed production worker instead of a canary root (operator decision).** No canary root
existed on the Pi (55 production units). The operator chose to arm one idle production worker
(38.5 % busy over 24 h), by appending the six telemetry exports to its own `.env` (backed
up, restored byte-identical afterwards) and restarting it twice through the release's `bot restart`. Armed
**21.3 minutes**, not 24 h: the soak was compressed into a driven exercise.
- **RC: PASS twice** (21:35 and 21:47), DM in, reply delivered, with telemetry on — #533's failure mode did not occur.
- **Overlap, exact, 21:30:14–21:47:56 (17.7 min):** OTel `tool_result` ids **63** = transcript `tool_use` ids **63**
  (main 59, subagent 4), no id on one side only — subagent, MCP and failed calls included; subagent calls export
  under the parent session id. The plane held **248** `tool_call` rows for those 63 calls (double registration,
  below): halved, 63 `PreToolUse` and 61 `PostToolUse` — the 2 failed calls have no plane completion row, while OTel
  records them as `success: "false"`.
- **Volume:** 72 requests, 1.04 MB of OTLP payload over 21.3 min (≈ 3.3 MB per busy hour; ≈ 31 MB/day at a 38.5 %
  busy share, an estimate — idle volume not measured); receiver RSS 20.4–20.9 MB; 620 KB stored (shapes only).
- **Live-bot shape:** MCP tools export as **`tool_name: "mcp_tool"`** at default settings (server and tool unnamed;
  the plane keeps the real name); `success` is the string `"true"`/`"false"`; new names `mcp_server_connection`,
  `skill_activated`, `subagent_completed`, metrics `claude_code.code_edit_tool.decision` and
  `claude_code.lines_of_code.count`; `hook_execution_start`/`_complete` are 136 of the first 250 rows; **max log lag
  12.9 s** on the busy bot (5.5 s headless).
- **Not observed in the compressed window:** watchdog restarts, summarizer child sessions, intake outages, idle
  volume. They move to C6b (P2-a1's canary week).

**Measured, fleet findings outside this epic's code** (filed 2026-10-05: 1 was already #2062 — Pi evidence added; 2 was #2087, fixed by #2089 on 2026-10-02 after the Pi's checkout `8bc588a8`, so the Pi needs a newer release; 3 is new, #2168):
1. **Every one of the Pi's 21 bots composes `bot-vitals.sh` twice** in the same `PreToolUse`/`PostToolUse` group —
   `$CLAUDLOBBY_NATIVE_DIR/bot-vitals.sh` and the legacy `$CLAUDLOBBY_ROOT/lib/bot-vitals.sh` — so the plane
   writes two `tool_call` rows per hook event. The legacy entry will point at a deleted script once the checkout
   pulls #1989.
2. **`bot restart` reports `ok: false`, rc 6, "bot lifecycle effect is unverified" on successful restarts** (2 of 2),
   returning during the unit's `ExecStartPre=/bin/sleep 30` stagger; both stops also left tmux/claude/MCP processes
   in the unit's cgroup ("Found left-over process … unclean termination"), gone within about a minute.
3. **`documentation/system-yaml-schema.md:313-317,332`** still describes the retired `walk_back_uncomposed_host_units`
   path (`lib-common.sh:5404`), which disables and deletes every `claudlobby-*` unit absent from a given dir.

**Read from code (the runbook's Part 2a, not executed):** `config plan`/`config diff`/`host activate` refuse
`--fleet` (`commands/releases.py:22-24`; the fleet is found under `$DATA/local/*/fleet.yaml`). A second data root
on a one-user host still shares `~/.claude` (plugin install/update at boot, `lib-common.sh:4693`), the host `~/.env`
tier, the enrolled host jobs (`claude-update`, `orphan-browser-reaper`, `plane-view` on `:8899`), the user
manager's `daemon-reload`, and the Telegram access file; `config diff` hides unit names (they are in
`plan.json .effects.units[]`). Plan 4 Task 8 carries these.

**Harness fix (`setup --fleet`):** `claudlobby.fleet` was hard-coded to `canary`; a borrowed bot is now labelled with
its real fleet.

### 2026-10-06 — §10 orders 2 (P2-b) and 3 (F14): live validation, merge, and the floor host's activation

Run by Claude Code sessions on the operator's Mac mini over SSH, driven by runbooks from the planning session. Full
reports kept by the operator (`~/rnc-results-2.md`, `~/rnc-results-3.md`).

**Live validation before merge (a borrowed idle production worker, put back byte-identical).**
- **#2172 (per-bot `CLAUDNA_STATE_DIR`), reproduced through the bot's own `.env`.** The new session landed in
  `$BOT_DIR/data/claudna/sessions/<sid>/`, together with `hooks/` and `runs/` (the whole root moves). Nothing new
  reached `~/.claudna`. The bot answered a DM. **Finding:** a clean restart closes the old-root session itself
  (`close_reason: other`), and sealing a live session first labels its clean close `abandoned`. The activation step
  became "after the restarts, seal only stranded sessions" (#2172's CHANGELOG and plan 6 Task 1 Step 2).
- **#2173 (bot-vitals never falls back to the cwd).** The PR's script recorded a row and advanced the marker with
  `BOT_DIR` set. Unset, empty and relative `BOT_DIR` each wrote nothing. The release's own script, run from a scratch
  dir, reproduced both halves of #874. `hook-retired-path` counts **once per hook event** (42 = 21 bots × 2 against
  the Oct 2 manifests). **Finding:** the live double `tool_call` rows came from a stale activation (bots on the Oct 2
  plan), not from the already-cleaned manifests.

Both merged, with #2191 (getting-started: the `[plane-ui]` extra) and clauDNA #405 (`--since 30m`).

**Floor host activation (Pi 5, 21 bots, 4 fleets): `fd1a1e5` → a release from `main` at `d02f057`.**
- **Plan review.** 21 restarts. `CLAUDNA_STATE_DIR` in 21/21 `bot.conf`. The retired `lib/bot-vitals.sh` went from
  42 hook entries to 0. Same 196 units, the plane view kept. No SQL migration. 0 `hook-retired-path`.
- **Activation.** It stopped once at `sessions_quiesced`: one unit's stop hit the 90 s timeout, and the pause refuses a
  `failed` service (#2193). Recovered with `reset-failed` + `--resume`, then `committed` / `active`. The fleet was
  offline about 16 minutes as a whole, about 15 to 35 minutes per bot.
- **Verified live.**
  - One `tool_call` row per hook event (before: pairs).
  - Every bot writes to its own `data/claudna`, and every live old-root session closed itself. The 9 stragglers from
    Oct 2 to 3 were sealed, and the sweep found nothing more.
  - `bot restart` returns rc 0 (#2087). The stop takes 45 ms: `KillMode=process` with `MainPID=0`, so the #2158
    subreaper can't hold a stop.
  - DMs answered, plane view up, host doctor failures unchanged from before.
- **Load.** After `producers_resumed`, the 1-minute load peaked at 48.5 on 4 CPUs. Two fleet-pulse runs timed out, a
  catch-up briefing ran twice, and the plane's staged emits peaked at 218 and drained (#2197).

**Process lessons (now in the runbook or filed).**
- Build with the selected release's extras: the walkthrough's default omits `[plane-ui]`, and the plan would drop the
  view (#2191, #2198).
- Stage from a login shell: `config plan` resolves tools from the caller's `PATH` (#2194).
- `migration plan` can race live writes, and a repeat passes (#2195).
- `handed_off` is recorded even when the 30 s wait timed out, which happened for 7 of 21 bots (#2196).
- A restart's SessionEnd could be killed between sealing and closing, leaving the session `open`. clauDNA #406 writes
  both in one append.
- The stale local-scope clauDNA records in `installed_plugins.json` are harmless (the bots run the user-scope
  release), but `claude plugin list` can't say which one applies. The session-store format is the reliable signal.

### 2026-10-06 to 2026-10-07 — Half A's pre-merge check, the `agent_cli` rename, and §10 orders 4–6 merged

Run by a Claude Code session on the operator's Mac mini, over SSH for the Pi leg. Full report kept by the operator
(`~/rnc-results-5.md`).

**#2203 (P1 Half A) pre-merge live check, on `8e99743`, before the rename: PASS.** No bot touched, nothing activated.
- **The marker scrub, interactive** (macOS, Claude Code 2.1.291, tmux; both arms export the three markers with a
  foreign session id first).
  - Control: the footer shows `Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker`, the session
    takes a fresh id, and no transcript file is written.
  - #2203's line (`unset …; exec claude`): no warning, the session's own id, and a 267 KB transcript.
  - Inside the scrubbed session the tool shell still sees `CLAUDE_CODE_CHILD_SESSION=1` and `CLAUDECODE=1`, with
    its own session id: Claude sets them on its own children (C10/C11). The evidence for the scrub is the absent
    warning, the saved transcript and the own id, not an empty marker.
- **The field and its refusal, read-only on the floor host's real manifests** (PR checkout outside the live root,
  login shell).
  - `config validate` for all four fleets: rc 0, 0 errors, warning counts, categories and text identical to the
    selected release's.
  - `config explain` for the field: `source: built_in`. The selected release answers "not a declared model field".
  - On a copy of one fleet's `fleet.yaml`: `codex` on one bot gives rc 4, naming the bot and `execution adapter not
    shipped`; `claude` gives rc 0. The live checkout's `git status` was empty before and after.
- **Runbook lessons.** macOS `printenv A B` reads only `A`: use `env | grep`. A tmux server started from a Claude Code
  session hands the pane that session's markers, so a test arm starts from an `env -i` base. `config explain` for a
  bot key needs `--bot` and reports the source, not the value. Fleets live at `local/home/<fleet>/`. The warning
  names `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` as the override, should a bot ever need to run with an inherited
  marker.

**The rename (operator, 2026-10-06 and 2026-10-07).** `runtime` already names release activation (#1997), so the
value is spelled `agent_cli` everywhere: the `fleet.yaml` key, `KNOWN_AGENT_CLIS`, `CLAUDLOBBY_AGENT_CLI` in
`bot.conf`, `BotPayload.agent_cli`, `session_alias(id, agent_cli)` and the `(agent_cli, session_id)` join key. The
OTel resource attribute the P2 plan composes is `claudlobby.agent_cli`, not `agent.runtime`. A leftover `runtime:`
key in `fleet.yaml` warns (`retired-key`), and is refused when its value isn't the CLI the bot runs. This log's
earlier entries keep the spelling they measured. #2203's squash commit on `main` kept its pre-rename title; its code
and CHANGELOG carry the new names.

**Merged 2026-10-07.**
- Claudron#224 (§10 order 4): the boundary spec, `(agent_cli, session_id)` throughout.
- #2201: the canary harness records Codex hook payloads, `CODEX_*` env names and the ancestor walk; it composes
  `claudlobby.agent_cli`.
- #2203 (order 5): P1 Half A.
- clauDNA#407 (order 6): `session.opened.agent_cli`, `session.json` as `claudna.session/2`, the export item's
  `session.agent_cli`, `session:<agent_cli>/<sid>:<seg>` provenance for non-Claude sessions (F9), the spec rules 1
  and 4 amended (F15) and the mission amended (D2). Released as clauDNA v0.27.0 (#408).

**Outstanding.**
- Half A's activation and rollout check on the floor host (`~/rnc-results-6.md`, by 2026-10-09 per #2203). It also
  moves every bot to clauDNA 0.27 at its restart. The Claudron PR that flips register row 10 to shipped follows it.
- The Codex batch (C1–C5, C7–C9; `~/rnc-results-4.md`). It was numbered before the Half A check but hasn't run. C1's
  `session_id` answer gates Claudron's ops-log id regex, P4 and #2149.

### 2026-10-10 to 2026-10-11 — Half A activated on the floor host, after two watchdog resets and a recovery

Run by a Claude Code session on the operator's Mac mini, over SSH to the Pi. Full reports kept by the operator
(`~/rnc-results-6.md`, `~/rnc-results-7.md`; logs on the Pi under `~/rnc-release-6/`).

**The activation stranded (2026-10-10 18:27Z to 2026-10-11 00:22Z).** Activation `728eb2c3…` moved the floor host
(Pi 5 on an SD card, 21 bots, four fleets) to Half A's release `r-f4555c47…` (source `c28c91c`). The host reset
twice under its systemd runtime watchdog (`RuntimeWatchdogSec=14`):
- **During `backup_saved`.** `PRAGMA quick_check` on the 1.19 GB plane-DB copy sorted through SQLite temp files on
  the card for about 22 minutes (#2313). Resumed with `SQLITE_TMPDIR=/dev/shm`, the same check passed in about 4.
- **During `bots_started`,** at the 16th bot start and load 13 to 14. Each started bot's resumed work piled onto
  the next start (#2314).

After the second reset nothing could continue the activation. The resume only re-observes recorded starts,
`repair-start` fit one bot of 15, native admission refused every unit, and rollback has no CLI verb. No record was
edited by hand.

**The recovery (2026-10-11).** #2312 adds `host repair-after-reset`.
- Run from a scratch CLI built from its branch (`d132dd6`), it proved the 16 recorded starts dead (the plane
  daemon, the 14 bots with results, and one bot start with none), archived them as `kind: host_reset` and reopened
  `ingest_started`, in one write that started nothing (17:49Z).
- The sealed release's own `--resume` then started the plane daemon and all 21 bots fresh, about 45 s per bot, and
  committed at 19:02:28Z. The activation is `active`. The bots were down about 24.5 hours.
- The watchdog was off for the resume, through a drop-in removed afterwards (14 s again at 19:21Z). A longer
  timeout isn't an option on this host: its own watchdog drop-in says the BCM2835 caps the timeout at 15 s (#2315).
  Peak load was 17.5 during the starts and about 27 just after, with the CPU mostly idle. Nothing reset.

**Half A's rollout check: PASS.**
- `CLAUDLOBBY_AGENT_CLI=claude` in 21 of 21 `bot.conf` (control: 0).
- The marker scrub: none of the three markers in the environ of three new bots' `claude`. Their transcripts are
  saved, and no pane shows "Transcript saving is off".
- clauDNA 0.27: the three current sessions are `claudna.session/2` with `agent_cli: "claude"`, and their
  `session.opened` carries it.
- The refusals, on a copy of one fleet's `fleet.yaml`: `agent_cli: codex` and a `runtime:` key each give rc 4
  naming the bot. The restored copy is byte-identical to the live file.
- The fleet-event row: `fleet notify` stored `source_ref` `fleet-events:sha:` + 32 hex.
- All 21 bots `healthy` and `ready`; a manager and a worker answered a DM. `host doctor` is rc 6, as on 2026-10-10.
  `plane doctor`: quarantine 27 (unchanged since 2026-10-07) and 14 reaped emits in 24 h (155 on 2026-10-10).

**Runbook lessons.**
- `fleet notify --event` takes `[a-z][a-z0-9_]{0,63}`: `rollout_check`, not `rollout-check`.
- `event show` doesn't print `source_ref`; read it from the stored row.
- A bot's current clauDNA session is the `open` one whose `claude_pid` is the bot's running `claude`, not the
  newest directory: an older, closed session's directory was touched a second after that bot's new one opened.
- `repair-after-reset --json` prints the envelope only; the resume command is the runbook's.

**Follow-ups.** Claudron#226 flips register row 10 to shipped (P1 Claudlobby Task 10's Half A step). The Codex
batch (`~/rnc-results-4.md`) is still outstanding.

## Plan changes this log caused (2026-10-05)

- **Epic §6 P0:** C6a, C10 and C11 carry their measured answers and what is still owed. §4 corrects what
  `CLAUDE_CODE_CHILD_SESSION` marks. §6 P3's child-guard bullet is struck. §9 gains the measured restart cost.
  §14 gains Q15 (summarizer telemetry) and Q16 (identity attributes on disk). §15 records this fold.
- **Plan 2:** Task 7 commits `SUBAGENT_SHELL_SHARES_SESSION_ID = True` from C11 and drops the marker branch, which
  never fires on a measured `True`. Task 7b is re-scoped: it runs only if the interactive leg shows a leaked
  `CLAUDECODE=1` changes a bot's boot, and then it scrubs `CLAUDECODE` too. Half B's C10 gate follows Task 7b.
- **Plan 4:** `records()` reads the event name from the log body. The env line counts are 6/8 (no temporality
  line). Risks gain the restart cost and the summarizer sessions. Task 0's C6a gate names the floor-host leg as
  outstanding.
- **Plan 5:** Task 5 (the guard reads the marker first) is struck. Its premise is falsified, and shipping it would
  record nothing for every session. The spec's §4.4/§11.5 closure and the SETUP_GUIDE bullet are corrected to
  match. Its canary table carries both answers.

## Operator legs (your machine)

Run them from the PR branch's checkout. The instrument is stdlib-only and safe to copy alone. Each step names
what to type.

**1. Interactive C11 and C10 in tmux (any host with Claude Code; ~10 min).**

```bash
H=harness/runtime-neutral-canary.py
python3 $H setup --dir ~/rnc --port 14319          # prints these steps too
python3 $H receiver --port 14319 --out ~/rnc/otlp   # terminal A, leave running
# terminal B — the clean arm, a real tmux-hosted session:
tmux -L rnc new-session -d -s bot ". ~/rnc/otel.env; cd /tmp && claude --settings ~/rnc/settings.json"
tmux -L rnc attach -t bot
```

In the session, type the two instructions `setup` printed: the main-thread `env | grep …` into
`~/rnc/bash-main.env`, then the same command through an Agent subagent into `~/rnc/bash-sub.env`. Wait 70 s,
`/exit`, then run `python3 $H report ~/rnc`. Read `bash-main_matches_start`, `bash-sub_matches_start`, and the
`env` values of `subagent_tool_hooks` and `main_tool_hooks_env`.

Then the C10 arm. Move the first run aside (`mv ~/rnc ~/rnc-clean`), run `setup` again, start the receiver again, and start the session
with the leaked markers:

```bash
tmux -L rnc10 new-session -d -s bot "export CLAUDE_CODE_CHILD_SESSION=1 CLAUDECODE=1 \
  CLAUDE_CODE_SESSION_ID=00000000-0000-4000-8000-0000000c0010; . ~/rnc/otel.env; cd /tmp && claude --settings ~/rnc/settings.json"
tmux -L rnc10 attach -t bot
```

Record one thing: **does it boot to the input box normally**, or does it warn or refuse because `CLAUDECODE`
is set? Then run the same two instructions and `report`. The foreign id must appear nowhere. `tmux -L rnc kill-server; tmux -L rnc10 kill-server` when done.

**2. C6a on the floor host (the Pi; F4's gate; ~15 min plus the 24 h sample).** Same `setup`/`receiver`, and the
same clean-arm session as step 1. Done when the `report` shows `all_json: true`, both paths are counted, and
`resource_attrs_on_every_row: true`. While it runs, record the receiver's RSS (`ps -o rss= -p <pid>`) and the
`otlp/` growth per minute. **If the export fails here, post `[FORK-REOPEN F4]` on #2144 before P2-a1 opens.**

**3. C6a on the canary RC bot (canary root, never production; 24 h).** Append the `otel.env` lines to that bot's
tier file `$BOT_DIR/.env` (the bot tier, `lib-common.sh:4138`), run the receiver on the host, and restart the bot
through the canary CLI. Record:
- **day 1:** RC comes up after the restart, and one inbound Telegram message gets a delivered reply;
- **bot-hour volume:** `wc -c ~/rnc/otlp/requests.jsonl` and the request count after one hour;
- **the overlap:** after 24 h, `jq -r 'select(.event=="tool_result") | .attrs.tool_use_id' ~/rnc/otlp/logs.jsonl | sort -u | wc -l`
  against the transcripts' `jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .id' <the bot's transcripts> | sort -u | wc -l`
  and the plane's `claudlobby event list --bot <bot> --type tool_call --since 24h --json` (the receiver keeps
  `tool_use_id` values: they are ids).

Remove the lines from `$BOT_DIR/.env` and restart the bot when done.

Paste each `report` and the notes here, as a new timestamped entry.


## 2026-10-15 — Build-all sprint ledger

### Kickoff gates and goal

**Goal condition:** complete the remaining epic verification checklist (§8), integrate each planned phase in its declared dependency order, publish the releases its sibling consumers require, and verify deploy reachability and mixed-runtime acceptance. A code merge alone does not satisfy a live or timed observation. Any unreached check stays named below; the epic is not complete while one remains.

- **Passed (recorded evidence):** orders 1–6, including clauDNA v0.27.0 and Half A's rollout; see the dated entries above. These are historical observations, not a fresh claim about the production fleet.
- **Passed:** phase docs and execution rationale are in epic §10/§10.1. The export release precedes its consumer; the telemetry canary precedes the plane-reader leg; measured Codex behavior precedes owner-side adapters and execution companions.
- **Authorized by the operator in this chat:** execute the entire planned bill of work without inter-phase confirmation. For code review, “just use subagent reviewers to run simplify, review, and verify procedures”; after convergence, admin merge is authorized. This replaces the prior human code-review gate only; it is not evidence that a runtime observation passed.
- **Constraint:** use Codex for reasoning and delegation; do not spend Claude model quota. Non-model CLI operations against the software under test are distinguished from Claude model turns.
- **Passed for the candidate code:** actual installed-CLI hook dispatch against a local fixed-response provider stub; prompt activity, sealing, private entrypoint and recorded-door exports observed. This proves the CLI/plugin protocol, not provider authentication or model acceptance. The published-version check is repeated at release 0.28.
- **Blocking at their dependent phases:** P2-a1's seven-day independent-root canary and day-1/day-7 reply, coverage, RSS and latency evidence; Half B's 48-hour keepalive/restart observation; the Codex batch's isolated authentication and measured feature support. No timer is silently shortened and no unsupported field is invented.
- **Planning inputs pending:** P4 and companion phase docs are written from the Codex measurements, as the epic requires. Existing locked decisions remain binding.

### Invariant registry

| Invariant | Check | On breakage |
|---|---|---|
| Claudlobby instructions remain byte-identical copies | `cmp CLAUDE.md AGENTS.md`; `cmp claudlobby/_runtime_scripts/CLAUDE.md claudlobby/_runtime_scripts/AGENTS.md`; `pytest tests/test_instruction_budget.py` in a prepared disposable export | Repair the authored Claude-side file, copy, and re-check before merge. |
| Existing Claude session aliases and receipt replay remain compatible | `pytest tests/test_plane_ids.py tests/test_request_receipts.py` (resolve names against each phase's current tree) | Stop integration; repair against before-leg behavior. |
| clauDNA activity stays frozen; export is additive and private records withheld | clauDNA `pytest tests/test_session_store_activity.py tests/test_session_store_export.py tests/test_session_store_hook.py`; `make test-runtime` | Fix before any release or consumer integration. |
| No consumer or register claims an unreleased sibling surface | Verify `gh release view` for the exact pinned tag and run the sibling contract/conformance suite before register/pin merges | Hold the dependent merge; publish through the owner's release process first. |
| Telemetry stays local and explicitly armed, raw storage bounded and private | P2 parser/composer/intake/sink tests and actual loopback CLI probe once the surface exists; repeat after each integration affecting it | Refuse activation; fix the producer/receiver contract. |
| Phase scope and public assets stay clean | `git diff --check`; `git diff origin/main...HEAD --stat`; review the staged diff for identifiers/secrets and unrelated cargo | Unstage or repair; never force a source change onto main. |

Run all applicable checks after every phase merge, recording exact output and any not-yet-created test path explicitly. A missing check is not a green result.

### Merge topology and reachability

Use fresh branches from the latest `origin/main`, squash merges with exact-head matching, and no direct commits or force-pushes to main. Avoid stacked phases; if parallel phases overlap, the second rebases and regenerates switch tables, system example and instruction copies before fresh checks. Preserve unrelated working trees and untracked files.

- **clauDNA:** export PR → release 0.28 through `scripts/release.sh` → exact tag/readback → Claudron row-11 register PR and Claudlobby 6b consumer. The plugin becomes live on a host only through the host's managed update/restart path.
- **Claudlobby:** telemetry 7a and export 7b are independent; 7c follows release 0.28. Half B is an independent ready phase. P2-a2 waits for the 7a canary week; 6a waits for 6b. Each release still needs an independent-root staged activation and observed rollout to be live.
- **Claudron:** row 11 cites released clauDNA 0.28; the Codex adapter and row 12 wait for actual Codex measurements and the owning release. Do not edit the engine's main checkout.
- **Codex:** the canary batch may run alongside the Claude-path phases. Its results ground P4, then the execution and clauDNA companions. Record unsupported features and unresolved decisions rather than constructing fixtures from presumed producer behavior.

### Phase results

| Order | Phase / plan | State | PR / CI | Reachability |
|---|---|---|---|---|
| 7b | P3 clauDNA export contract / plan 5 | DONE: owner implementation, release and register integrated | clauDNA#411 `312e93c`, #412 `fe665ea`; Claudron#228 `bb4b72f` | v0.28.0 published; consuming host update/restart is separate and not claimed. |
| 7a | P2-a1 telemetry intake / plan 4 Tasks 3–8 | Integrated with 6b; parser/privacy findings under final delta review and full verification | Local `bb8a9c81`; no PR yet | Seven-day canary is required before merge; no production activation. |
| 7c | P3 summary consumer / plan 6 6b | DONE: reviewed, verified and integrated | [#2361](https://github.com/Claudfather/Claudlobby/pull/2361), `b4ae5dae`; mandatory Linux/harness/conformance passed | Native composed-producer/cache observations passed; merged, NOT live in production. |
| 8a | P2-a2 plane rows/readers / plan 4 Task 9 | Waiting on 7a canary evidence | Pending | Not live. |
| 8b | P3 Claudron pin / plan 6 6a | IN PROGRESS: fresh branch after 6b; own before-leg running | Local phase branch; no PR yet | Real scratch migration/harvest and per-host runbook required. |
| 9 | P1 Half B session joins / plan 2 | Code/reviews/full comparison passed; live/timed gates block merge | Draft [#2360](https://github.com/Claudfather/Claudlobby/pull/2360), `81147ec2`; all reported CI passed | Native protocol sample passed; real lifecycle and two 48-hour observations remain. |
| 10 | P0 Codex canary batch / existing runbook | CLI/config preflight passed; isolated authentication choice pending | Measurement only | No authenticated model turn or runtime measurement claimed. |
| 11 | P4 owner-side adapters / per-repo plans | Waiting on measured Codex answers | Pending | Owner releases and parity evidence required. |
| 12 | Execution and clauDNA companions / #2149 and clauDNA#404 | Plans follow P4 | Pending | Mixed-runtime acceptance not yet demonstrated. |

### Phase entries

**Export evidence carried forward:** at `0e6e0ea`, all eight GitHub verification checks pass; paid Claude routing evaluation is skipped. Local pinned `make check`: 2058 passed, 9 skipped, one pre-existing macOS sed failure (`tests/test_pretooluse_permissions_hook.py::TestWriteRedirectionKeepsThePrompt::test_dev_null_still_approves`), exit 2. Python 3.9 runtime: 875 passed, exit 0. Existing export suite: 63 passed before and after simplify. Offline canary: seven checks, 15 real shell-hook calls with synthetic payloads, zero model calls and unexpected errors. These results do not prove an authenticated host dispatch or a production rollout.

### Review rounds and class sweeps

Historical export review fixed retired-only exhaustion and unreadable-manifest gaps, then canonicalized the owned telemetry role. The actual local built-in simplify prompt ran through four independent Codex lenses in two waves: reuse and altitude clean; redundant archive scans and eager live projection reads fixed. A fresh exact-head convergence review follows under the operator's new review authority. Cross-repository class sweeps and final verdicts are appended per round below.

### Owner-decision and external-evidence queue

- Code review and admin integration are delegated and authorized; no repeat confirmation is requested for those steps.
- No seven-day or 48-hour observation exists yet for the new phases; record the real start/end and evidence before dependent merges.
- Isolated Codex authentication must use the documented supported path; do not copy authentication material or change user config by inference.
- Any new auth/security fork or unsupported runtime capability is a named decision, not an automatic waiver.
- Production host activation and outbound canary messages require concrete scoped host/channel authority; independent local samples proceed without those writes.

**Review round 1 — export convergence at `0e6e0ea`:** code clean; two source-plan omissions found (phase-3 C10/C11 evidence fold and export module docstring status flag). A clauDNA worker owns both. Class sweep also found an unconditional flag/cursor-equality claim in plan5; corrected here to untruncated pages, matching the existing bounded-status test and normative spec. The concurrent branches carry the same pre-integration plan copy and will receive this ledger/plan correction on rebase; no duplicate conflicting edit is assigned. P1 Task7b stale marker prose is assigned to the session-joins phase worker.


**Class-sweep evidence — round 1:** tracked text across the three repositories, their main snapshots and active sprint branches was searched for `CLAUDE_CODE_CHILD_SESSION|CLAUDLOBBY_HOOK_CHILD|SUBAGENT_SHELL_SHARES_SESSION_ID` and export/status descriptions. Nine initial snapshots yielded 867 matching lines (copies and broad vocabulary counted separately): Claudlobby source 44/101, ledger 85/101, telemetry 85/101, session joins 86/101; clauDNA export 5/73, source 2/38, main 2/41; Claudron source 0/0, main 1/1 (marker/export matches). Executable pins and dated/struck history are legitimate; active clauDNA phase-3 evidence and export docstring omissions are fixed at `3c857c3`; active P1 Task 7b prose is owned by the session-joins branch. A narrow `git grep -n -I -F '`next` is the same with or without the flag' -- .` sweep found five copies: the ledger copy is now qualified to untruncated pages; two active phase copies receive that fix when rebased onto this ledger merge; source/main copies describe their pre-integration snapshot. clauDNA and Claudron have no match for that literal. The actual bounded-page behavior is already pinned in the export suite. No producer-source reconstruction was accepted as a live fixture.

**Export candidate evidence — `3c857c3`, 2026-10-15:** the docs-only correction leaves executable AST unchanged. Affected export/runtime-layout checks: 148 passed, exit 0; skill/integration/changelog checks exit 0 (39 skills, 61 existing warnings); Ruff and diff checks exit 0. The repeated offline canary passes seven checks through 15 real shell-hook invocations, peak 60.13 ms, no unexpected hook errors and zero model calls. Its first invocation used the system interpreter while wrappers deliberately selected the development venv; the interpreter-identity assertion failed. Running the documented venv interpreter passed; this was a harness invocation mismatch, not a product change.

**Native CLI protocol canary — `3c857c3`, 2026-10-15 21:25–21:27 UTC:** installed Claude Code 2.1.301 loaded the real candidate plugin in private HOME/config/state, with empty MCP configuration, real hooks and native OTel exporters. A loopback Messages stub returned fixed SSE responses under a fake key; no Claude model quota was used. Both the one-response arm and a bounded `Bash true` arm exited 0. Actual hooks recorded one prompt, opened/sealed/closed the session, and wrote a schema-valid mode-0600 entrypoint pointing at the candidate package (`agent_cli=claude`, current pre-release manifest 0.27.0). Exports invoked through that record both exited 0 and validated: plain zero items; flagged one disabled-status item with `counts.prompts=1`, `sealed_by=session_end`. Summary generation was deliberately off. The short arm reported its asynchronous prompt hook response cancelled after its event had already landed; the tool arm reported hook success and recorded the same one prompt. Actual OTLP/HTTP JSON logs/metrics, including native `tool_result`, were captured privately mode 0600 for the intake fixture. A refusing proxy rejected auxiliary requests, but environment proxies are not proof of OS-level egress confinement. This is protocol evidence with a local provider stub, not a real-provider or production-bot claim. The isolated plugin-manager update check separately observed the old cache directory surviving an update; release-version/native-dispatch readback follows the actual 0.28 release candidate.

**Parallel-merge adjustment:** P2-a1 remains subject to its seven-day pre-merge canary. To preserve the epic's P2/P3 parallelism, 6b may land first with the identical reviewed strict-mapping helpers currently implemented by the telemetry worker. In that order 7a rebases onto 6b and keeps only its telemetry registration; no duplicate helper implementation is introduced. Consumer fields remain owned by 6b.

**Review round 2 — clean:** an independent exact-head read of clauDNA `3c857c305a0201d56bdd1da96db67e167b699614` against `046115e42540f5073c7d51aaaff33600d73c9244` returned **NO FINDINGS**. Both prior omissions are closed; no new implementation or requirement gap was identified. The same reviewer returned **NO FINDINGS** on this two-document governance diff, including its protocol/provider/rollout distinctions and timed gates. The review used supplied canary evidence and performed no tests, network or model calls. The final candidate is pushed once for fresh CI; a clean candidate check run precedes its authorized admin merge.


**7b integrated:** clauDNA#411 merged by authorized admin squash on 2026-10-15 at `312e93cf279f1431825becf8c6cf040e28856509`, from the exact reviewed/green head `3c857c3`. Fresh CI on that head passed all eight verification jobs; paid routing evaluation was skipped. The candidate's frozen activity/export/entrypoint and Python 3.9 runtime gates passed in CI; Lobby source was unchanged by this owner-side feature merge. Release #412 is a fresh branch from that merged main, not a stacked feature branch.

**0.28 release candidate:** repository `scripts/release.sh minor` produced `de3a606a047bce69a92cd18ca1b498cc5d1fc50b`; only both plugin versions and the dated changelog heading changed. Independent review: **NO FINDINGS**. Manifest/changelog validators passed. A fresh actual installed-CLI tool arm against the same fixed local provider protocol exited 0 and wrote a valid mode-0600 entrypoint with `plugin_version=0.28.0`; recorded-door plain/flagged exports validated (zero plain items, one disabled item with one prompt). No executable source changed. Only the branch is pushed, with follow-tags explicitly disabled; the main-push workflow must create the remote tag at the later merged SHA. Release/tag readback is required before row 11 or 6b can claim the released contract.

**7a implementation checkpoint:** local `218a016a` has a separately prepared before/after comparison: 858 / 925 passed, both exit 0, no failing names. Direct harness before/after: 352 passed, zero failed, exit 0. Ten targeted mutants each failed as expected. An actual local CLI receiver/sink sample accepted captured native logs/metrics with HTTP 200, compact private files (0600/0700), stripped metadata-mode forbidden attributes, refused protobuf with 415, exited cleanly on SIGTERM and passed the sink doctor check without creating Plane state. These local protocol/storage results do not satisfy the seven-day floor-host/RC acceptance. The fixture is a redacted actual Claude 2.1.301 exporter body captured from the local-provider CLI arm, explicitly distinct from the older C6a floor-host evidence.

**Half B implementation checkpoint:** implementation `5d27470c` plus docs `c09656b1`; source correctness review returned **NO FINDINGS**. Focused before/after: 172 / 203 passed, both exit 0; an exact-head broader affected set passed 323 tests, exit 0. Direct harness before/after: 352 passed, zero failed, exit 0. Three receipt/session/reader mutants failed as expected, then restored checks passed 66 tests. A real installed CLI/Plane sample proved committed A attribution survives retry B, prepared-unrecorded legs can use B, task-history/session lookup and format-1 receipt intent compatibility. Supervisor lifecycle was simulated, with no live bot/model; report commit succeeded and delivery could not reach a nonexistent bot, as expected. Actual rollback previews to the Half A and pre-P1 releases exited 0 without receipt-format refusal. Full-suite comparison, real live-session/subagent reference and both before/after 48-hour windows remain pending. Simplify reuse and complexity lenses are clean for production code; one minor duplicated test encoder fixture is queued for the batch convergence fix.


**7b release and register complete — 2026-10-15:** clauDNA#412 merged `fe665ea6ce6cdaba95bc8692de09b1f1f6b87b77`; all nine reported verification checks passed on its reviewed head. [Release-tag run 38811482187](https://github.com/Claudfather/clauDNA/actions/runs/38811482187) succeeded and [v0.28.0](https://github.com/Claudfather/clauDNA/releases/tag/v0.28.0) published at 21:39:15 UTC. Remote annotated tag `667fef0a57f8b13d24809a9deb81e711f56651d8` peels to that merged commit; its tree equals the actual native-tested release candidate. Only the runner-created unpublished local tag was removed before fetching the published tag. Claudron#228 then passed independent review (**NO FINDINGS**) and both CI checks, and merged `bb4b72f4bf5c0623a0391bda9cd8bd59031aa793` at 21:49:49 UTC. Row 11 now points at the versioned owner's §8 and distinguishes the planned consumer from the shipped owner. No engine behavior changed and no production fleet installation is claimed. 6b is now implementing against that released contract.

**Invariant checkpoint after the owner release:** from the prepared independent before export, `pytest tests/test_plane_ids.py tests/test_request_receipts.py tests/test_instruction_budget.py -q` returned `39 passed in 0.26s`, exit 0. Both source instruction-copy comparisons passed. clauDNA's frozen activity/export/hook/runtime gates passed in its exact release-candidate CI, and released bytes match the checked candidate tree. The owner tag and Claudron register readback above close the unshipped-surface invariant. New telemetry surfaces remain unmerged and opt-in; their actual local storage/protocol evidence is recorded separately. Scope/diff checks pass for all three integrated PRs.

**7a review round 1:** independent spec review found malformed gzip can raise uncaught `zlib.error` instead of HTTP 400/refusal accounting, plus missing host-job operational comments (runtime retention parsing, shared port and disarm recipe). Both are assigned together; a real malformed-DEFLATE HTTP regression first failed (one failed, 72 passed, exit 1). The class sweep also found the current P0 capture harness's decoder, which remains used by this sprint; it is included in the fix batch while preserving that instrument's deliberate immediate-200 semantics. The optional prune-after-append 503 diagnostic is not a spec violation because the declared append includes retention; evidence will explicitly distinguish an unacknowledged append from proven body loss. No mapping-helper changes are needed.

**Half B simplify round complete:** reuse, complexity, efficiency and altitude have no production-code findings. The duplicate report-test encoder wrapper was removed and six affected tests passed at `81147ec2`; indexes suggested for optional session-query efficiency require a schema migration and remain explicitly excluded by this phase's no-migration boundary. A separate native local-provider experiment now observes real CLI hook-store IDs and the actual Bash acceptance door: the independent store ID derives to the recorded Plane UID, and a second actual session replays without changing that first attribution. Fresh-session replay is not a claim of literal `/clear`; supervisor, Agent and timed legs remain explicitly bounded by the final protocol report. Full before/after suites are still pending until their actual terminal output exists.


**Native environment check — 2026-10-15:** installed Claude Code 2.1.301 with the released 0.28 plugin and an explicit parent OTel exporter block ran a names-only recorder through actual SessionStart, UserPromptSubmit, PreToolUse and SessionEnd hooks, plus actual Bash. All five observed environments contain no `OTEL_*` names; `CLAUDE_CODE_ENABLE_TELEMETRY` remains. Native OTel requests still reached the private loopback receiver. This local fixed-provider protocol sample confirms the scrub premise without claiming the floor-host/RC week or changing the historical summarizer evidence.

**Verification ownership correction:** the first Half B full-suite processes ended without terminal counts or exit files after their worker returned; partial before/after logs are retained and excluded from evidence. A persistent verification agent verified 881 tracked code/test files in each prepared export against the intended baseline/candidate and restarted only the missing full pair, remaining active through terminal results. Affected results, native samples and mutants above remain valid. 7a's 858/925 and 88 results are affected checks, not its full-suite gate. Both full 7a legs remain queued for the final post-6b base/rebased candidate, avoiding an obsolete long run followed by another after the already planned rebase.

**7a quality round:** four confirmed findings are assigned as one batch: rollback a partial raw write before any future acknowledged append (and refuse if rollback cannot preserve the line boundary); guard invalid-Unicode serialization and recursively unparsable tool parameters; accumulate tiny chunks without one retained object per chunk; establish the socket timeout before header parsing so an incomplete header cannot indefinitely block graceful shutdown. Existing malformed-gzip and operating-comment fixes were verified first. Common mapping helpers remain unchanged. Full gates and the week are still pending; no source-ready claim is made while these findings remain.

**6b contract class sweep:** released 0.28 summaries use an array of `{step, result}` arc entries, structured journey lists and successful outcome `shipped`. The consumer follows that actual schema. Across the three repo/main/sprint snapshots, the specific stale-shape/`completed` outcome claim yields four hits per old Lobby snapshot: three in the P3 consumer plan, fixed by its worker, and one canonical epic table row, corrected here to `shipped`. clauDNA/Claudron scans have zero hits for that class. The worker's remaining nested-arc stress-test wording is a legitimate new-contract match. Old snapshot copies receive the correction on integration; dated history is retained. Real native 0.28 summary generation against a fixed local structured-output provider produced the captured `ok` fixture; the actual released export door validates/maps it with the disabled segment. No source-derived producer fixture or real Claude model call was used.


**Ledger integration:** docs-only Claudlobby#2359 merged `9cd03abf59a12988c014200481d4f3e4d9ea45d1` at 22:05:22 UTC after independent review, diff/copy checks, the 39-check invariant sample and passing rollout/vault/conformance/harness checks. The three optional full-platform jobs were still running; no green full-suite result is claimed. The live repository ruleset requires one review and declares no required status checks; the operator's delegated review/admin-merge authorization covers this docs-only integration. Executable base bytes remain those of `18f01cba`, so source phases adopt the initial ledger/plan fix by rebase. Subsequent progress and the canonical stale-outcome correction travel with 6b in a separate parent-owned docs commit before its reviewed push.


**Native Half B protocol result — `81147ec2`:** all five actual Claude Code 2.1.301 arms exited successfully against a fixed loopback provider, using the released 0.28 plugin and a sealed installed CLI whose 329 executable package files match the candidate. Independent hook-store session IDs derive to the Plane UIDs observed through the actual Bash assignment-accept and report doors. Fresh sessions replay the original receipt and preserve its first session attribution; exactly one accept/report row exists per request. A real advertised `Agent` tool launched a linked child Bash report, with one native `subagent_completed` event; the child door's UID matches the parent hook store. Report receipt commits succeeded and delivery returned the expected failure because no manager bot existed. Five Agent-arm Messages requests were all loopback; no real provider/model quota was used. The supervisor adapter was explicitly simulated. Literal interactive `/clear`, real service/tmux/supervisor acceptance, the before/after 48-hour observations and real-provider acceptance remain pending. The Agent leg exercised a report, not the plan's literal assignment-progress door.

**6b implementation checkpoint — `b084858c`:** 68 owned files implement the released export consumer, strict `claudna` mapping, composed summary/harvest gates, timer/switches, bounded reader-visible events, per-item durable emit before ack, stall handling, captured contract/conformance and retirement of both old hooks. No Claudron pin bump is included. Final focused checks: `668 passed`, exit 0; affected shell/doctor/package checks: `328 passed`, exit 0. Direct harness: `359 passed, 0 failed` (before `352 passed, 0 failed`); all seven new checks passed by name. All 13 configured mutants failed with a named test and exit 1; a later verification audit found the two-item cell only proved cardinality, leaving 12 exact required procedures plus that surrogate. Genuine mixed-batch consumer evidence remains pending below. The real released export door validates/maps two native-capture-backed items. A separate sealed installed-CLI sample passed host setup, staged config plan/diff, actual export and ack, reader-visible journey/disabled events, and a second empty tick. Its lifecycle adapter was simulated; actual installed CLI/Plane/export operations were real and executable source equivalence was checked. The full before/after suites remain active. The broad after5 leg predates the final private-tick local `--json` parser flag, estate-silencer no-op, and harness call correction; those bounded deltas have the exact-final focused/affected/sample evidence above. It must not be described as an exact-HEAD full-suite run. Independent simplify/code/spec review remains required before push.

**7a quality-fix verification:** the four intake findings and the current P0 decoder sibling regressions reproduced as `12 failed, 53 passed`, exit 1, then passed as `65 passed`, exit 0. The in-scope Plane socket reader also retained one bytes object per receive; its bounded bytearray replacement preserves the existing size cap, absolute deadline, EOF/error behavior and bytes return. The many-short-receive regression failed before (`1 failed, 113 passed`, exit 1; peak 12,301,074 bytes) and the fresh final affected export passed `114 passed`, exit 0. This is a transport memory fix, not P2-a2's Task 9 mapping or wire contract. Final class sweep, commit and independent clean review follow before any ready claim.


**6b review round 1 — two findings reproduced/assigned:** a successful ack for an earlier session reset the bot's tick-level failure streak before a later session could fail, suppressing the three-tick stall page. The fresh regression failed with `1 failed`, exit 1; the worker is moving failure/stall reset to complete-tick success and preserving per-ack timestamps. The reset-class sweep found eight sites in the old consumer; the corrected candidate has six legitimate skip/full-success reset sites, with none inside the session loop. Other active sprint branches and both siblings have no consumer implementation of that class. Separately, all-skipped rows cannot establish summary configuration: trivial/no-transcript/gave-up/retired/unexportable can occur while armed. The observed metric is now summaries available on k of N represented bots. The old consumer branch has seven claim matches; the phase worker corrects its six, and this parent-owned canonical table corrects the seventh. Old Lobby main/other sprint snapshots have three copies each, to be replaced on integration; clauDNA/Claudron snapshots have zero. Actual builtin simplify reuse is clean; other independent lenses and final convergence follow the fix.

**Native exact Agent progress extension:** one additional actual CLI arm against the frozen Half B installed release committed `assignment progress` from a linked Agent child Bash. Both its communication and task-event session UIDs equal the installed derivation of the independent parent hook-store ID; one native `subagent_completed` event and five loopback provider requests were observed. CLI exit 0; the door's notification exit 5 is expected with no manager bot and does not negate committed recording. This closes the exact progress-door protocol leg without real provider inference or service-supervisor claims. Two bounded interactive attempts stopped at the native first-run Anthropic connectivity check, refused by the sample's deliberate proxy, before any model request. Literal `/clear` remains unobserved; the attempts do not count as successful session or canary evidence.


**6b broad baseline terminal result:** the separately prepared before export finished normally: `9553 passed, 78 skipped, 2 warnings in 2185.99s (0:36:25)`, exit 0, 9631 collected. The broad after5 leg remains running; no before/after verdict is claimed yet. Native simplify complexity is clean; efficiency found three bounded cleanups now assigned: avoid serializing the whole unbounded journey before trimming, group export items once by session instead of rescanning per cursor, and retain mapped records only in dry runs. Required per-item durable emits and producer ordering remain unchanged.

**Additional canary boundary:** 6b's source plan requires a real armed/unarmed bot pair under the candidate composition. The sealed-CLI/Plane sample with simulated supervision proves export/ack/readback and does not satisfy that remaining bot-host leg by assertion. A private interactive Half B attempt with only fresh onboarding state reached the real terminal/trust prompts, but automatic approval review rejected the proposed receipt-replay input twice, citing possible quota/data exposure under the operator's Codex-only instruction. The process was cancelled without any Messages request (zero model calls); the specific fixed-loopback/fake-key test authorization is pending in chat. No alternate input transport or real credential was introduced. Isolated Codex login choice is separately pending. Continue all unaffected code and test work; do not merge past the unobserved mandatory sample.


**6b review-fix batch — `f3b0abce`:** two correctness/prose findings and three efficiency findings are folded into one separate commit. Emit/ack failure matrix reproduced two failures before the fix; the final affected suite passes `798 passed in 52.65s`, exit 0. Failure streaks reset only after complete tick success, with progress/failure/recovery cells. Bounding now trims without a full serialization of unbounded journey input (the old path's measured peak was 60,121,381 bytes for 30 million source characters); session grouping preserves producer cursor and per-session order; normal ticks retain no preview records. Actual builtin reuse, complexity and altitude lenses are clean; final convergence review is pending on the fixed head. Fixture capture README now cites actual CLI 2.1.301, matching both native command records, without rewriting legitimate dated floor-host 2.1.263 evidence. The old broad after5 leg continues to terminal; a new separately prepared exact-final after leg is required for this runtime-fix head, paired with the completed unchanged before leg. Parent canonical-doc changes are separate from the worker's seven-file fix commit.


**Half B full comparison and draft:** both authoritative prepared runs terminated normally. Before `18f01cba`: `9553 passed, 78 skipped, 2 warnings`, exit 0; candidate `81147ec2`: `9584 passed, 78 skipped, 2 warnings`, exit 0. Summary-scoped failing/error names are empty in both; passing delta +31. Post-run proof confirms 881 source/test files unchanged and the source tree clean. Earlier interrupted logs remain excluded. Authorized draft #2360 is published at that exact reviewed head, body read back and attached to the chat; CI is running. Literal `/clear`, native service acceptance and both full 48-hour windows remain explicit pre-merge blockers; the PR stays draft.

**6b fresh composed-producer sample passed:** the final consumer bytes at `f3b0abce` were sealed in a new independent root; equivalence covers 655 distributed runtime/native/contract/library files, zero mismatches. Two actual native 2.1.301 `--print` sessions ran from their bot directories with unchanged candidate-composed `bot.conf` and settings, released clauDNA 0.28 and fresh stores. The armed session completed its actual detached summary through fixed loopback protocol; the unarmed session produced disabled. Both native SessionEnd paths closed. The actual sealed tick committed one event per bot, reader output rendered the journey/disabled fields, each real cursor advanced through segment 1, and the second tick added zero. No store was copied into this sample. Service actions were simulated, with no native-supervisor or real-provider quality claim; Task 8 explicitly permits SessionEnd. Scoped tool approval covered this separate noninteractive sample; it did not execute the denied literal `/clear` input. Its first preparation attempt failed before any SUT start because an output parent directory was absent; the unchanged runner passed after that directory was created.

**6b X18 cache observation passed:** actual native plugin-manager commands in that same bot's private HOME/config installed released 0.27.0 from a unique local marketplace, then updated to released 0.28.0. Cache versions changed `[0.27.0]` → `[0.27.0, 0.28.0]`; the older directory and manifest survived unchanged. CLI 2.1.301, no auth or model command, no remote marketplace/default or production-config change. This observes the independent bot context, not a production host rollout.

**6b verification audit and integration findings:** the older after5 full run terminated with `11 failed, 9576 passed, 77 skipped, 2 warnings`, exit 1. These are real omitted integration updates: new job startup delay and deterministic timer expectations, a state-path/skill-reference false match, and new vendored contracts missing from build identity inventory; the old harness snapshot also lacked the timer composition call already fixed before `f3b0abce`. A separately prepared correction passes `200 passed, 6 skipped`, exit 0; its wrapper and final integration commit are pending. No full green result is claimed. The audit also found the required two-item mutant used a cardinality surrogate; the separate ingest proof did not measure the mutated consumer's ack behavior. A private evidence worker now exercises real seen/new batching and the terminal fallback honestly. Any old 'cursor always holds' prose will be corrected from that observation, without weakening the protection. The exact-f3 full run remains active; after the final integration/premise batch a new exact final leg is required.

**7a simplify convergence — `a09d7215`:** all four actual builtin lenses and final code review are clean. The efficiency finding changed pruning from every append to the specified rotation/hourly cadence, including failed-rotation finalization to preserve the count ceiling. Affected checks reproduced `1 failed, 48 passed`, exit 1, then passed `50 passed`, exit 0. Controlled ordinary append at 30 files reduced `lstat` calls 61→1 and directory scans 1→0; existing open/close/fstat/pread counts are unchanged. Three-repo/17-ref/10-worktree class sweep records legitimate hourly and constructor checks. Post-6b rebase/full comparison, Linux CI and the seven-day floor-host/RC canary remain pending.

**Baseline CI readback:** docs-only #2359's tests run 38810734507 has now finished successfully, including Linux default, Ubuntu/Python 3.10, macOS/Python 3.11 and harness. This is the unchanged executable baseline, not candidate CI for either runtime phase.


**Genuine mutation procedures closed:** fresh private prepared exports exercised the production consumer with the real Plane emitter, actual seen seg1/new seg2, and observed rows at acknowledgement. Baseline: `4 passed`, exit 0; genuine combined-batch mutation: `1 failed, 3 passed`, exit 1; original-ID fallback mutation: `1 failed, 3 passed`, exit 1; restored: `4 passed`, exit 0. The combined batch produced real mixed-state refusals, retained normal seg1, replaced the new normal summary with unexportable statuses, then acked through 2 with pending 0. Original-ID fallback produced two real ledger/family refusals, no visible skipped row, recorded both errors in `last_unexportable`, then acked through 1. Neither held the cursor. The old cardinality surrogate and isolated ingest proof remain correctly narrower historical evidence; the required real procedure is now performed. The active source plan/canonical epic replace false unconditional cursor-wedge claims with normal-summary/fallback-status preservation and retain terminal acknowledgement. Four real behavior cells are being adapted into the existing committed suite, without private trace paths. Sweep covers 22 surfaces across three repositories; legitimate owner cursor monotonicity and harvest holds remain. Source equivalence checked 1,353 files per arm; only the intended consumer mutation differed, with identical Python 3.11.5/34 dependencies.


**6b final verification and integration — 2026-10-15 23:31:25 UTC:** [#2361](https://github.com/Claudfather/Claudlobby/pull/2361)
merged by authorized exact-head admin squash as `b4ae5dae86e7f8b62894dd8a500e24e2201cd17d`, from reviewed
`92e7e4e9707042094065da9fe39c8d9968167f38`. Independent completion review returned **NO FINDINGS**.
The separate prepared before/final runs terminated normally: `9553 passed, 78 skipped, 2 warnings` versus
`9595 passed, 78 skipped, 2 warnings`, both exit 0; scoped failed/error names are empty, and the +42 collected
cases equal +42 passes. Source proof covers 1,353 tracked files with zero mismatches and identical interpreter
and 33 non-Lobby dependency versions. The dated f3 run completed with ten known integration failures; it is
retained separately from the corrected final verdict. Final integration checks passed 222 with six platform
skips; direct harness passed 352/0 before and 359/0 after. Genuine mixed-batch/fallback procedures, actual
fresh armed/unarmed native SessionEnd sample, bot-context old-cache retention and final sealed replay/equivalence
were independently audited. The [default Linux pytest and harness jobs](https://github.com/Claudfather/Claudlobby/actions/runs/38819899651)
and [released export contract/conformance](https://github.com/Claudfather/Claudlobby/actions/runs/38819899677)
passed on that head. Supported Ubuntu/Python 3.10 and macOS/Python 3.11 were still running at merge; all-CI-green
is not claimed. Merged and candidate tree IDs both equal `0d927ef306efefa22d7a1fe38c02151a0df82d2e`.
Production activation remains a separately authorized host operation.

**Post-6b invariant checkpoint:** existing prepared isolated exports returned `39 passed` for Lobby identity,
receipt and instruction-budget checks, `197 passed` for released clauDNA activity/export/hook checks, and
`875 passed` for `make test-runtime` on the existing Python 3.12 environment; all exit 0. Both Lobby instruction
copy pairs match. The tested release tree equals published v0.28.0 (`fe665ea6`); release CI separately covers
Python 3.9. This checkpoint makes no new production or real-provider claim.

**7a parallel topology and integration review:** telemetry first rebased from `a09d7215` onto frozen consumer
`92e7e4e` so independent verification could continue. The planned post-squash rebase then moved it onto actual
`b4ae5dae`; its candidate tree was unchanged (`cdb1c9eb17caad0ee3d337b9d6e7b909e6edfd38`), and the baseline
tree equals actual main. The earlier integration-only review at `f4c524a6` preserved the prior blobs/modes of
52 consumer-only and 24 telemetry-only paths, shared mapping helpers and config-explain, both registrants/jobs/
doctor rungs and hook retirements. The subsequent parser fix below deliberately changes its own parser/reexport
files; the actual-main rebase preserves that fixed candidate tree. Integration
checks passed 982; final generated/instruction checks passed 87. The exact installed intake canary again observed
logs/metrics HTTP 200, protobuf 415, private directory/file modes, no Plane writes and clean SIGTERM. The sole
integration review finding was a blank line splitting a reference table, fixed on the authored Claude side and
copied. A tracked-Markdown sweep across three repositories, 17 ref snapshots and ten active worktrees found 28
occurrences belonging to three intentional table boundaries and zero remaining accidental headerless continuations.

**7a full-suite findings, still under verification:** the old frozen after export exposed eager backend imports
while constructing any CLI parser. The existing isolated parser/host subset reproduced `52 failed, 49 passed`,
exit 1, against baseline `101 passed`, exit 0. Moving the shared port/retention grammar into the existing
stdlib-only parser-module convention preserves public imports/defaults and the unchanged import blocklist;
the affected set then passed 1,084, exit 0. Reuse review is clean. A separate intake-before-sink privacy-negative
fixture/order finding is being reproduced and corrected; no terminal final full result is claimed. Old baseline
and failing after exports remain immutable through terminal output, and a fresh corrected after leg is required.
The seven-day floor-host/RC gate and P2-a2 dependency remain unchanged.

**6a preflight and start:** its fresh branch is based on actual merged `b4ae5dae`, and its own separately prepared
before-leg is running. Read-only contract analysis identified the need to preserve doctor's D001/D007/D008 and
other-code presentation while sharing the migration prober and a request-local 60-second status-plus-doctor
budget across validation and doctor. Source-only canary preparation found that released v0.6.1 creates format 2,
and a legacy harvest draft is needed for the real v0.9.0 m003 migration branch. No migration or harvest observation
is claimed until the actual private scratch-vault/native canary runs. Refreshed companion issues #2149 and
clauDNA#404 remain open and explicitly depend on measured Codex owner adapters; no speculative fixture or plan
was substituted for those missing observations.


**Supported-platform CI readback:** all reported checks for merged #2361 and draft #2360 now completed successfully,
including Ubuntu/Python 3.10 and macOS/Python 3.11. The earlier statements that those jobs were running describe
the merge/publication instant, not the refreshed state. #2360 remains draft because its live/timed acceptance is
independent of green CI.

**6a live-plan finding before migration:** actual released Claudron v0.6.1 created a format-2 scratch vault with
a legacy harvested draft; actual v0.9.0 status/doctor reported pending m003 and D001. The exact `983ac22b` wheel
was assembled into a sealed independent CLI. Its real `config plan` succeeded but exposed only generic
“configuration warning reported by the validator” lines: the existing metadata-only command discarded the new
category and fix instruction. The canary stopped before applying the migration, and the phase worker owns a
category-only/static-guidance correction that preserves redaction of authored paths and warning text. No
migration or harvested-draft success is claimed at this checkpoint. A private runner syntax typo and a build
from the commit-free prepared test index failed before host setup; the separate exact-commit packaging checkout
corrected the build provenance without changing the test export.

**Claudron response-reader class sweep:** an independent owner-contract read confirmed clauDNA treated present
malformed capabilities as a legacy empty set and filtered malformed resolve items into an apparently empty
search. A bounded owner fix at `070f80a1` rejects those unusable reads, preserves existing harvest cursors and
allows valid retries; valid legacy absence, empty results, authored sources and additive fields remain supported.
Test-first failures: 18; affected suites: 175 passed; local runtime: 901 passed; actual released v0.9.0/v0.6.1
engine lanes: 7/2 passed with 2/7 expected lane skips. Unsandboxed owner `make check`: 2084 passed, 9 skipped,
the same pre-existing macOS sed failure, exit 2; this is not a green local full check. Independent review and
exact-head CI precede integration. Lobby's shared reader has the corresponding capability/pending/finding
shape guard, and its status-envelope check is being tightened in the same phase.
