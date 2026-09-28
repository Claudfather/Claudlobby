---
name: cross-fleet-initiative
description: "Coordinate a multi-team initiative across multiple manager fleets via a 7-stage operating loop (FRAME, DECOMPOSE, GATE, DISPATCH, MONITOR, SYNTHESIZE, CLOSE). Use when a strategic goal spans more than one fleet and needs the senior-management layer to keep both aligned, surface forks to the owner, and drive to a defined done state."
argument-hint: "<stage> <initiative-name> | bootstrap <initiative-name> <one-line-goal> | next <initiative-name>"
---

# Cross-Fleet Initiative

A senior-management operating skill. Owns the orchestration layer for any initiative that:

- Spans more than one fleet — a single fleet's `/autonomous-sprint` is wrong-shape.
- Has enough scope that the owner shouldn't be the operating loop (the orchestrator absorbs operating cadence; owner still owns strategic ratifies at gates).
- Has a definable done state (north-star metric, ship gate, validation result).

The skill is invoked **per-stage**. State persists in `<initiatives_root>/<name>/`. A `bootstrap` invocation creates the directory and starts at FRAME.

## Configuration

Destination fleet names and human-facing chat/project metadata live in `config.json` alongside this skill. Copy `config.example.json` to `config.json` and fill in real values. A configured name or chat handle is not routing authority: each destination fleet's current manager comes from `claudlobby --fleet FLEET --json fleet show` at use time (`data.fleet.manager`).

Required config keys:
- `managers`: array of `{name, fleet, telegram_handle, project_root, fleet_repos}` objects; `name` is a display label and `fleet` is the exact active destination identity
- `managers_chat_id`: Telegram chat where strategic forks surface
- `founder_handle`: Telegram user for direct escalation
- `founder_user_id`: Telegram user ID for programmatic tagging
- `cron_offset_minutes`: poll cadence offset (e.g. `8` for `8-59/15` cron)
- `initiatives_root`: absolute path where initiative state directories are stored

If `config.json` is missing, the skill exits and instructs the operator to create it from `config.example.json`.

The universal `fleet-ops` skill covers the caller's own fleet. A coordinator that works with another fleet needs explicit `bots.<coordinator>.tool_permissions.allow` entries in its fleet configuration. Replace `DEST` below with each exact active destination name and include only the verbs used:

```yaml
tool_permissions:
  allow:
    - "Bash(claudlobby --fleet DEST --json fleet show)"
    - "Bash(claudlobby --fleet DEST --json task admit *)"
    - "Bash(claudlobby --fleet DEST --json task list)"
    - "Bash(claudlobby --fleet DEST --json task list *)"
    - "Bash(claudlobby --fleet DEST --json task show *)"
    - "Bash(claudlobby --fleet DEST --json message send --to DEST/*)"
    - "Bash(claudlobby --fleet DEST --json request show *)"
    - "Bash(claudlobby --fleet DEST --json message receipt *)"
    - "Bash(claudlobby --fleet DEST --json message wait *)"
```

The message grant scopes the target fleet; resolve and address its current manager, not an arbitrary worker. These grants do not include destination `task assign` or `assignment deliver`. Do not add a blanket `claudlobby` grant. A permission or scope refusal is a stop, not a reason to use tmux.

## Arguments

Parse `$ARGUMENTS` as `<stage> <initiative-name>` or `bootstrap <initiative-name> <goal>`.

Valid stages:
- `bootstrap` — create initiative directory + STATE.md, set stage=FRAME, then immediately run FRAME
- `frame` — produce FRAME.md (1-pager artifact)
- `decompose` — produce DECOMPOSE.md (per-manager scope sheets)
- `gate` — produce GATE.md (milestone definitions)
- `dispatch` — fire opening dispatches to each manager
- `monitor` — start the 15-min poll cron with the initiative's MONITOR prompt
- `synthesize` — consolidate cross-fleet findings at a gate boundary; produce `synthesis/<date>.md`
- `close` — run cross-fleet retro; produce RETRO.md; codify lessons to MEMORY.md

If `<stage>` is `next`, the skill reads STATE.md and runs whichever stage is next.

## STATE.md schema

Every initiative directory has a STATE.md with:

```markdown
---
name: <initiative-name>
goal: <one-line>
started: <ISO date>
current_stage: <stage-name>
last_action: <ISO date>
managers: [<list>]
gates_cleared: [<gate-ids>]
gates_pending: [<gate-ids>]
forks_open: [<fork-ids with brief>]
status: active | paused | closed
---

## Decision log
- <ISO date>: <decision> (ratified by <who>)
```

The STATE.md is the durable index. All stage artifacts cross-reference it.

## The 7 stages

### 1. FRAME

**Inputs:** initiative name + one-line goal (from invocation), owner's strategic context (read from chat scrollback or prior directives).

**Output:** `FRAME.md` — one-pager artifact with these sections:

- **Anchor (north-star)**: the single sentence that orients everything.
- **Success metric**: how we know it's done. Empirical, observable.
- **Scope (in)**: 5-10 bullets naming exactly what's IN.
- **Scope (out, explicit)**: 3-5 bullets naming what's NOT this initiative (prevents scope creep mid-flight).
- **Connected initiatives**: the upstream and downstream this depends on or feeds.

**Procedure:**
1. Read STATE.md + any owner chat scrollback in the last 24h via Telegram MCP for context.
2. Draft FRAME.md based on the goal + context. Don't invent the anchor — extract it from the owner's language.
3. Post FRAME.md content to managers chat as a single message. Tag the owner, ask for ratification.
4. On ratification, mark `current_stage: DECOMPOSE` in STATE.md. On request-changes, iterate.

### 2. DECOMPOSE

**Inputs:** ratified FRAME.md.

**Output:** `DECOMPOSE.md` — per-manager scope sheet.

For each manager in config, a section:
- **Owner**: <manager name>
- **Lane**: the slice of the initiative they own
- **Repos**: which repos their fleet will touch
- **Workers expected**: which fleet members likely pick up the work
- **Cross-fleet dependencies**: what they need from the OTHER manager(s) and what the other manager needs from them
- **First dispatch directive**: the opening tactical step they should fire

**Procedure:**
1. Re-read FRAME.md.
2. For each manager, propose a lane. Apply the principle: each lane is single-fleet-actionable except for explicit handshake points.
3. Resolve each destination's current manager with `claudlobby --fleet FLEET --json fleet show`. Send the proposed lane with `claudlobby --fleet FLEET --json message send --to FLEET/MANAGER --text "..." --request-id UUID`. Keep the UUID and message ID; inspect `request show` and `message receipt`, then use `message wait MESSAGE_ID --for reply --timeout SECONDS` for a bounded reply check. A transport receipt is not agreement. Wait for each manager's actual response before marking the split accepted.
4. On all managers ack'd, mark `current_stage: GATE`.

### 3. GATE

**Inputs:** ratified DECOMPOSE.md.

**Output:** `GATE.md` — milestone definitions.

For each gate (typically 2-5 per initiative):
- **Gate ID**: e.g. G1, G2.
- **Name**: short.
- **Definition of done**: empirical, observable.
- **Unlocks**: what fires after this gate.
- **Owner**: which manager confirms gate-met.
- **Ratify-required**: does owner ratify at this gate or auto-proceed?
- **Risk**: what could go wrong; rollback plan.

**Procedure:**
1. Read FRAME.md (success metric) + DECOMPOSE.md (lanes).
2. Walk the dependency graph between lanes. Gates fall at the natural sync points.
3. Distinguish auto-proceed gates (orchestrator ratifies; owner gets visibility-only) from ratify-required gates (owner must explicitly bless).
4. Post GATE.md to managers chat. Tag owner for ratification on the gate-ratify-required list.
5. On ratification, mark `current_stage: DISPATCH`.

### 4. DISPATCH

**Inputs:** ratified DECOMPOSE.md + GATE.md.

**Output:** `dispatches/<manager>-<date>-opening.md` for each destination (durable scope text, with its admitted task ID recorded beside it).

**Procedure:**
1. For each manager, compose an opening dispatch with:
   - Brief framing (anchor + their lane in 2-3 sentences)
   - First-dispatch directive from DECOMPOSE.md
   - Cross-fleet handshake protocol (when to mirror what to the managers chat)
   - Gate references (which gates apply to their lane, who ratifies)
   - Reporting expectations (recorded task progress and manager-to-coordinator messages, cadence)
2. Save dispatch to `dispatches/<manager>-<date>-opening.md`.
3. For each exact destination fleet, admit unassigned work: `claudlobby --fleet FLEET --json task admit --title "..." --body-file dispatches/<manager>-<date>-opening.md --request-id UUID`. Retain the UUID and returned canonical `data.task_id`; use `claudlobby --fleet FLEET --json task show TASK_ID` to inspect the selected fleet's queued work. The destination fleet owns assignment and `assignment deliver`; this coordinator never assigns its workers.
4. Resolve that fleet's current manager with `fleet show`, then send a separate ordinary message naming the task ID and gate expectations: `claudlobby --fleet FLEET --json message send --to FLEET/MANAGER --text "..." --request-id UUID`. Inspect the request and `message receipt MESSAGE_ID`; use a bounded `message wait` when a reply is needed. Admission is not delivery, and a sent message is not worker acceptance. On uncertain recording or transport, inspect the same UUID and receipt; never mint a second send automatically.
5. After every destination task is recorded and the manager messages have been checked, mark `current_stage: MONITOR`. Record any unverified notification in STATE.md rather than claiming it landed.

### 5. MONITOR

**Inputs:** STATE.md (current stage, gates, open forks).

**Output:** standing poll cron + spotlight messages to managers chat.

**Procedure:**
1. Compose the MONITOR cron prompt parameterized to this initiative:
   - Read each destination's canonical task with `claudlobby --fleet FLEET --json task show TASK_ID` at 15-min cadence; read manager messages and independent gate evidence
   - Watch for: task progress, manager replies, gate-condition checks, PR state changes on initiative-relevant PRs, spotlights
   - **Context-aware manager nudge** at light cadence (every 2-3 polls). Classify each manager's state:
     - **WORKING** = recorded task progress or a substantive manager reply since the last poll
     - **IDLE** = the manager explicitly confirms it has no active work and the current task read supports that account
     - **UNKNOWN** = the manager has not confirmed its state, the read is stale or unavailable, or the two disagree; disclose the gap instead of calling it idle
   - If WORKING: optionally ask the manager to check silent workers, context, and rate limits through an ordinary message.
   - If IDLE: check for autonomous-allowed work queue on GitHub issues. If queue exists with unblocked items, ask the manager to resume through an ordinary message. If queue is empty or all items require owner ratify, surface a discussion to the managers chat.
   - Surface flags or forks to managers chat when input required
   - Update STATE.md with each gate clear and each fork opened/closed
2. Register the cron via CronCreate with offset-minute (per config) and the initiative-parameterized prompt.
3. The cron runs until the initiative reaches CLOSE OR until the operator runs `/cross-fleet-initiative pause <name>` (which CronDeletes and marks `status: paused`).

The MONITOR stage is steady-state; stages 6 and 7 fire on triggers within it.

### 6. SYNTHESIZE

**Triggers:** a gate's definition-of-done is met (recorded work and independent evidence support it) OR a fork lands that the owner must ratify.

**Output:** `synthesis/<date>-<gate-or-fork>.md` — consolidated read for the owner.

**Procedure:**
1. Pull the relevant state: gate definition (from GATE.md), canonical task state, manager messages, PR diffs or empirical artifacts, and each manager's read.
2. For gate-clear: confirm DoD is empirically met; document evidence; on auto-proceed-gates: ratify; on ratify-required: surface to owner with the compiled evidence + your read + the ask.
3. For fork-ratify: name the fork, name the options (use descriptive names not letters), name each manager's lean, name your read with the four lenses (best practice / future-proof / elegant / consistent-with-codebase), name the ask to owner.
4. Post synthesis to managers chat as a single coherent message. Tag owner.
5. Update STATE.md: gate cleared OR fork ratified.

### 7. CLOSE

**Triggers:** all gates cleared AND success metric empirically verified.

**Output:** `RETRO.md` + MEMORY.md updates.

**Procedure:**
1. Run the success-metric check empirically (hit the live surface, query the data, run the test).
2. Compose RETRO.md sections:
   - **What landed**: the durable artifacts (PRs merged, surfaces shipped).
   - **What worked**: patterns to repeat.
   - **What didn't**: friction points.
   - **What we'd do differently**: process changes to fold into the skill itself (the skill's feedback loop).
   - **Followups filed**: GitHub issues that emerged but were scoped out.
3. Append durable lessons to MEMORY.md (the user-level memory index).
4. Post RETRO.md summary to managers chat. Tag owner for "initiative closed" ack.
5. Mark `status: closed` in STATE.md. CronDelete the MONITOR cron.

## Operating discipline

- **Don't compress the stages.** Each is load-bearing. Skipping FRAME means later forks lack a north-star check; skipping GATE means decisions become discretionary; skipping CLOSE means lessons don't compound.
- **Don't escalate every fork.** SYNTHESIZE applies the four lenses first; only escalate when the lenses don't tip.
- **Don't lose state to a crash.** Every stage writes durable artifacts to disk before posting to chat. A restart at any stage rebuilds context from disk.
- **The owner owns the ratify; the orchestrator owns the loop.** The skill makes the operating loop mechanical — the strategic compass stays the owner's.
