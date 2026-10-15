# Runbook: the P0 Codex canary batch on the Mac mini (epic #2145 §6 P0: step 0, C1–C5, C7–C9)

> Epic: `documentation/plans/2026-10-04-runtime-neutral-observability-plan.md` (§6 P0, §10 order 10). Results go in the run log, `documentation/plans/2026-10-04-runtime-neutral-observability-run-log.md`. Hand this whole file to an agent session on the machine with the Codex CLI installed.

You're a Claude Code session on the operator's Mac mini, which has the Codex CLI installed. You measure how Codex
behaves at the points the observability epic depends on, and write the answers to `~/rnc-results-4.md`. **Nothing
here touches the Pi, a bot, a fleet, or any repository.** You push nothing and post nothing.

**Before anything:** if `~/rnc-results-4.md` already exists on this Mac, stop and hand it to the operator; this batch
ran already. (The numbering is out of order on purpose: `rnc-results-5.md` is the Half A canary, which ran first.)

## Ground rules

- **Isolate Codex from the operator's own setup.** Every Codex command runs with `CODEX_HOME=$C`, a throwaway
  home (Step 0), so the operator's `~/.codex` (config, history, sessions) is never read or changed. If Codex
  can't authenticate from the throwaway home, stop and ask the operator how they want to sign in (copying their
  `auth.json` into `$C`, or a fresh login). Don't guess.
- **No hosted telemetry, ever.** Step 0's preflight goes into `$C/config.toml` before the first Codex session.
  The only network peers allowed are the model API and `127.0.0.1`.
- **Never record content.** The harness keeps ids, event names, and key names and types only. Never paste a
  prompt, a model reply, a tool's output or a rollout's text into the results. Ids, field names, record *types*,
  sizes, counts and timings are fine. The prompts you give Codex are throwaway ("run `true`", "reply OK").
- **Don't guess Codex syntax.** Hooks, `[otel]`, plugins and `/hooks` trust are recent Codex features. For each
  one, read the installed version's own docs first (`codex --help`, `codex <sub> --help`, and the official Codex
  docs for that version). Record the exact config you used. If the installed version lacks a feature, record
  `codex --version` and "not supported", and move on.
- **Record exact commands, outputs and UTC times.**
- **If anything fails, record the exact error text and continue with the next canary** unless it's Step 0.

## Step 0: setup and the telemetry preflight (before any Codex session)

```bash
export R=~/rnc-codex C=~/rnc-codex/home D=~/rnc-codex/run
mkdir -p "$C" "$D"
codex --version
```

1. **Get the harness** from Claudlobby `main` (#2201, merged 2026-10-07, adds the Codex fields), into `$R`, not into
   any checkout:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Claudfather/Claudlobby/main/harness/runtime-neutral-canary.py -o "$R/rnc.py"
   export H="$R/rnc.py"; python3 "$H" --help | head -3
   grep -c 'CODEX_' "$H"   # non-zero: this is the version with the Codex fields
   ```
   Record the file's sha256.
2. **Write `$C/config.toml` with the preflight** (epic §6 P0 step 0): `[otel]` with `metrics_exporter = "none"`
   and `[analytics]` with `enabled = false`. Check both keys against this version's docs, then record the file.
3. **Auth** (see the ground rules), then a smoke run: `CODEX_HOME=$C codex exec "reply OK"` (or this version's
   non-interactive form). Record the rc.

## C1: hook payloads (with C9 and the C2 walk)

1. **Wire the harness to every hook event this Codex version offers**, from its docs (expect at least
   `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`, `Stop`, `SessionEnd`). Each
   command is:
   ```
   python3 <H> hook --log <D>/hooks.jsonl
   ```
   with absolute paths. For **C9**, give `SessionStart` a second command that adds a host selector in the
   position the clauDNA adapter would use, logging to its own file:
   `python3 <H> hook --log <D>/hooks-host-codex.jsonl` (C9 passes if this file fills, which proves the command's
   own arguments reach the hook). For the **`SessionEnd` budget**, use
   `python3 <H> hook --log <D>/hooks.jsonl --hold 800`. A second arm later uses `--hold 2500`.
2. **Trust-by-hash.** The docs say hooks must be trusted before they run. Record exactly what this took: the
   `/hooks` screen, a prompt, a config key. A composed bot needs this step unattended.
3. **Run the session in tmux** (this doubles as C7's setup), from a scratch dir:
   ```bash
   mkdir -p "$R/work" && cd "$R/work" && git init -q
   tmux -L rncx new-session -d -s cx -x 200 -y 50 "cd $R/work && CODEX_HOME=$C codex"
   ```
   Drive it with `tmux -L rncx send-keys -t cx '<text>' Enter`. Watch with `tmux -L rncx capture-pane -p -t cx`,
   recording only the prompt and status lines, never replies.
   1. Prompt: `run this shell command and nothing else: env | grep -E '^(CODEX_SESSION_ID|CODEX_THREAD_ID)=' > <D>/bash.env`.
      This is C1's "session-id env var for tools" question.
   2. Prompt: `run: true`, to get a tool-call pair.
   3. A manual compaction (`/compact`, or this version's command). Record whether `PreCompact` fires and whether a
      `SessionStart` with `source: compact` follows.
   4. Exit normally (this version's quit command). Record whether `SessionEnd` fired and whether its `held` line
      landed (`report` → `holds`).
4. **The resume arm.** Run `CODEX_HOME=$C codex resume --last`, or this version's form. Record `SessionStart`'s
   `source` and whether `session_id` is the same as before. Exit.
5. **The `SessionEnd` budget, second arm.** Set the hold to `2500` and repeat a start then exit. Record whether
   the `held` line landed (the docs say 1 s by default, 3 s max). If the timeout is configurable, record the key.
6. **The socket table, during step 3.** Run
   `lsof -nP -i -a -p $(pgrep -n codex) | awk 'NR>1{print $9}' | sort -u` while the session is up. Map remote IPs
   to names (`dig -x`, or `lsof` without `-n`). Record every peer. Anything besides the model API and 127.0.0.1
   is a finding; a `statsig` peer means the preflight failed.
7. **Answers.** Run `python3 "$H" report "$D"`. From it and the logs, record:
   - `payload_keys_by_event`: the real field names per event;
   - the `session_id` format, and whether it matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`
     (test with `python3 -c 'import re,sys;print(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", sys.argv[1])))' <sid>`);
   - whether `transcript_path` is set on each event (`keys` shows `str:N` when it is);
   - `env_names_seen` / `env_values_seen` for the hook processes, against `bash.env` for the tool shell (the docs
     say child shells get `CODEX_SESSION_ID`/`CODEX_THREAD_ID` and hooks get neither);
   - `SessionEnd`'s `reason` values;
   - the holds;
   - C9's file.

## C2: the owner process

From `report` → `ancestor_comms`: the chain of command names above each hook process. Record:
- whether a stable `codex` process (the one that lives the whole session) appears in it;
- how many levels up it sits;
- whether any env var names its pid, in `env_names_seen` (the `$CLAUDE_PID` analogue).

## C3: context injection (Codex, then Claude)

Generate two nonces: `N1=$(uuidgen | cut -c1-8)` and `N2=$(uuidgen | cut -c1-8)`. They are throwaway values, so
they're fine to record.

1. **Codex `SessionStart` stdout.** Add `--stdout "Canary marker for this session: $N1"` to the `SessionStart`
   command. Start a session and ask: `what canary marker were you given at session start? reply with the marker only`.
   Record yes or no (whether the reply equals `$N1`). Exit.
2. **Codex after compaction.** Change the `SessionStart` command's text to `$N2`. If this version can target
   `source: compact` specifically, use that; otherwise record that it can't. Start a session, `/compact`, then
   ask the same question. Record which marker comes back.
3. **Codex `PreCompact` block.** Give `PreCompact` the command
   `--stdout '{"continue": false, "stopReason": "canary"}'`. Run `/compact`. Record whether compaction was blocked,
   and anything shown. Then remove it.
4. **Claude, per Claudron#179.** This one uses `claude`, in a throwaway settings file passed with `--settings`.
   Never edit `~/.claude/settings.json`.
   - Set a `PreCompact` hook printing `{"decision": "block", "reason": "Canary marker: <N3>"}`.
   - Start an interactive `claude --settings <file>` in tmux, run `/compact`, then ask for the canary marker.
   - Record whether the block reason reached the model. Also record whether a `SessionStart` hook with
     `matcher: compact` printing `<N4>` reaches it after `/compact`.
   - Use `tmux -L rncc`, and kill the server when done.

## C4: rollout files

Find the session's rollout file under `$C/sessions/` (newest). Never print its lines. Record:
- the path pattern, relative to `$C`;
- **append-only:** note its byte size and the sha256 of its first N bytes, run one more turn (`resume`, then `run: true`), then confirm the size grew and the first N bytes' hash is unchanged;
- **record types:** `jq -r '.type // .kind // "?"' <file> | sort | uniq -c`, and for each type its top-level and payload key *names* (`jq -c '[.type, (keys)]'`, and one level into the payload);
- which types carry user text, assistant text, **per-turn token usage** and **tool calls** (from the key names: `content`, `usage`, `function_call`, …);
- whether the rollout's id matches the hook `session_id`.

## C5: Codex OTel to a local receiver

1. Start the receiver: `python3 "$H" receiver --port 14320 --out "$D/otlp" &` and record its pid.
2. Set `[otel]` in `$C/config.toml`, per this version's docs:
   - an OTLP/HTTP **JSON** exporter to `http://127.0.0.1:14320/v1/logs` (the full `/v1/logs` URL);
   - `environment = "canary-bot"` (the candidate per-bot carrier);
   - user-prompt logging **off** if the key exists.

   Keep `[analytics] enabled = false`. Record the block.
3. Run one short session (`run: true`, then `reply OK`). Exit. Wait 15 s, then run `report "$D"`.
4. Record:
   - `requests` per path (are there `/v1/traces`?);
   - `all_json`;
   - `event_names`;
   - `resource_keys` (does `environment` appear, and under which key?);
   - whether the OTel `conversation.id` (in `otel_session_ids`) equals the hook `session_id`.
5. **Socket table again**, while the session is up: peers must still be only the model API and 127.0.0.1.
6. Stop the receiver (`kill <pid>`).

## C7: Codex in tmux

From the C1 session, record:
- the idle prompt glyph and line (from `capture-pane`, that line only);
- whether `send-keys '/status' Enter` (or any slash command) is executed;
- whether text sent with `send-keys` while Codex is busy is queued or lost;
- after `tmux -L rncx kill-server`, whether a new `codex resume --last` picks the session back up.

## C8: plugin-manifest hooks

1. From `codex --help` and the docs: does this version have plugins or a marketplace? If not, record that and skip.
2. If it does, make a throwaway local plugin under `$R/plugin` whose only hook runs
   `env | grep -E '^(PLUGIN_ROOT|PLUGIN_DATA|CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA)=' > $D/plugin.env`.
   Install it into `$C` the way the docs say, run one session, and record which of the four are set and whether
   each points into the plugin.
3. Try the clauDNA marketplace in `$C` the same way Claude Code takes it (`Claudfather/clauDNA`). Record whether
   it is fetched, whether `claudna` installs, and whether any of its hooks run. clauDNA#120 saw the marketplace
   fetched but the plugin not installed.

## Clean up

- `tmux -L rncx kill-server; tmux -L rncc kill-server`, and kill the receiver if it's still running.
- **Keep `$D`** (the logs) until the planning session has the results.
- Ask the operator before removing `$C`: it may hold a copied `auth.json`.
- The operator's `~/.codex` and `~/.claude` are untouched; confirm with their mtimes before and after.

## Results file

Write `~/rnc-results-4.md` with:
- `codex --version` and the harness sha256;
- a section per canary (step 0, C1, C2, C3 Codex and Claude, C4, C5, C7, C8, C9), each with:
  - the exact config used;
  - commands, outputs and UTC times;
  - the `report` JSON where relevant;
  - a one-line answer to each question the section asks;
- anything that failed, with the exact error text.

No content: ids, key names, types, sizes and counts only. Tell the operator it's ready; the planning session folds
it into the run log and the plans (P4 `host_codex.py`, the Claudron adapter, Claudlobby payload adapters,
#2149).
