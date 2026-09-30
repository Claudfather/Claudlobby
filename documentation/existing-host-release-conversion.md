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
its executable and checkout path. Stop and disable its scheduler through the
native user manager, verify the service is not running, and prevent concurrent
manual pulls. A setting in new source cannot stop an already-installed old job.
A commit ceiling remains held until conversion is verified; it is not permission
to let the next scheduled pull cross the ceiling.

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

If incompatible source has already been pulled, keep source updates disabled and
preserve the observed state. Use the recorded activation owner where its evidence
permits; recovery of an unrecorded partial estate requires a host-specific plan.
