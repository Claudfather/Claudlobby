---
title: "Run log — runtime-neutral observability (#2145): canaries and build-all delivery"
type: run-log
status: build-all in progress; goal not met; owner export, consumer and Claudron pin merged; mandatory live/timed/Codex gates remain
created: 2026-10-05
updated: 2026-10-19
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

The historical P0 measurements follow. Current implementation, review and integration state is in the [2026-10-16 closeout](#2026-10-16-utc--corrected-candidate-closeout); the sprint ledger retains its dated delivery history.

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

**Current topology (2026-10-16 UTC):** delivered owner/register/consumer PRs used authorized exact-head squash merges. Corrected 6a [#2363](https://github.com/Claudfather/Claudlobby/pull/2363) merged normally as `e7a14ea1`, preserving reviewed `15ea28db` ancestry and tree equality. Telemetry [#2364](https://github.com/Claudfather/Claudlobby/pull/2364) now publishes `13ceadfe`, a zero-conflict normal merge of reviewed DNS correction `606e8798` with actual P4 main `94088c2b`. Its fresh full suite, all nine latest exact-head CI checks and independent terminal review passed. Half B remains unchanged at `5f979881`. Earlier e644/56a/9d checkpoints and failures remain historical evidence. No force push or history rewrite occurred. Each draft retains its own real/timed gates; preserve unrelated worktrees and untracked files.

**Superseded kickoff topology, retained as history:** ~~Use fresh branches from the latest `origin/main`, squash merges with exact-head matching, and no direct commits or force-pushes to main. Avoid stacked phases; if parallel phases overlap, the second rebases and regenerates switch tables, system example and instruction copies before fresh checks. Preserve unrelated working trees and untracked files.~~

- **clauDNA:** export PR → release 0.28 through `scripts/release.sh` → exact tag/readback → Claudron row-11 register PR and Claudlobby 6b consumer. The plugin becomes live on a host only through the host's managed update/restart path.
- **Claudlobby:** telemetry 7a and export 7b are independent; 7c follows release 0.28. Half B is an independent ready phase. P2-a2 waits for the 7a canary week; 6a waits for 6b. Each release still needs an independent-root staged activation and observed rollout to be live.
- **Claudron:** row 11 cites released clauDNA 0.28; the Codex adapter and row 12 wait for actual Codex measurements and the owning release. Do not edit the engine's main checkout.
- **Codex:** the canary batch may run alongside the Claude-path phases. Its results ground P4, then the execution and clauDNA companions. Record unsupported features and unresolved decisions rather than constructing fixtures from presumed producer behavior.

### Phase results

| Order | Phase / plan | Current disposition | Evidence / remaining gate | Reachability |
|---|---|---|---|---|
| 7b | P3 clauDNA export / plan 5 | DONE: implementation, releases and register integrated | [clauDNA #411](https://github.com/Claudfather/clauDNA/pull/411), [#412](https://github.com/Claudfather/clauDNA/pull/412), strict-reader [#413](https://github.com/Claudfather/clauDNA/pull/413), release [#414](https://github.com/Claudfather/clauDNA/pull/414); [v0.28.1](https://github.com/Claudfather/clauDNA/releases/tag/v0.28.1) published; [Claudron #228](https://github.com/Claudfather/Claudron/pull/228) marks row 11 shipped | Published owner contract; production host update/restart is separate. |
| 7c | P3 summary consumer / plan 6, 6b | DONE: reviewed, verified and merged | [#2361](https://github.com/Claudfather/Claudlobby/pull/2361), `b4ae5dae`; recorded full comparison, direct harness, CI and native protocol/cache evidence | Merged; no production activation claimed. |
| 8b | P3 Claudron pin / plan 6, 6a | DONE: reviewed, verified and merged | [#2363](https://github.com/Claudfather/Claudlobby/pull/2363), reviewed `15ea28db`, normal merge `e7a14ea1`; full 9595→9667 passed, both 78 skipped / 2 warnings / exit 0; direct harness 359/0 both; all CI green before merge; native migration/harvest sample passed | Merged with reviewed ancestry and identical tree; production host migration/activation remains separate. |
| 7a | P2-a1 telemetry intake / plan 4, Tasks 3–8 | BLOCKED: source verified; seven-day observation pending | Draft [#2364](https://github.com/Claudfather/Claudlobby/pull/2364), exact normal merge `13ceadfe` on P4 main `94088c2b`: fresh full 9780 passed, 78 skipped, four warnings, exit 0; affected 210 passed; fresh CLI canary passed. All 1374 source files/modes, interpreter and 33 dependencies match. Independent terminal review and all nine latest exact-head CI checks passed | Incoming P4's 23 cases are separately attributed; ancestral `15ea28db` is not a current-main full baseline. Complete floor-host RC/Telegram C6b week remains required before merge. |
| 8a | P2-a2 Plane rows/readers / plan 4, Task 9 | BLOCKED: dependent phase not implemented | Requires 7a week and measured token/coverage ratios, then its own implementation, checks and 48-hour observation | No Plane telemetry ingestion/readers delivered by 7a. |
| 9 | P1 Half B session joins / plan 2 | BLOCKED: literal clear passed; real native lifecycle and two timed windows absent | Draft [#2360](https://github.com/Claudfather/Claudlobby/pull/2360), unchanged `5f979881`; full 9667→9698 passed, both 78 skipped / 2 warnings / exit 0; direct harness 359/0; five native protocol arms and separate literal `/clear` replay passed; independent clear review NO FINDINGS | Real service/tmux/supervisor acceptance and full 48-hour BEFORE and AFTER remain. |
| 10 | P0 Codex batch / existing runbook | DONE: actual native measurements and independent evidence review converged | Codex CLI `0.162.0-alpha.17.2`; authorized private auth-only reuse; reviewed C1–C5/C7–C9 report and sanitized actual fixtures; Claude C3 separately reviewed NO FINDINGS; protected originals unchanged and owned jobs terminal | Measured answers unlock P4; no fleet activation or mixed-runtime acceptance claimed. |
| 11 | P4 owner adapters / measured per-repo plans | DONE: reviewed, verified, merged and required owner releases published | [Claudron #229](https://github.com/Claudfather/Claudron/pull/229), [release #230](https://github.com/Claudfather/Claudron/pull/230), [v0.10.0](https://github.com/Claudfather/Claudron/releases/tag/v0.10.0); [clauDNA #415](https://github.com/Claudfather/clauDNA/pull/415), [release #416](https://github.com/Claudfather/clauDNA/pull/416), [v0.29.0](https://github.com/Claudfather/clauDNA/releases/tag/v0.29.0); [Lobby #2366](https://github.com/Claudfather/Claudlobby/pull/2366), actual merge `94088c2b`. Independent reviews, actual artifact checks, exact-head CI and post-merge registry passed | Published owner adapters and fixture conformance; fleet composition/activation and mixed-runtime acceptance remain companion-owned. |
| 12 | Execution and behavior companions | SOURCE DELIVERED for behavior; execution and live acceptance blocked | [clauDNA #417](https://github.com/Claudfather/clauDNA/pull/417) merged as `3d6bfc60`; [release #418](https://github.com/Claudfather/clauDNA/pull/418) and [v0.30.0](https://github.com/Claudfather/clauDNA/releases/tag/v0.30.0) publish `4e4a5401`. Public native installation matches all 438 files/modes and discovers 36 skills and six untrusted hooks. Full 1F/2186P/9S versus baseline 1F/2134P/9S preserves the sole failure; Python 3.9.6 floor 944 passed. Catalog selection remains 17/18, exit 1. The [#2149 execution plan](2026-10-16-codex-execution-companion.md) and audit are reviewed; source still waits for accepted Half B | No native agent registration or global trust is claimed; the tested immutable-profile carrier failed and its replacement is undecided. #404 and #2149 remain open for actual execution and their complete mixed-runtime week. |

### Phase entries

**Export evidence carried forward:** at `0e6e0ea`, all eight GitHub verification checks pass; paid Claude routing evaluation is skipped. Local pinned `make check`: 2058 passed, 9 skipped, one pre-existing macOS sed failure (`tests/test_pretooluse_permissions_hook.py::TestWriteRedirectionKeepsThePrompt::test_dev_null_still_approves`), exit 2. Python 3.9 runtime: 875 passed, exit 0. Existing export suite: 63 passed before and after simplify. Offline canary: seven checks, 15 real shell-hook calls with synthetic payloads, zero model calls and unexpected errors. These results do not prove an authenticated host dispatch or a production rollout.

### Review rounds and class sweeps

Historical export review fixed retired-only exhaustion and unreadable-manifest gaps, then canonicalized the owned telemetry role. The actual local built-in simplify prompt ran through four independent Codex lenses in two waves: reuse and altitude clean; redundant archive scans and eager live projection reads fixed. A fresh exact-head convergence review follows under the operator's new review authority. Cross-repository class sweeps and final verdicts are appended per round below.

### Owner-decision and external-evidence queue

Code review and admin integration are delegated and authorized. P0 measurements, literal clear, 6a and both P4 owner releases are complete, with fresh registry checks. The behavior port and v0.30.0 are also delivered; telemetry exact-head CI and independent terminal review passed. Remaining work requires the concrete host/channel scope for live observations and the explicitly dependent source phases.

| Item | Smallest unblock | Required evidence / disposition |
|---|---|---|
| 6a host delivery and vault migration | Select one authorized canary host and vault clone; deliver the sealed reviewed release and its hash-pinned `[vault]` dependencies using the per-host [Claudron runbook](../integrations/claudron-integration.md) | Separately upgrade any host-managed Claudron CLI. From one clone run the explicit doctor fix, push/sync its commit, pull every peer and rebuild every local index; plan/diff with the exact delivered CLI and confirm the migration warning clears. The scratch-vault proof does not migrate production. |
| Telemetry integrated-head verification | DONE: all nine latest exact-head CI checks and independent terminal review passed for `13ceadfe` | Integrated full passed 9780 tests with 78 skips, four warnings and exit 0; all source/runtime checks match. Draft status and the complete seven-day live gate remain. |
| Telemetry week | Obtain a concrete independent floor host, root/unit/fleet labels, scoped RC/Telegram channel authority and dated activation schedule | Observe seven consecutive days, including day-1/day-7 delivered replies, hourly checks, coverage/token ratios, RSS/storage/latency and scrub evidence. No start/end or completed window exists yet; this remains a pre-merge gate. |
| Task 9 | After that week and its measured ratios, execute its own reviewed implementation plan | Plane rows/readers, affected/full/harness/native evidence and its own 48-hour observation; not part of the intake-only draft. |
| Half B live acceptance | Select an authorized independent live canary and schedule the complete BEFORE and AFTER windows | Literal `/clear` replay is now independently verified. Real service/tmux/supervisor acceptance and both full 48-hour windows still need UTC endpoints, restart causes and pulse/heartbeat evidence. |
| Codex batch | DONE: reviewed actual native report and redacted fixture provenance | The user explicitly authorized copying only existing authentication into private CODEX_HOME. Actual turns, measurements, sanitized fixture provenance and independent review are complete; source authentication/configuration remain unchanged. |
| P4 | DONE: owner releases and Lobby conformance integrated | Actual package/plugin checks, exact-head CI, reviewed-tree merge proofs and every post-merge registry passed; latest 39/197/944. Fleet activation remains companion/operator-owned. |
| Companions | Behavior source and v0.30.0 delivered; execution source waits for accepted Half B | #404 and #2149 stay open for the complete mixed-runtime acceptance week; unsupported capabilities stay explicit. No native plugin agent registration is presumed. |
| Production activation | Select a concrete staged host plan and scope deliberate migration/outbound channels | Operator-owned activation and rollout observations remain separate from merged code and local samples. No new production activation is recorded in this closeout. |

**Operator command templates, after selecting the host and scope:** use the exact sealed candidate CLI in that host's independent canary root, with its declared fleet, labels, Plane state and channels. Follow the [fleet update lifecycle](../fleet-update-lifecycle.md) for delivery and the [Claudron runbook](../integrations/claudron-integration.md) for the one-clone migration. Replace every placeholder with that authorized host's values before running:

```bash
claudron doctor --fix --json --vault <VAULT_CLONE>
# Push/sync the migration commit; pull every peer before rebuilding its index.
claudron index --full --json --vault <VAULT_CLONE>
<CANDIDATE_CLI> config plan --release <RELEASE_ID>
<CANDIDATE_CLI> config diff <PLAN_ID>
<CANDIDATE_CLI> host activate <PLAN_ID> --install-directory <NATIVE_UNIT_DIRECTORY>
```

These are manual templates, not executed commands or authorization for an unspecified production host. Missing authority or elapsed time never grants approval or waives a gate.

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


## 2026-10-16 UTC — Corrected candidate closeout

The epic goal remains unmet. The frozen full suites for `15ea28db`, `e644d07d` and `5f979881` are terminal and green; 6a is merged after successful exact-head CI, and the post-merge invariant registry is green. Telemetry’s subsequent test-only readiness correction has passing affected checks and fresh CI in progress; the older full/native observations remain attributed to `e644d07d`. Telemetry and Half B remain draft with mandatory live/timed gates open. Earlier dated red, partial, quarantine and observer-failure records are retained; those snapshots do not describe the current corrected heads. The [phase table](#phase-results) and epic §10.0b agree on DONE/BLOCKED dispositions.

### Delivered owner contract and consumer

- **7b DONE:** [clauDNA #411](https://github.com/Claudfather/clauDNA/pull/411) and [#412](https://github.com/Claudfather/clauDNA/pull/412) delivered v0.28.0; [Claudron #228](https://github.com/Claudfather/Claudron/pull/228) marks the released export contract's row 11 shipped. The follow-on strict reader in [clauDNA #413](https://github.com/Claudfather/clauDNA/pull/413) rejects malformed capabilities/resolve replies without consuming work as an empty result. [Release #414](https://github.com/Claudfather/clauDNA/pull/414) published [v0.28.1](https://github.com/Claudfather/clauDNA/releases/tag/v0.28.1) on 2026-10-16 at 00:21:44 UTC, tag target `5025af58273b2150bf479c745729bb62dab4228b`. The tested release candidate and published tag have identical trees; [release workflow](https://github.com/Claudfather/clauDNA/actions/runs/38829036126) is retained. Post-release invariants returned **197 passed** for owner activity/export/hooks and **39 passed** for Lobby identity/receipts/instructions, both exit 0. Final strict-reader local full qualification: **2097 passed, 9 skipped, one known pre-existing macOS sed failure, exit 2**, versus the earlier **2058 passed, 9 skipped, the same failure, exit 2** baseline; this is not a green local full run. The failure is `tests/test_pretooluse_permissions_hook.py::TestWriteRedirectionKeepsThePrompt::test_dev_null_still_approves`. Final runtime checks passed **914**, exit 0. All eight exact-head CI checks passed, and release #414 completed successfully. The dated 2084/901 checkpoint above describes the earlier candidate, not these final counts. Publishing the plugin does not prove a production host updated or restarted.
- **7c DONE:** [summary consumer #2361](https://github.com/Claudfather/Claudlobby/pull/2361) merged as `b4ae5dae` after its own before **9553 passed, 78 skipped, 2 warnings, exit 0** and after **9595 passed, 78 skipped, 2 warnings, exit 0**, empty failed/error names and +42 passes. Its direct harness and required Linux/contract jobs passed. Genuine mixed-batch/fallback mutants, actual armed/unarmed native SessionEnd, bot-context cache retention and sealed export/replay evidence remain the consumer's recorded proof. The old failing f3 export stays excluded from that final comparison. Consumer delivery does not claim production activation.

### Corrected 6a and child integration

The ten failures in the old frozen full runs came from existing standalone `_validate_bots` callers after the migration reader became a required keyword. The correction makes that seam optional, creates a reader only when none is supplied, and expands the existing per-vault/fresh-request test across public validation, standalone bots and an explicitly supplied shared reader. The host plan still creates one reader outside its fleet loop, and doctor shares its request reader. Scope is two files; no assertions or skip policy were weakened.

The exact red check was **10 failed, 29 passed, exit 1**; corrected focused checks were **190 passed, exit 0**. Four actual local builtin simplify lenses and an independent correction/integration read returned **NO FINDINGS**. This closes the reviewed source defect; it does not turn any older red full run green.

| Frozen run / head | Terminal outcome | Use in the final verdict |
|---|---|---|
| 6a before / `b4ae5dae` | **9595 passed, 78 skipped, 2 warnings, exit 0**; failed/error names empty | Completed before leg for corrected 6a. |
| Old 6a / `80c2d297` | **10 failed, 9655 passed, 78 skipped, 2 warnings, exit 1** | Retained red history; the ten helper-call names match the old telemetry run. |
| Old telemetry / `3e762644` | **10 failed, 9742 passed, 78 skipped, 2 warnings, exit 1** | Same ten scoped failures; no before-only/after-only failure names. Not final corrected evidence. |
| Obsolete Half B / `0a60a9eb` | Interrupted: **10 failed, 5209 passed, 65 skipped, 2 warnings, exit 2** | Partial SIGINT result excluded from terminal full comparison. |
| Corrected 6a / `15ea28db` | **9667 passed, 78 skipped, 2 warnings, exit 0**; failed/error names empty | Final paired result versus `b4ae5dae`: +72 passes; direct harness **359 passed / 0 failed, exit 0** in both legs. |
| Corrected telemetry / `e644d07d` | **9754 passed, 78 skipped, 2 warnings, exit 0**; failed/error names empty | Final paired result versus corrected `15ea28db` (9667/78/2, exit 0): +87 passes, no failing-name differences. |
| Corrected Half B / `5f979881` | **9698 passed, 78 skipped, 2 warnings, exit 0**; failed/error names empty | Final result versus corrected `15ea28db` (9667/78/2, exit 0): +31 passes; final direct harness **359 passed / 0 failed, exit 0**. |

Normal child merges preserve the published histories and the reviewed correction: telemetry `e644d07d` has parents `3e762644` and `15ea28db`; Half B `5f979881` has parents `0a60a9eb` and `15ea28db`. Independent Git tree comparison confirms exactly `claudlobby/validator.py` and `tests/test_claudron_loop.py` changed per child. All other **1366 telemetry** and **1356 Half B** blobs/modes are preserved. Telemetry retains its own validator registration; Half B's two corrected blobs match 6a exactly. The shared migration request budget and prior summary/session-join ownership are intact. Terminal source inventories preserve **1353 baseline / 1357 corrected 6a**, **1368 telemetry** and **1358 Half B** archived files with zero mismatches. The three full legs use the same Python 3.11.5 binary and **33** non-Lobby dependency versions. The actual full command is the prepared interpreter running `-m pytest --tb=no -ra`, with separate private HOME/TMPDIR; the direct harness command is `bash harness/validate-bot-change.sh`. Final 6a direct-harness pairing matches all 359 named results after normalizing 18 scratch-path differences. Half B preserves all 1358 source files/modes across both its final full and direct-harness runs.

Corrected Half B affected checks returned **640 passed, 6 optional-vault skips, exit 0**, failed/error names `[]`. Corrected telemetry affected checks returned **1 failed, 1308 passed, 6 skipped, exit 1**: the sole failure is the already-quarantined PAT bridge timeout [#2362](https://github.com/Claudfather/Claudlobby/issues/2362), whose runtime/test assertions/timeouts remain unchanged. Its narrow rerun returned **1 passed, exit 0**. Both affected results remain recorded; the narrow rerun is not a green affected/full verdict. The later final full suite independently passed 9754 tests, including this quarantined cell. Default local suites include quarantine; the marker assigns a separate CI lane and does not remove the test locally. Final corrected 6a and Half B direct-harness results above supersede the earlier-head harness result for those heads.

### Exact corrected-head observations and their limits

**6a `15ea28db`:** an independently sealed candidate with real released Claudron 0.9.0 and published clauDNA 0.28.1 planned against an actual old-release format-2 scratch vault with a legacy draft. It showed the named migration category and static remedy. Explicit operator-style m003 fix, normal local-clone propagation and both index rebuilds produced format 3/no pending; the same plan cleared the warning. A fresh native Claude Code 2.1.301 `--print` session used candidate-composed settings and fixed localhost Messages/SSE under a fake key. SessionEnd sealed a real summary; explicit forced harvest created one external-trust draft through capture/amend, advanced the cursor, and replay wrote nothing. **664** distributed source-backed files and **665** installed files matched the exact source/package. All software-under-test commands passed. The first read-only observer lacked a separately captured owner resolve and initially matched the deliberately seeded legacy draft; both failures were retained. A constrained real owner read let the unchanged verifier pass without repeating producer or harvest writes. Native service actions were simulated; cleanup left no owned process. No real provider inference, automatic next-SessionStart harvest, production rollout or timed acceptance is claimed.

**Telemetry `e644d07d`:** the fresh installed intake accepted sanitized live-captured native JSON logs and metrics with HTTP 200 and one complete raw line per body. Unsupported protobuf returned 415. Directory/file modes were 0700/0600; forbidden metadata keys were absent; SIGTERM and `plane doctor` exited 0, with no Plane dependency. This proves local protocol/storage behavior. It supplies neither the mandatory seven-day floor-host resource/coverage observation nor real RC/Telegram delivery. The #2362 quarantine tracks its unchanged timing cell; it is not a runtime fix or a reason to omit that test locally.

**Half B `5f979881`:** five fresh actual native CLI arms, real hooks/Bash/Agent and Plane readbacks passed with clauDNA 0.28.1 and sealed Claudron 0.9.0. Two sessions replayed one acceptance and retained the first uid; two more replayed one communication and retained its original uid. One linked native Agent child recorded one communication and progress event matching the independently read parent hook-store uid. All five CLI arms exited 0. Report/progress doors deliberately exited 5 after committed recording and failed notification because no manager process was running. **664** source-backed distributed files and **665** installed wheel files matched before/after. Fixed localhost responses and a fake key made **13** local Messages requests without real provider inference. There were no setup/observer failures or repeated arms in this corrected-head run; cleanup found no owned process. Native supervision alone was simulated. Literal interactive `/clear`, real service/tmux/supervisor acceptance and both complete 48-hour windows remain unexecuted.

All three corrected-head canary packages received an independent read-only procedure/evidence audit with **NO FINDINGS**. Real native CLI/hook/Bash/Agent, Plane and owner-command observations are distinguished from replayed captured telemetry. Fixed localhost fake-provider responses consumed no real model inference. NativeHost service actions were simulated, and refusing environment proxies do not prove OS-level egress confinement. Literal interactive `/clear`, real service/tmux/supervisor behavior, the seven-day and 48-hour windows, production rollout and provider/model quality remain unproven by these samples.

**Documentation claim convergence:** the final independent review found five active epic-issue claims that still prescribed the original Collector, hard plan refusal or old runtime naming. All five were corrected to the ratified direct `plane-otel` intake, named warning/doctor check, internal vendor→house mapping, `claudlobby.agent_cli` and `(agent_cli, session_id)`. The bounded pre-publication sweep covered eight Git snapshots across three repositories (2001 tracked Markdown instances), the two canonical drafts and the issue draft: **2004 document instances, 516 broad-pattern matching lines**. Current merged main and both child snapshots each had 101 matches: 55 dated history/spec quotes, 38 correct amendment/negation lines and 8 unrelated legitimate terms/refusals. The private canonical pair had 49 historical/spec and 25 correct current lines; the corrected issue had zero matches. The remaining matches were two unrelated clauDNA console-collector lines and 137 lines in the older, unrelated Lobby checkout. No obsolete active prescription remained in current delivery scope. Dated design text and unrelated checkouts were preserved. Independent follow-up returned **NO FINDINGS**.

### Integration and invariant evidence

**6a actual normal merge:** [#2363](https://github.com/Claudfather/Claudlobby/pull/2363) merged at **2026-10-16 01:48:40 UTC** as `e7a14ea1d158eb272566f81a3e2c2ad1bb459ae7`, with parents `b4ae5dae86e7f8b62894dd8a500e24e2201cd17d` and reviewed `15ea28db9b03e93eb635a5ab0c17e9ce931c320a`. Merge/candidate trees match `06daa0c1797ae80f815c1eaa36c28d620a36f23b`; the reviewed head is an ancestor. No force push or branch deletion occurred. [Linux pytest, harness and both supported-platform jobs](https://github.com/Claudfather/Claudlobby/actions/runs/38832586770), [vault/export/rename conformance](https://github.com/Claudfather/Claudlobby/actions/runs/38832586933), [quarantine](https://github.com/Claudfather/Claudlobby/actions/runs/38832586824) and [rollout check](https://github.com/Claudfather/Claudlobby/actions/runs/38837033320) all completed successfully before merge. This is source integration, not production activation.

**Child topology and CI readback — 2026-10-16 02:01:58 UTC:** [telemetry #2364](https://github.com/Claudfather/Claudlobby/pull/2364) and [Half B #2360](https://github.com/Claudfather/Claudlobby/pull/2360) are retargeted from `codex/2145-claudron-pin` at `15ea28db` to main at `e7a14ea1`; both remain OPEN/DRAFT. Reviewed `15ea28db` remains their merge base and an ancestor of actual main. Exact head/tree identities and all content stayed unchanged: telemetry `e644d07d03231320ae465812454bf6ac5ebcc104` / `fa0830479126ec982ce35dcac940f216a5911a3e`, Half B `5f9798810034209a21c6f84ffebd857184bf34e6` / `b290c4ba8da242dde49797ee4ad79745d0f1faa5`. Child-only scope remains **48 telemetry** and **28 Half B** paths; exact PR bodies were read back. No source/code change, push, rebase, merge or cancellation request occurred in the metadata closeout.

Half B's latest [Linux, Ubuntu/Python 3.10, macOS/Python 3.11 and harness checks](https://github.com/Claudfather/Claudlobby/actions/runs/38833735626), [vault/export/rename conformance](https://github.com/Claudfather/Claudlobby/actions/runs/38833735463), [quarantine](https://github.com/Claudfather/Claudlobby/actions/runs/38833735471) and [post-retarget rollout check](https://github.com/Claudfather/Claudlobby/actions/runs/38838083519) all succeeded. Telemetry's [supported macOS/Python 3.11 job](https://github.com/Claudfather/Claudlobby/actions/runs/38833549968/job/116757908848) **failed**; all other latest [Linux, Ubuntu/Python 3.10 and harness checks](https://github.com/Claudfather/Claudlobby/actions/runs/38833549968), [vault/export/rename conformance](https://github.com/Claudfather/Claudlobby/actions/runs/38833549912), [quarantine](https://github.com/Claudfather/Claudlobby/actions/runs/38833549930) and [post-retarget rollout check](https://github.com/Claudfather/Claudlobby/actions/runs/38838057559) succeeded. That failed macOS result remains historical evidence: its sole failure was the five-second startup port-file wait, before incomplete headers or SIGTERM were exercised; the underlying startup delay cause is unproven. A green local full run does not replace that result. Earlier retarget-triggered rollout checks are retained as cancelled, with the subsequent latest checks successful. Both PRs retain their mandatory empirical gates regardless of retarget or CI status.

**Telemetry test-only follow-up:** current public head `56a59f9faa646cf8f201f5df6d7abe0a7a0433f5`, tree `aaaa81f54afde8c8b514fc32f8743361a13c73eb`, changes only `tests/test_plane_otel_intake.py`; all **1367** other tracked files/modes match `e644d07d`. The real six-second startup control reproduced the old readiness failure (**1 failed, 1 passed, exit 1**). Separate 20-second startup and five-second handler waits produced **46 passed, exit 0**, while the original three-second SIGTERM drain, success assertions and cleanup remain byte-identical. Removing the accepted-socket header timeout still failed both arms at that original drain check (**2 failed, exit 1**). An early-exit control produced bounded, scratch-redacted stderr (**1 failed, exit 1**). Independent source/procedure review returned **NO FINDINGS**. This corrects a test readiness budget; it does not establish the underlying CI delay cause or change production behavior. The [fresh exact-head Linux/platform/harness workflow](https://github.com/Claudfather/Claudlobby/actions/runs/38839339370) is running; current vault/export/rename, quarantine and rollout checks have passed. No full-suite success is assigned to this new head from the earlier frozen `e644d07d` run. Both the prior failure and new CI remain separate; the seven-day gate is unchanged.

**Startup-readiness class sweep:** five recorded snapshots across three repositories (local `origin/main` refs plus telemetry/Half B heads; no claim of remote freshness) were searched with `git grep` for deadline assignments, readiness-marker existence and `wait`/`communicate` timeouts, plus an AST count of actual subprocess calls. Raw matching-line counts for those three patterns were Lobby **69/21/63**, Claudron **0/7/3**, clauDNA **0/3/0**, telemetry **71/22/66** and Half B **69/21/63**; repeated main/child matches are not unique defects. Inspection covered **33** stored relevant deadline contexts and all **8** telemetry literal five-second deadlines. It found **one unique evidenced fixture defect, already corrected; zero new siblings**. Other waits separate readiness from behavior, govern in-process recovery or implement an independent measurement contract; no other timeout or source was changed.

**Post-6a invariant registry:** Lobby identity/receipts/instructions returned **39 passed**; released clauDNA activity/export/hooks returned **197 passed**; `make test-runtime` returned **914 passed**. Every command exited 0, scoped failed/error names are empty, and before/after checks preserve **1357 Lobby** and **401 owner** files/modes with zero mismatches. Lobby imports resolve to the prepared reviewed `15ea28db` tree, identical to the actual normal merge. Owner imports resolve to released v0.28.1 `5025af58` under the existing **Python 3.12.13** interpreter; this runtime check is not a new Python 3.9 floor claim. No installs, source edits, production activation or live/timed observations occurred in this invariant checkpoint.

The [owner queue](#owner-decision-and-external-evidence-queue) contains corrected-head telemetry verification, concrete host/channel scope, timed observations and dependent implementation. Private authentication and native measurements are resolved. Merged software, source equality and green local/CI checks do not close those gates.

### Goal verdict and remaining work

**Goal NOT MET.** Remaining acceptance gaps are the seven-day floor-host RC/Telegram canary, Task 9 implementation and its own 48-hour observation, Half B real native lifecycle and both complete 48-hour windows, companion implementation, and the mixed-runtime week. P4 owner adapters are released and Lobby conformance is integrated, with actual artifact checks and fresh post-merge invariants. The queue above gives each smallest unblock. Delegated review and authorized admin integration waive no runtime gate. Host migration, activation and outbound channel use require a concrete operator-selected scope.


## 2026-10-16 UTC — Authorized native measurements and P4 continuation

This entry supersedes the authentication and literal-clear blockers in the earlier dated closeout. The operator explicitly authorized private reuse of existing Codex authentication and the local fake-provider Claude software canary. Source review and eligible admin integration remain delegated; live host/channel authority and elapsed observation gates remain separate.

### Half B literal clear

At unchanged `5f979881`, actual Claude Code 2.1.301 with released clauDNA 0.28.1 and sealed Claudron 0.9.0 ran literal interactive `/clear` in an independent private canary. Before clear, one acceptance and one progress report committed. After clear, the native hook store had a distinct session/derived UID; replaying the identical request IDs returned replayed receipts, kept exactly one accepted event and one communication under the first UID, and left the second UID lookup empty. Format-1 intent omitted session attribution. Both sessions closed; CLI exit 0; all 664 source-backed/665 installed files matched bytes/modes. Expected report-door exit 5 means recording committed while manager notification was unavailable.

Six fixed localhost Messages responses (two Bash replies) and a fake key exercised the actual native CLI, hooks, doors and Plane. Twenty-six proxy refusals prove proxy-aware refusal, not OS-level network confinement. The first authorized arm exposed only a private canned-response scheduling error, invoked no receipt door and did not run clear; it remains separate from the corrected pass. Independent inspection of the receipts, hooks, rows, lookups and package proof returned **NO FINDINGS**. The PR body was updated and read back exactly with unchanged head and draft state. Real service/supervision and both full 48-hour windows are still absent.

### P0 native Codex observations (independent review: NO FINDINGS)

Installed SUT: **Codex 0.162.0-alpha.17.2**. The unmodified main shape-only harness SHA256 is `d84982af190ce84292456744a678cb0d007671500d1fccd03bb291c0008afbec`. Measurements use private HOME/CODEX_HOME, no daemon and explicitly authorized auth-only reuse. The authenticated smoke used `-s read-only`; interactive sessions used `-a never -s workspace-write` in the private scratch root. Native `permission_mode=bypassPermissions` is recorded separately and does not establish filesystem sandbox authority. Analytics was disabled and metrics exporter set to none before the first turn. The authenticated strict-config smoke exited 0. Original auth/config/Claude settings hashes and mtimes remained unchanged at batch cleanup; the additional sanitized-fixture arm is separately checked before final review.

| Canary | Actual observation | Consequence / limit |
|---|---|---|
| C1 | Native stdin emitted SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd, PreCompact, PostCompact and Interrupt. Common keys include `session_id`, `transcript_path`, `cwd`, `hook_event_name`; observed tool name `Bash`, `tool_input.command` string and PostToolUse `tool_response` empty string. UUID session IDs match the existing SID grammar. | Use captured shapes, never producer-source reconstructions. Non-null transcript paths in observed arms do not prove universal non-nullability. No PostToolUseFailure capture is claimed. |
| C1 lifecycle budget | SessionEnd 800ms completed (~0.84s); 2500ms did not complete with the native default 1s; explicit timeout 3s completed (~2.54s). | Composition must declare the budget it needs; no default-3s assumption. |
| C2 | Hook environment has CODEX_HOME but neither CODEX_SESSION_ID nor CODEX_THREAD_ID. Actual tool shell has both IDs, equal to the hook UUID. Native owner ancestry is stable `codex → tmux → launchd`, Codex one level above the hook. | Owner PID must come from a bounded own-ancestry walk, not an assumed hook PID env or unrelated process-table search. |
| C3 Codex | Startup stdout marker matched the actual model reply. Successful compact emitted PreCompact/PostCompact; the later SessionStart(source=compact) marker replaced the old marker in the reply. `continue:false` blocked the second manual compact (two PreCompact, one PostCompact). | Injection and manual block work in this installed version. A sandbox-limited tmux observer read initially failed; the owned CLI stayed alive and compaction succeeded. |
| C4 | Native rollout is append-only across exact-ID resume: 162197→181134 bytes, full previous prefix hash unchanged. Filename UUID matches hook ID. Types include session_meta, event_msg, response_item, world_state, turn_context, token_usage_record, compacted; records carry ordinal. Token usage is present in token_usage_record payload; actual tool calls use custom_tool_call/custom_tool_call_output records. | Reader dispatch must use recorded agent_cli, avoid duplicate event_msg/response_item text and keep tool/system/context content out of summaries. Golden excerpt will preserve actual shapes with content redacted. |
| C5 | Twenty-eight HTTP-JSON log requests reached `/v1/logs`; zero metrics/traces requests were observed with metrics disabled. Configured environment carrier is resource `env`, not `deployment.environment`. Foreground `conversation.id` equals hook UUID. An additional OTel conversation has no hook or rollout, and both API log records lack an ID (two of 84 total records). | Do not infer global one-to-one attribution or assign the unmatched ID to the bot session. Peer snapshots show loopback and model-host DNS-matching peers; they are observations, not exhaustive egress enforcement. |
| C7 | Native `/status` executed; idle glyph `›`; both busy Enter submission and explicit Tab queue executed after the busy tool. Owned tmux kill exited the CLI, and exact UUID resume executed a recovery turn. | This is private tmux behavior, not fleet service/supervisor acceptance. |
| C8 | Native local marketplace/plugin installation and packaged hook execution worked. PLUGIN_ROOT/PLUGIN_DATA and CLAUDE_PLUGIN_ROOT/CLAUDE_PLUGIN_DATA are all set, root/data aliases equal and inside the private installation/home. Public clauDNA v0.28.1 marketplace fetch exited 0; plugin install exited 1: `plugin claudna was not found in marketplace Claudfather`. | A Codex manifest is still required. No released clauDNA hooks were installed, trusted or executed by that failed attempt. Plugin install and hook trust are separate. |
| C9 | The second SessionStart command populated its own selected log; native trust-all persisted the private hook hashes across restart. | Explicit hook-command selection is viable. Global trust/config was untouched. |

### Claude C3 request-delivery evidence (independent review: NO FINDINGS)

A separate native 2.1.301 fixed-localhost-provider arm used fake credentials and no Anthropic inference. Manual PreCompact `decision:block` stopped the compact request and showed its throwaway reason; the reason reached a later explicitly submitted main request, not an unattended follow-up. Successful compaction after disabling only that private test block emitted SessionStart(source=compact); its stdout marker appeared in the next main request and the old reason did not. Fixed responses deliberately omitted both markers, so these observations prove delivery into requests, not semantic uptake by a real Claude model.

A bounded automatic arm used the native supported `--autocompact 100k` with explicitly synthetic usage counters to trigger an actual PreCompact with `trigger:auto`. The worker observed a subsequent native main request without further user input and without either marker, with no SessionStart(compact). Independent review checked the chronology, cleanup and configured fixed print branch/hash and returned **NO FINDINGS**. The auto hook preceded the same-turn main request by 10.544 ms. Actual stdout bytes were not separately captured. The result supports Claudron#179's planned conditional owner-contract correction for unreliable unattended block-reason delivery; successful SessionStart(compact) delivery was measured on the manual path only. These are controlled trigger measurements, not real usage/token behavior.

A post-manual-run privacy walker followed a native-created URL-handler symlink and temporarily removed the installed Claude binary's execute bits. The retained-path audit found one external target; correction-time bytes were unchanged. Standard npm executable mode 0755 and version operation were restored before the auto arm; no pre-run mode capture exists, so exact original-mode proof is not claimed. Future walkers use lstat, skip symlinks and require resolved private descendants. This observer mistake does not change the earlier native request observations.

### Measured P4 planning premises

The measured common C1 fields already match Lobby's two hook readers. The Lobby phase first proves conformance using redacted actual stdin; it adds runtime normalization only if a captured field requires it. Main still emits legacy `tool_call` rows: #2173 explicitly retained them under Q17 until P2-a2 names tools from OTel. The old shorthand “marker-only P2-b” is not evidence that removal shipped. P4 must preserve that deferred retirement boundary.

The clauDNA phase must resolve the design-era layer mismatch: rank-5 detached readers cannot import rank-6 host adapters, and strict downward imports forbid peer host type imports. Keep shared types/reader dispatch below consumers or pass callbacks; do not relax the layering gate. Owner PID feeds the child guard, clear links and abandoned sweep, so a Codex hook with no PID env must not record None by default when its own ancestry is available. The summarizer runner remains Claude under locked F10.

At this earlier premise checkpoint, no owner release or live/timed completion was claimed. Later entries below supersede its owner-release state; live floor-host, Half B, Task 9 and mixed-runtime gates remain named.

**Final P0 artifacts:** the authorized `rnc-results-4.md` report and private copy match SHA256 `4d13c914c1227807d736293573b24e10ac9f055a504f298c6955288369bc9cb7`. Eleven native hook/source fixtures plus a 17-record actual-rollout golden excerpt are redacted before export; manifest SHA256 `8ca604533fe6d6f509d75a832938f422fac3aa8c2512db95592f97b07eae8c2a`, golden `85dc581b6c525be0412dff6713f3cc8e9b45e7ec09cdc7108fa07a08b245164c`. Ten tracked native owner PIDs, receiver and private tmux are gone. Protected originals match hashes/modes/sizes/mtimes; retained private auth is 0600. The fixture driver's initial early compact submission produced no compact observation; its separate bounded idle correction captured actual PreCompact/PostCompact/SessionStart(compact), then closed normally.


### P4 Lobby conformance — local verification

The measured per-PR [Lobby plan](2026-10-16-runtime-neutral-p4-lobby-hook-conformance.md) has no runtime edit. Three actual native stdin fixtures were redacted before write, with original/sanitized hashes and a redaction recipe. Existing tests now exercise the captured UserPromptSubmit envelope across sender-wire shapes and both JSON encodings, ordinary-prompt no-row control, and Pre/PostToolUse marker/event/no-BOT_DIR paths. In separate prepared exports with Python 3.11.5 and identical pinned dependencies, affected command `.venv/bin/python -m pytest --tb=no -ra tests/test_plane_dispatch_in.py tests/test_plane_cutover_keepalive.py` returned **60 passed / exit 0** before and **83 passed / exit 0** after. Failed/error names are empty both; **+23 passes**. Source bytes/modes are unchanged through each run (1357/1363 paths); every one of the **723 non-test/non-documentation paths** matches across legs. No broader new-head full-suite result or live fleet activation is inferred. Independent simplify/spec/evidence review and CI remain pending.

**P0 review correction:** final C5 count is 84 records across 28 JSON requests, with two API records and both missing conversation.id. Independent review and root recount agreed; both report copies and this ledger are corrected. The original report/cleanup snapshot is retained privately, and no measurement was rerun.


### P0 and Lobby review convergence

Independent review covered every native fixture/transform, the actual-rollout golden, C1–C5/C7–C9 records, protected originals, cleanup, both complete changed test modules, phase plan and paired raw test artifacts. Two findings closed: (1) the newly authored stronger “read-only operation” claim was corrected to read-only smoke versus interactive workspace-write, with native permission enum kept separate; (2) final C5 missing-ID API count was corrected from one to two of 84 records in both current report copies and the canonical ledger. The pre-review report is preserved privately. Source-wide claim checks and exact report readback closed the class; owner plans received the corrected report hash/count. No source finding remained. Reuse, simplification, efficiency and altitude lenses returned **NO FINDINGS**. The final reviewed report SHA is `4d13c914c1227807d736293573b24e10ac9f055a504f298c6955288369bc9cb7`.

Lobby receipt/vitals conformance review also returned **NO FINDINGS**, with all three published fixtures byte-identical to their sanitized native captures and +23 focused cases independently verified. Exact-head CI remains a separate integration gate. Claudron's owner adapter is now review-ready locally (full 856→887 passed / exit 0; affected 146 passed / exit 0; ten actual owner CLI/local-Git arms passed); clauDNA's owner adapter is in implementation. No unmerged owner surface is claimed as released or live.


### P4 Claudron owner — integrated, release pending

[Claudron #229](https://github.com/Claudfather/Claudron/pull/229) admin-squash-merged as `3ddaf9c1d2ca9e13c2ba2a783f25070196296e3e` after both exact-head CI jobs (full and live consumer contract) passed. Actual merge tree `d0f5a47285fd84eef23988153183763c6a2bea3a` equals reviewed `fc421eae`. Both independent source/simplify and spec/verification reviewers returned **NO FINDINGS** after fixing the one native-budget defect. Fresh corrected evidence: full baseline **856 passed / exit 0** → candidate **893 passed / exit 0**; affected **152 passed / exit 0**; ten actual owner CLI/local-Git arms passed. Native trust, host timeout enforcement, whole-sync completion within three seconds and fleet acceptance remain unclaimed. No owner release or production activation exists yet.

The cross-phase registry ran after this merge using proven current-published Lobby `284e7f2d` and clauDNA `5025af58` sources with fresh private HOME/TMPDIR: **39** identity/receipt/instruction, **197** activity/export/hook and **914** runtime checks passed, all exit 0; source bytes/Git executable modes remained unchanged. The first private runner stopped on a missing helper before any test ran; the corrected runner's complete results remain separate. Owner release preparation follows; consumer composition must wait for the actual versioned surface.

### P4 owner integration and release verification — 2026-10-16 UTC

Claudron's v0.10.0 metadata PR [#230](https://github.com/Claudfather/Claudron/pull/230) merged as `b1039d148449655d4c0981fa1cc1c364e89148c7`, tree-identical to reviewed `44ad0f7d`. Both CI jobs passed before merge. The existing [release workflow](https://github.com/Claudfather/Claudron/actions/runs/38853336918) completed successfully at that exact merge. The published [v0.10.0](https://github.com/Claudfather/Claudron/releases/tag/v0.10.0) tag peels to the merge; its downloaded wheel SHA256 is `059c1fb0669a194889098391c9d5771ab69df947ee9ad390c62d6f91214e1393`. All 21 packaged Python files match merged source. A private install of that actual published wheel returned version 0.10.0 and the `codex-session-loop` contract capability, with no provider calls. The local smoke wheel has a different archive hash; it is not substituted for published-package proof. The cross-phase registry ran after the metadata merge: **39 / 197 / 914 passed**, all exit 0, source bytes and Git executable modes unchanged.

clauDNA's adapter PR [#415](https://github.com/Claudfather/clauDNA/pull/415) merged as `82d9ceae0f9602f792e6005f3c2b397cbfa0a2cf` after all nine exact-head CI checks passed. Actual tree `729f63879e61f50b09cfbddf6931fcc662623205` equals independently reviewed `97448bc2`. Review converged after the native rollout reader was restricted to positively classified user text and measured assistant phases: the original 17-record excerpt lost noncontent classification enums, so the final 20-record excerpt preserves both genuine-user and injected-environment shapes and was independently reproduced from the same native capture. Original P0 artifacts remain unchanged. Fresh full Python 3.12 comparison was **1 failed / 2097 passed / 9 skipped** before and **1 failed / 2134 passed / 9 skipped** after, make exit 2 both; the same sole macOS permissions-test name failed, with no new failures. Python 3.9 runtime checks increased **914→944 passed**, exit 0 both. The native plugin discovery correction proved 39 default-discovered skills became zero through explicit inert paths; the installed API exposed no agent-list surface, so native zero-agent enumeration is not claimed.

The post-#415 registry used the actual new published owner source, not the prior release: **39** identity/receipt/instruction, **197** activity/export/hook and **944** runtime checks passed, all exit 0; imports and unchanged tracked source bytes/Git executable modes were checked. F8's activity module remains byte-identical and F10's Claude summarizer runner remains fixed. A v0.29.0 release is being prepared; neither owner source integration nor private packaging tests activate a fleet.

### Telemetry startup cause and narrow correction — 2026-10-16 UTC

The exact `9d998d94` macOS [CI job](https://github.com/Claudfather/Claudlobby/actions/runs/38848932953/job/116805613947) finished **2 failed / 9753 passed / 77 skipped / 1 deselected / exit 1**. Both child traces stop in `socket.getfqdn → HTTPServer.server_bind` after TCP bind but before activation/listening and port publication. This identifies the blocking call; the underlying operating-system resolver cause remains unknown. HTTP and SIGTERM drain were never reached in those two failures.

Reviewed correction `606e8798` bypasses only the unused reverse-DNS lookup by calling the existing TCP bind and retaining the bound numeric host/port. Socket reuse, the public IPv4-only admission policy, privacy, handler/drain behavior and **20s startup / 5s handler / 3s drain** budgets are preserved. Both independent correctness/simplify and spec/evidence reviewers returned **NO FINDINGS**. Focused RED **2 failed / 125 passed** became GREEN **127 passed**, exit 0. The retained forced-blocked-resolver control fails on the old path before listening and passes on the fix in 0.36 seconds; the broken-drain mutant still fails both cases at the unchanged three-second bound. The scoped class sweep includes current sprint repository mains and unmerged branches; other HTTP fixtures have distinct contracts and no observed equivalent startup failure.

The real CLI canary accepted captured logs and metrics (200), refused protobuf (415), preserved private files/directories (0600/0700), exited on SIGTERM with 0 and passed doctor with 0. Intake worked without an initialized Plane database; doctor later created an empty state/plane directory, so no claim is made that the directory stayed absent. Exact candidate archive/Git/prepared export match across 1368 files; the reused original `15ea28db` baseline matches across 1357 files, with identical interpreter hash and 33 installed dependency versions. The fresh committed-head full suite is still running and is not claimed complete here. Draft #2364 also retains its mandatory independent floor-host seven-day observation gate.

### P4 clauDNA v0.29.0 — published and independently verified

[Release PR #416](https://github.com/Claudfather/clauDNA/pull/416) passed all nine exact-head CI jobs and admin-squash-merged as `3c56fb6c6b3e01546ee264e684a383d741a0a497`, tree `33244925ae8a541eb0520612880506d48c99115f`, identical to reviewed `f52ecd69`. The [existing release workflow](https://github.com/Claudfather/clauDNA/actions/runs/38854929746) succeeded at that merge. Published [v0.29.0](https://github.com/Claudfather/clauDNA/releases/tag/v0.29.0) was read back, and a fresh tag fetch peels to that exact commit/tree.

The release review identified stale two-manifest contributor instructions. Its class sweep covered current mains and relevant unmerged sprint heads across all three repositories: eight current stale claim lines on the owner main were corrected through contributor/release/distribution guidance, the PR checklist and validator/test headers; zero unresolved current claims remain in the release tree. Five historical owner changelog lines and the Lobby historical 0.26/0.27/0.28 facts remain historical. Three manifests differ only in version, and both changed Python module ASTs are identical after removing module docstrings; the eight intended changed paths leave the other 417 tracked paths unchanged. Local full check retained **1 failed / 2134 passed / 9 skipped / make exit 2**, with the same known macOS permissions-test name and no new failures. The final manifest check passed. Independent corrected source/simplify/spec review returned **NO FINDINGS**.

The private native packaging check publicly fetched the actual published tag, exported that exact tree and installed it through a private local marketplace. Native Codex `0.162.0-alpha.17.2` returned installed version 0.29.0, zero plugin skills through both plugin/read and skills/list, and exactly five untrusted store hooks with SessionEnd timeout three seconds. All **425** Git/archive/source/cache paths match in bytes and Git executable modes; source-before/after maps are identical. The agent path is explicitly inert and statically empty, with no native agent-count API claim. Ten command receipts and the owned app-server normal stdin-close/wait all exited 0. No auth copy, model turn, hook trust grant, source/global configuration mutation or hook execution occurred. The lstat privacy walk followed no symlink. Independent evidence review returned **NO FINDINGS**. This proves published-tag local-marketplace installation/discovery, not remote marketplace ingestion or fleet acceptance.

The registry ran after this release merge on proven actual owner source: **39** Lobby identity/receipt/instruction, **197** owner activity/export/hook and **944** owner runtime checks passed, all exit 0, with imports and source stability checked. This refreshed runtime leg uses Python 3.12.13; the separate actual Python 3.9 floor run and successful CI floor job remain distinct evidence. Both P4 owner releases are now available for the execution companion; no host activation or mixed-runtime week is inferred.

### P4 complete — Lobby conformance integrated

[Lobby #2366](https://github.com/Claudfather/Claudlobby/pull/2366) passed every latest exact-head CI check: main pytest, Ubuntu/Python 3.10, macOS/Python 3.11, harness, vault/export/rename/quarantine and the corrected rollout check. The earlier rollout-body failure is retained; its newer check passed. After readiness, the authorized admin squash produced `94088c2bdf61c9daa72561e3d34ef6a28383068a`, tree `588f69d933634641a93efa9029a851ce1d0b3019`, identical to reviewed `f01b5fc1`. The first merge attempt was refused because the PR was still draft; it made no merge. No force push or source rewrite occurred.

The post-merge registry used a fresh history-free export of the actual merge, its own matching pinned venv, an index created before install, one resource preparation and private HOME/TMPDIR. This avoids assigning the earlier export's three pre-finalization document bytes to the merge. All 1363 actual Lobby files and 425 actual released owner files match source before/after; imports resolve to those proven roots. **39** identity/receipt/instruction, **197** owner activity/export/hook and **944** owner runtime checks passed, all exit 0. The runtime leg uses Python 3.12.13; separate Python 3.9 evidence remains distinct.

P4 is **DONE for its declared fixture-tested owner-adapter and Lobby conformance scope**: Claudron v0.10.0 and clauDNA v0.29.0 are published and checked, and Lobby's measured existing hooks need no runtime edit. This does not claim Codex fleet composition, native supervision, production activation or mixed-runtime acceptance. Those belong to the companions. clauDNA#404's counted plan has completed its first challenge round; four acceptance/provenance gaps are being corrected and independently re-reviewed before implementation. Source-independent telemetry verification continues; its seven-day and the other timed gates remain binding.

### Behavior companion #404 — plan convergence and implementation started

The measured audit covers **39 skills, eight agent prompts and 12 hook-tree files**, with 194 source-file hashes pinned to P4 source. Codex's selected catalog is **36 existing skills** (16 portable, 20 adapted); only `cleanup-legacy-install`, `promotion-intake` and `skill-scaffold` are excluded from Codex discovery. Claude's 39 and Cursor's 35 remain unchanged. Two selected-to-excluded references receive explicit source-checkout guidance. All eight bundled agent prompts are mapped to a read-and-dispatch procedure. A route is supported only on execution hosts where actual same-host controls and the required fixture proof pass; unsupported routes remain explicit. No native plugin-agent registration, Claude model alias or background/memory enforcement is inferred.

Independent pre-implementation challenge found four concrete gaps: reviewer dispatch lacked a host-qualified receipt and result oracle; all native skill probes were explicit and could not measure automatic routing; startup execution was optional despite mandatory acceptance; public promotion lacked a locator/privacy gate. The revision adds an undisclosed known-bug fixture and genuine same-host spawn/wait/finding proof, an unhinted normal-catalog native prompt separate from structured catalog selection, mandatory packaged startup execution/context delivery, and sanitized logical evidence labels with regenerated exact-byte receipts. Every related claim context was swept; originals remain private. Corrected plan SHA256 `ed3dca43c5630b4ee3092297cdbc8097c0870922b6c9ba53e6e3de6deb3f78b2` independently converged **NO FINDINGS** before source edits.

Implementation starts on a fresh branch from actual v0.29.0 main `3c56fb6c`, preserving the release's three-manifest documentation corrections. Separate history-free baseline/private HOME/TMPDIR evidence records **1 unchanged baseline failure / 2134 passed / 9 skipped / make exit 2** under Python 3.12.13 and **944 passed / exit 0** on actual Python 3.9.6; all 425 source files remain unchanged. The baseline failure is the known macOS permissions test, not a new regression. New scope checks reproduced their expected RED cases before validator implementation. Source implementation, fresh after evidence, actual Codex routing/action proofs and independent implementation reviews remain in progress; no behavior release or mixed-fleet acceptance is claimed.

The #2149 execution-companion audit/plan is separately underway against actual Lobby main `94088c2b`, incorporating released owner contracts and measured native capabilities. Half B's real service/two-window gates and concrete live host/channel scope remain dependencies. No running fleet, authentication policy or unobserved native permission mechanism is assumed from the existence of a source adapter.


### 2026-10-16 UTC — DNS correction full result and exact P4 integration

The full default suite at DNS correction `606e8798342b31861dfdd14226ffcc1e45435b83` completed at 05:36:58 UTC: **9757 passed, 78 skipped, four warnings, exit 0**, with empty failed/error names. The separately prepared ancestral `15ea28db` baseline had 9667 passes, 78 skips, two warnings and exit 0. Both used the same Python 3.11.5 executable and 33 dependency versions. All 1368 candidate worktree and prepared-export files retained their exact bytes and Git modes after the run. The two additional warnings are JUnit `record_property` warnings in unchanged tests; no warning was suppressed. Independent terminal-evidence review found no material findings. Earlier failed macOS runs remain failed evidence at their original heads.

Local normal merge `13ceadfe72706ec83832b89643cf9ba5e66ad5a9` (tree `c78d4afd06c13e5ee64f30dd51314e8ca8004c0b`) then incorporated actual P4 main `94088c2b` with zero conflicts or manual resolutions. Exactly ten incoming P4 documentation/test/fixture paths equal that main; 1364 other paths are preserved. The fresh prepared export matches all 1374 source files and uses the same interpreter/dependency inventory. Its affected union passed **210 checks, exit 0**. P4 collection is explicitly reconciled from 60 to 83 cases, a separate increase of 23; the ancestral baseline is not presented as a full result for current main.

The integrated actual CLI canary again returned logs/metrics 200 and protobuf 415, retained metadata exclusions and private directory/file modes, and shut down on SIGTERM with exit 0. Independent integration/spec/evidence review found no material findings. **This candidate's own full default suite started at 05:46:04 UTC and remains in progress.** The prior 9757-pass result belongs only to `606e8798`. Publication and fresh exact-head CI follow the terminal result; PR #2364 remains draft for the complete seven-day floor-host gate.


### 2026-10-16 UTC — execution-companion plan converged; source dependency retained

The [#2149 execution plan](2026-10-16-codex-execution-companion.md) is now promoted with a [sanitized planning receipt](2026-10-16-codex-execution-companion-audit.json). The counted audit covers 170 selected source/documentation files, including 32 core files and all 55 fleet-skill directories (78 tracked files), pinned to actual Lobby P4 main `94088c2b`. This is a targeted planning audit, not a full-repository correctness verdict.

Independent review found one stale evidence class: five planning artifacts still listed the literal native Claude `/clear` test as unperformed. The author corrected all five plus their generator, regenerated receipts, and preserved the previous revision. Re-review covered all nine changed private paths and returned **NO FINDINGS**. The accepted test remains bounded to Half B `5f979881`, native Claude 2.1.301, clauDNA 0.28.1 and Claudron 0.9.0 with a fixed localhost fake provider and no real Claude inference. It does not establish Codex `/clear` support. The promoted plan bytes have SHA256 `8b0c93a3982470079b6e87ba1225b70ef8f85f8178fa7a353a217afbb6fa489e`; public assets use logical evidence labels and hashes, without private absolute paths, native identifiers or raw payloads.

The selected first topology is one channel-less Codex worker with coordinator relay and canonical assignment/report doors. Native policy enforcement, interactive configuration profiles, actual composed AGENTS loading, own-session identity/restart, released #404 behavior and Claudron capability parity remain explicit prerequisites. **Source implementation remains blocked on real Half B service/supervision acceptance, its full 48-hour BEFORE and AFTER windows, and its accepted merge.** The separate telemetry week and complete mixed-fleet week remain unwaived. No source implementation, production auth provisioning, live activation or elapsed-window result is claimed by this planning phase.


### Review round — qualify dispatch claims by execution host

Independent review of all five current closeout documents found one overbroad dispatch-support sentence. A counted sweep across eight current/main/candidate snapshots in the three repositories inspected 6603 tracked or intended source files and returned 14 matching lines. Two were current unqualified support claims: the ledger sentence is corrected here, and its CONTRIBUTING sibling is assigned to the behavior owner for the next source revision. The other 12 are counted inventories, already qualified dependency prose or unrelated queue-weight examples. The complete matching-line inventory is retained privately.

The supported contract maps the eight bundled role prompts to a procedure; it does not prove every native host or register plugin agents. Actual same-host availability and required fixture evidence decide each supported route. No additional native experiment is needed to correct this wording. The follow-up source review also identified an unconditional always-available-subagents instruction in ironclad; that separate operational instruction class is assigned to the behavior owner before source convergence.


### 2026-10-16 06:21 UTC — integrated telemetry full passed and exact head published

The default full suite on `13ceadfe72706ec83832b89643cf9ba5e66ad5a9` completed at **06:21:37 UTC** with **9780 passed, 78 skipped, four warnings, exit 0** in 2131.50 seconds. JUnit records 9858 cases, zero failures and zero errors; failed/error names are empty. Owned session and pytest process are terminal. Post-run readback preserves every one of 1374 worktree/export files and modes, the Python 3.11.5 executable and all 33 dependency versions. The result is 23 passes above frozen `606e8798`, exactly the separately counted incoming P4 cases. The original `15ea28db` before leg remains an ancestral baseline, not a fresh full result for current main.

Root verified the terminal counts/source/runtime assertions, then published only this final head by normal fast-forward from public `9d998d94` to `13ceadfe`. [PR #2364](https://github.com/Claudfather/Claudlobby/pull/2364) remains **draft**. Its body was updated with the qualified source/full/canary evidence after checking for concurrent edits, and exact body/head/draft readback passed. Fresh exact-head CI has started. Independent terminal-proof readback is underway; no current-head CI pass, source merge, production activation or completed seven-day window is claimed here.

### 2026-10-19 UTC — behavior source and v0.30.0 delivered; telemetry CI converged

[clauDNA #417](https://github.com/Claudfather/clauDNA/pull/417) merged as `3d6bfc60ac7e6f064cad8759ca4b8937cda740a3`, tree-identical to independently reviewed `3f072e0b`. All nine exact-head checks passed before authorized admin integration. Independent stranger review covered all 73 changed paths; five material source findings were fixed and re-reviewed. The released-baseline audit is valid: all 194 original selected entries match their original commit; a later baseline refresh updates one two-line docstring and the empty-mode statistic. The earlier allegation of mixed audit provenance was retracted, not counted as a fixed defect.

Fresh separately prepared Python 3.12.13 exports record baseline **1 failed, 2134 passed, nine skipped** and final **1 failed, 2186 passed, nine skipped**, make exit 2 both. The same permissions-hook test fails on macOS; no new failed names. The preserved first candidate had six additional failures, subsequently fixed. Actual Python 3.9.6 floor: **944 passed, exit 0**. The corrected final aggregate retains all seven complete historical test IDs and has SHA256 `157d393d1ab2f22566dfcb56a74d65207558b2822d84277cf9f63852bdcf746e`. Independent final evidence review returned **NO FINDINGS**.

Final source packaging independently discovered 36 skills and six untrusted handlers against all 438 matching files/modes with no auth, model turns or hook trust. Historical native action evidence remains assigned to its earlier snapshot: actual installed workflow reads, the unhinted known-bug review and real child spawn/wait are distinct from final packaging. Sealed child payloads do not establish plaintext prompt byte identity. The original startup-marker arm exposed no hook exit receipt; the instrumented arm is a separate observation. The catalog measurement remains **17/18, exit 1**, preserving the ironclad mode miss and unchanged fixture/judge; no all-pass result or reason for the model's null mode is inferred.

The five-path metadata/current-scope documentation [release #418](https://github.com/Claudfather/clauDNA/pull/418) merged as `4e4a5401116a7407b12f80c90a4437546cca4f56` after independent review and exact-head CI. The actual [v0.30.0](https://github.com/Claudfather/clauDNA/releases/tag/v0.30.0) tag and release were read back. A fresh anonymous native **public** `Claudfather/clauDNA` marketplace installation at that exact tag verifies the ingested Git origin/HEAD/tag, all 438 marketplace and installed-cache bytes/modes, exactly 36 native skill entries and six untrusted handlers. All native commands and app-server lifecycle exited 0; original auth/config and the executable are unchanged. No auth copy, model turn or hook trust was used. The separate public v0.29.0 ingestion also matched all 425 files; historical v0.28.1 ingestion failure remains preserved. Fresh cross-phase registry checks after both source and release merges passed **39/197/944**, all exit 0 with source identity preserved.

Telemetry [#2364](https://github.com/Claudfather/Claudlobby/pull/2364) retains exact head `13ceadfe` and draft status. Its own full suite passed 9780/78/four warnings/0, affected checks 210/0, and actual CLI intake/doctor canary passed. Independent terminal proof review and all nine latest exact-head CI checks are now successful. This clears source verification; the complete seven-day floor-host RC/Telegram observation is still unperformed and gates merge.

**Goal NOT MET.** An independent reachability review found no separate source deliverable to extract around the agreed dependencies: Task 9 waits for the actual telemetry week and measured ratios, then owns its implementation and 48-hour observation; #2149 source waits for real Half B service/supervision acceptance, full 48-hour BEFORE and AFTER windows, and accepted merge. #404 remains open for the actual mixed-fleet week. Concrete independent host/root/fleet/unit/channel scope is still required before live delivery, migration or activation. No production rollout or elapsed window is claimed.

### 2026-10-19 UTC — native carrier failure and temporary-evidence cleanup

The independently reviewed profile prerequisite disproved the tested 0400 immutable-profile candidate: captured bytes/mode changed with a native TUI preference across the interactive sequence. Strict unknown-field rejection passed; positive settings observation stopped at workspace-routing discovery before a prompt. The first observer has no native exit receipt, and final original auth/config/binary readback is unperformed. F2149-5 is reopened; no alternative is selected, no policy capability inferred and no execution source start is claimed around Half B. The execution plan and sanitized receipt preserve these limits.

The operator requested temporary-evidence cleanup. Running-process/open-file and open-PR checks found no active job depending on the old exports. A private gitignored durable archive preserves 9,443 main receipts/logs/maps plus 280 original-capture supplements, including native transcripts, provider/OTLP bodies, PTY data, prompt inputs and both original source-pin audit tars. Every archive member was hash-verified, with independent retention review. Cleanup removed 21.864 GiB of scratch allocations and measured 18.793 GiB additional filesystem availability; no errors. All 1,862 protected Plane/activation metadata entries remained unchanged. Global agent state was outside the deletion scope. The remaining small release working set is pinned until the final registry completes.

The fresh post-v0.30.0 registry passed 39/197/944, all exit 0 with source identity preserved. The actual public native install passed all 438 source/cache files and modes, exactly 36 skill entries and six untrusted hooks; five commands and app-server exit were 0, with no auth copied, model turn or hook trust. Both final runs automatically archived/read-back their receipts and removed their just-created disposable directories. Later independent reviews use the retained maps, raw RPC and reviewed driver; they do not claim to reread discarded caches. Future workstream runs use this archive-before-disposal lifecycle.
