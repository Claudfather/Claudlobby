# Converting an existing source-pulling host

This release uses **a coordinated conversion runbook, with no compatibility shims**.
Do not pull #1989 into a running checkout. Old units and running sessions may read
`lib/` on every use; deleting their task, report or setup entrypoints strands them
before a later restart can repair their composed instructions.

The candidate is built in a separate checkout. Its sealed CLI can assemble and
stage against the existing host data root without replacing the live source.
First adoption then runs through `host activate --adopt-existing`, which owns
handoff, quiescence, configuration installation, unit replacement, release
selection and startup. **This operation is host-wide, not a rolling restart of
individual production bots.** A host with any protected busy bot must wait.

The commands below describe the implemented doors. They do not certify a
populated Linux/Pi conversion or interrupted native recovery; those acceptance
results remain required before production use.

## 1. Hold the old source and preserve the recovery inputs

Before merging incompatible code into the branch an old source-puller follows,
identify that host's exact installed pull-root timer/service or launchd label by
its executable and checkout path. Either stop and disable that exact scheduler
and verify its service is not running, or verify that the installed old job reads
and enforces a commit ceiling below this conversion. For a ceiling, inspect the
actual installed script and its configured input; a setting in new source is not
proof. Confirm that expiry keeps holding instead of releasing it, let any running
pull finish, and prevent concurrent manual pulls. Keep the source hold until
conversion is verified; it is not permission to cross the ceiling.

Record, for this host:

- The live source commit, authored fleet manifests and host override, selected
  release if present, and the exact installed units with their enabled/running state.
- Every fleet and bot on the host, its private tmux socket, active assignment and
  handoff status, and explicit restart holds. Preserve protected sessions until
  their operator releases the hold.
- Recoverable configuration and state backups. Take the final SQLite/Plane backup
  with its existing consistent-backup procedure or while writers are quiesced;
  copying only a live SQLite database file can omit WAL data. Do not copy secret
  values into the PR or evidence report.

Also record these, because first adoption cannot recover them later:

- **Run intent.** List every de-enrolled bot (the explicit stop contract),
  and every `<bot runtime dir>/autonomous-runner.paused` marker. Activation starts
  every candidate bot, and the new runner gate reads only recorded
  `bot automation` state. It therefore refuses, before any record or native
  effect, while a candidate includes a stopped bot or a configured runner whose
  legacy marker has no recorded pause. A paused runner is never silently resumed.
  Resolve it in the reviewed candidate:
  - omit the stopped bot, or start it deliberately;
  - remove `autonomous_runner` from a paused bot and re-plan, then record
    `bot automation pause` after activation before restoring it.

  Remove a marker only when that runner may resume.
- **Open legacy work.** List every open dispatch and assignment row per bot, using
  the old installation's own reads. Legacy rows keyed only by a `dispatch-log`
  content hash are not mapped to canonical tasks. Have the manager complete,
  report or withdraw that work through the old installation before the window,
  and keep the list with the recovery inputs so it can be compared after adoption.
- **Plane queues.** Count the files in `state/plane/staged/`, in the spool and its
  inflight claims (`state/plane/spool/`), and in `state/plane/spool/quarantine/`,
  without opening or moving any of them.

On Linux, this release targets `systemctl --user` units. Ordinary system-level
units, foreign unit ownership or an incomplete roster require a separately
reviewed conversion; do not silently move them into the user manager or use sudo
to bypass refusal. On macOS, verify the owning GUI/user launchd domain and any
persistent disabled overrides. Never stop jobs by a broad name or process match.

## 2. Build, install and stage without pulling the live checkout

In a separate build checkout at the approved aggregate commit, follow
[Getting started, steps 1–2](getting-started.md#1-prepare-the-release-inputs) to
build the wheel, prepare its hash-locked wheelhouse, install it into a bootstrap
venv and call `host setup`. For an existing estate, set `DATA` to the **existing
absolute host data root** that owns the fleet manifests, runtime and Plane state;
do not substitute a new empty root or copy only part of an estate. Keep `WORK`
and the build checkout separate from that root. Retain the exact `RELEASE_CLI`,
`RELEASE_ID`, wheel hash and native `USER_UNIT_DIR` reported by assembly.

Assembly leaves selection unchanged and starts nothing. Keep the old checkout
pinned so old bots and enrolled timers can still read their existing scripts.
Do not hand-install units or regenerate live bot directories between these steps.

From an external operator shell, use the assembled sealed CLI:

```bash
"$RELEASE_CLI" --root "$DATA" config plan --release "$RELEASE_ID"
# Set PLAN_ID to that successful result's plan_id, then inspect it:
"$RELEASE_CLI" --root "$DATA" config diff "$PLAN_ID"
"$RELEASE_CLI" --root "$DATA" host status
```

Every authored manifest must declare `fleet.manager`, naming one of its own bots.
There is one implicit manager per fleet, and `config plan` refuses a manifest
without one. If an old manifest lacks it, adding it edits a file that the running
old installation also reads. First confirm that the old installation accepts the
field, then make the edit while the source hold is in force.

For fleet manifests outside the discovered root layout, pass each exact authored
manifest with repeated `config plan --fleet-path PATH`. Review the **complete
host roster**, producer jobs, ingest ownership, task-history migration blockers,
removed legacy units, candidate native command paths and changed permissions.
All expected task-recheck jobs must target the candidate CLI, not the removed
`lib/task-recheck.sh`. New sessions must use fleet-ops and the public task,
assignment and report commands. Opted-in bots must retain their heavy-slot hook.
A successful plan does not mean these effects have been delivered.

## 3. Canary the candidate, then authorize the host window

Before adopting production, exercise the same candidate on an **independent
canary root** with one bot (and a temporary peer when needed to prove delegation),
distinct native labels/private sockets, private Plane state and throwaway
channels. Use the same OS/native manager as the target; Mac evidence does not
substitute for user-systemd/Pi evidence. Record the exact candidate and plan.

Prove public task admission/assignment/delivery, linked and deliberately unlinked
reporting, control-note safety, handoff/restart continuity, expected timer targets,
and the heavy-slot behavior for an opted-in bot. Check ordinary degraded delivery
and strict recording refusal only through an explicitly authorized bounded fault.
Retain honest unavailable/refused outcomes. Unknown delivery is never permission
to resend automatically. Capture normal-load Pi timing separately.

### Pre-adoption queue and data preflight

First adoption refuses unless the existing Plane is readable and the staged,
spool and inflight queues are all empty. Quarantine is retained and inventoried,
without replay or deletion. It checks this before any
activation record or pause, and again after quiescence. Before the window:

- Keep the **old** ingest daemon running so it can commit staged and spooled
  batches, then confirm that those directories are empty. A new pending entry
  written between that check and the window will still block adoption.
- Do not use the sealed CLI to drain or move queue entries on a host that has not
  yet been adopted. `plane spool retry` and `plane spool quarantine` need an active
  selected release and refuse beforehand. `plane status` and `plane doctor` refuse
  a Plane that still needs migration, so they cannot count these queues there.
- **Quarantine is evidence, not debris.** Its entries are retained outside replay,
  and neither replay nor discard may be assumed. Never delete or replay them to
  satisfy the check. Readable quarantine bytes remain bound to the migration
  inventory even when their payload is malformed. An unreadable, missing or
  changed quarantine file still refuses; adoption never repairs it automatically.
- An inflight spool claim is normal while the old daemon is draining. One that
  persists after the queues settle needs its ownership checked with the old ingest
  stopped. Do not remove it by hand.

A refusal with `no activation record was created` means the host is unchanged, not
that adoption succeeded. Run-intent and queue/data refusals report the blocker
and safe next step without exposing native stderr or pending payload values.
Correct that cause before retrying. Only `host status` and the step-5 observations
establish success; an incomplete log or a refusal never does.

The production window requires every protected workload to finish or receive an
explicit operator-approved handoff. A passing isolated canary does not release
those holds. Do not use `fleet setup` to overwrite an existing manifest as a
shortcut around first adoption.

## 4. Adopt through the one activation owner

After the host window is authorized and the plan/source/roster remain unchanged:

```bash
"$RELEASE_CLI" --root "$DATA" host activate "$PLAN_ID" \
  --adopt-existing --install-directory "$USER_UNIT_DIR"
```

Use `--adopt-existing` for the first unsealed, running estate only. For a host
already selected into the sealed model, omit that flag and use the upgrade path.
Do not run manual unit enrollment, live regeneration or per-bot restarts alongside
activation. The coordinator pauses old producers, saves handoffs and quiesces
sessions/ingest before installing the staged configuration and units; it records
the release selection and starts the candidate through the native owner.

Record the activation ID and all outcomes. A refusal or incomplete result is a
stop condition, not an invitation to delete locks, selection or history.

## 5. Verify before releasing the source hold

```bash
"$RELEASE_CLI" --root "$DATA" host status
"$RELEASE_CLI" --root "$DATA" host doctor
"$RELEASE_CLI" --root "$DATA" --fleet "$FLEET" fleet reconcile
"$RELEASE_CLI" --root "$DATA" --fleet "$FLEET" brief
```

Repeat the fleet-scoped observations for every declared fleet. Confirm:

- The selected release and activation match the approved candidate, with no
  pending step, and native enrollment points to the candidate's installed paths.
- Old task-recheck and source-puller units are retired; candidate watchdogs,
  recheck jobs and ingest are admitted. Confirm no scheduled or running unit still
  executes a deleted path. Keep the old puller disabled; do not re-enable it.
- Every intended bot starts a new session with the staged CLI coaching and grants,
  retains task/session continuity, and answers a real channel probe. A tmux server,
  old log line or someone else's Telegram poller is not that proof.
- The Plane and request outcomes agree with observed actions. Warnings and unknown
  data remain visible; an empty or unavailable view is not a healthy estate.

Only after those observations may the operator release the **source checkout
hold** for development. Running bots continue using the selected sealed release;
a source pull is no longer a deployment. Remove superseded units/artifacts only
when their absence from the installed inventory and consumers is established.

## Interrupted activation and recovery limits

Inspect `host status` using the same sealed candidate, root and recorded IDs.
For a supported interrupted stage, resume the same frozen plan and activation:

```bash
"$RELEASE_CLI" --root "$DATA" host activate "$PLAN_ID" \
  --install-directory "$USER_UNIT_DIR" --resume "$ACTIVATION_ID"
```

Resume reconciles the existing journals and never repeats a handoff with a
recorded result. `supported_step: null` means the available evidence cannot
support safe continuation, including a begun handoff without a recorded outcome
or an older record without per-bot handoff evidence. Keep the host/source hold,
inspect the exact native/session state and obtain an explicit recovery decision.
There is no blanket rollback command for arbitrary unknown native effects.
Do not invent a new activation ID, remove coordination files, restore old unit
files over running candidate processes, or report an incomplete adoption as done.

If one candidate bot's start at pending `bots_started` has no result because
that bot died, and it is verified dead, an operator may archive that one attempt.
The command starts nothing. It can run from a newer CLI:

```bash
claudlobby --root "$DATA" host repair-start "$ACTIVATION_ID" \
  --fleet "$FLEET" --bot "$BOT" --reason "session exited before bridge"
```

It refuses unless all of these hold: this activation is selected and pending
`bots_started`; the named bot has a start intent with no result; its source and
installed unit bytes match the frozen start; and its unit is inactive with no
accepting private tmux server. It checks that the caller runs outside every
unit this activation recorded, not the ordinary unit inventory. The old attempt, its fence and the dead evidence
stay in the activation record under `start_repairs`; other receipts are not
changed. Then run the sealed candidate's `--resume` command above once. That
command gives the bot a fresh fence and starts it. Nothing retries
automatically, so each further repair needs the command again.

For the specific refusal "existing canonical handoff section is malformed" at
pending step `queues_classified`, the journal supports repair-forward before
migration. Preserve a copy of the named bot's `.claude/session.md`, retain its
operator/session notes, and correct only the malformed stale canonical section.
Inspect `host status` again and use the same plan, candidate, install directory
and activation ID with the resume command above. Do not clear journals or invent
another activation ID. A focused test exercises this exact repair through the
migration boundary with no repeated native operations; it does not establish
native recovery or completed adoption. This procedure does not cover a changed
task assignment, missing manager, unknown handoff outcome or a later migration
failure. Keep the host held when the recorded state does not match this case.

If incompatible source has already been pulled, keep source updates disabled and
preserve the observed state. Use the recorded activation owner where its evidence
permits; recovery of an unrecorded partial estate requires a host-specific plan.
