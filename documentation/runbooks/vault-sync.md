# Runbook — the `vault-sync` host job

**What it is.** A dormant host job that runs Claudron's `sync` door on a
cadence against every vault this host's bots are wired to, records each outcome
on the observable plane, and pages once when the state changes.

**Why it exists.** Claudron's sync fires only at session boundaries, under a
2-second hook budget, and reports to a log file *inside the vault it is failing
to sync*. A host wedged on 2026-09-09: every sync refused for twelve days,
printed success-shaped output, and **nothing scheduled ever looked**. The
sibling fixes stop the wedge happening. This is the job that notices one.

## Arming it

Dormant by default — `enroll: false` composes **no unit at all**. Armed, it
**commits and pushes** on every host it runs on, which is why it is opt-in.

In **that host's own** `system.yaml` (host jobs bypass the fleet merge —
`fleet.yaml` cannot reach them):

```yaml
host:
  jobs:
    vault-sync:
      enroll: true
```

then:

```
lib/setup-system
systemctl --user list-timers | grep vault-sync      # Linux
```

Arm **one** host, watch `vault.sync_ok` for a week, then arm the second.
`claudlobby doctor --switches` lists the job either way, so the off state is
visible rather than merely documented.

**Backout:** `enroll: false` + `lib/setup-system` — the unit disappears.

**If you arm this once the engine carries `sync --check`, read the first few
`vault.state` samples yourself and confirm they carry the verdict you expect
before trusting the quiet.** That is the whole ask; the rest of this block is why.

**The rehearsal has never run the state you will be in.** All five scenarios in
`lib/rehearse-vault-sync.sh` drive a stub whose `--check` arm exits 2
unconditionally, so **every** scenario runs with `state=unknown` and **none has
ever seen an rc-0 verdict**. That was harmless while no engine had the flag. It
stops being harmless the moment the engine carrying `sync --check` is pulled here
(see the note under *When an ALERT fires*): `state=clean` then becomes what this
job runs in every day, and it is the single state the end-to-end harness has
never exercised.

Nothing is known to be wrong on that path, and here is exactly how much is
known: **one** unit test reaches it —
`test_a_check_verdict_is_recorded_when_the_engine_has_one` in
`tests/test_vault_sync.py` — which drives the real script with a real verdict
envelope and asserts the log records `state=clean`. That is the branch being
*reached*, not evidence that the plane sample or the paging behave correctly
under it. (Saying "it is covered by unit tests" would be the same overstatement
this file just finished removing one paragraph above.) What is missing is the
end-to-end evidence, and for a dormant job whose case for arming rests on that
harness, the person arming it should be the one told so rather than a reviewer
who read the stub.

## Cadence

Every 15 minutes, with a 120-second randomised delay so two hosts do not hit
the same remote at the same instant. The cadence *is* the deliverable: it
bounds how long a wedge can sit unseen, which was twelve days.

## What it records

Metric samples per vault (subject `vault:<name>`):

| metric | meaning |
|---|---|
| `vault.sync_ok` | `1` / `0` — every run, both directions, so a missing sync has a denominator |
| `vault.state` | Claudron's `sync --check` verdict, or **`unknown`** when the installed engine has no such flag |
| `vault.ahead` / `vault.behind` / `vault.uncommitted` | when the envelope carries them |

Plus one `vault_sync` system event on any failure, carrying the refusal text.

**`unknown` is a real answer, not a gap.** Until Claudron's health door ships,
the job records `unknown` rather than inventing a state. A fabricated `clean`
is precisely the success-shaped output this program exists to end.

## When an ALERT fires

```
FLEET ALERT vault_sync_failed — vault sync FAILED for vault:<name> (state=…):
<detail> — run 'claudron sync --check' in that vault; this job never resolves
a conflict
```

1. **Run the check it names**, in that vault: `claudron sync --check` (or
   `claudron sync` on an engine without the flag) and read the refusal.
2. **The job does not fix anything, by design.** It never resolves a conflict,
   aborts a rebase or expires a lock — Claudron's `sync` does those or refuses
   them, and a scheduled door that rewrites a tree under a running fleet is the
   failure mode, not the remedy.
3. Typical refusals and where they are handled: a stopped rebase and a
   side-branch clone are Claudron's `sync` to abort or refuse; a stale lock is
   its lock handling. This job reports them.

**You will get exactly one page per change in whether the sync SUCCEEDED.** A
still-wedged vault on the next tick says nothing — the arm is the transition, not
a clock, because paging every 15 minutes about a still-true condition is how an
operator learns to mute the channel. Recovery produces exactly one NOTICE.

**What does NOT page, stated because an earlier draft of the line above promised
more than the job delivers.** The debounce marker holds `ok` when the sync
succeeded and `bad:<health-verdict>` when it did not, so the health verdict is
part of the compared state *only while the sync is failing*. A verdict that
changes while the sync keeps succeeding — `clean` → `unknown` because
`sync --check` stopped answering, and back — therefore pages **zero** times.
Measured: three ticks across that transition, marker `ok` throughout, no page,
while a real failure on the next tick paged normally.

It is **recorded, not lost**: every tick writes a `vault.state` sample, so the
change is on the plane and queryable —

```
claudlobby --fleet <name> events --type vault_sync --tail 50   # the failures
```

— and the samples themselves (`vault.state`) carry the verdict per run. If you
want to know that the health door stopped answering, read the samples; do not
wait for a page. That gap is deliberate rather than overlooked — a verdict
change with a succeeding sync is a softer signal than a failed sync — and it is
tracked as #1741, so whoever meets the decision makes it rather than inheriting
this answer by accident.

**The verdict is `unknown` until the engine carrying `sync --check` is pulled on
this host — not "for now".** The flag exists: it landed in Claudron on
2026-09-22 (PR 162, merge `cd720eb`, `claudron/cli.py`). It reaches a host only
when that checkout is pulled, which is its own open problem (#1251). So the
condition is a **pull**, and stating it that way is deliberate: an earlier draft
of this line said the flag "does not exist yet", which was true when written and
stops being true with nobody editing the file — the reader then trusts a word
that has already expired. Measured on this host at the time of writing:

```
$ claudron sync --check --json
claudron: error: unrecognized arguments: --check     # rc 2
```

rc 2 is argparse's usage error, which is exactly what this job records as
`unknown`. **Check it rather than trusting this paragraph** — run that command
in the vault. If it answers an envelope, the engine is current here and the
verdict will be a real one.

## Reading the history

```
claudlobby --fleet <name> events --type vault_sync --tail 50
```

The samples answer "when did this vault last sync successfully, and how often
does it fail" — the denominator the hook-only path never had.

## Rehearsing it

`bash lib/rehearse-vault-sync.sh` drives the real job through clean → wedged →
still-wedged → recovered against a real plane in a throwaway root, with a
stubbed engine and a fake escalation chat. It carries a positive control: a
*changed* failure state must page again, or the debounce is muting rather than
debouncing.
