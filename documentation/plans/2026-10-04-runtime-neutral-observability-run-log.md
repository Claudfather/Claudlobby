---
title: "Run log — runtime-neutral observability (#2145): P0 canaries"
type: run-log
status: P0 Claude batch — headless legs measured; the floor-host and interactive bot legs are the operator's
created: 2026-10-05
epic: 2026-10-04-runtime-neutral-observability-plan.md
issue: Claudfather/Claudlobby#2145
pr: Claudfather/Claudlobby#2144
---

# P0 run log

Epic §6 P0 and §10 order 1. Instrument: `harness/runtime-neutral-canary.py` (stdlib; `receiver` is a raw
OTLP/HTTP-JSON receiver on `127.0.0.1` that answers 200 first, the shape the `plane-otel` intake takes; `hook`
logs hook payloads; `c10` probes tmux env inheritance; `report` prints the verdicts). It records metric names,
temporality, event names, attribute *keys*, and the values of a short allowlist of id/enum keys. Every other
value is kept as its type and length. Prompts, tool inputs and responses never reach a log
(`tests/test_runtime_neutral_canary.py` pins that).

## Summary

| Canary | Answer so far | Decides | Still owed |
|---|---|---|---|
| **C6a** confirmation leg | **Passes off the floor host.** Claude Code 2.1.289 exports OTLP/HTTP-JSON logs and metrics straight to a local endpoint under the plan's env block; the resource attributes land verbatim on every row; temporality is delta by default; `session.id` = hook `session_id` across `--resume`. | F4 stays locked on this evidence; **F4's gate is the floor host**, so P2-a1 still waits on the operator leg. | Floor host (Pi): the same run; bot-hour volume and receiver footprint; the 24 h `tool_call` overlap; RC day 1. |
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
