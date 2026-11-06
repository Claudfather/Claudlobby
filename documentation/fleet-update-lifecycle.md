# Fleet update lifecycle

How a running fleet picks up new plugins, skills, and Claude Code binaries — **without losing a bot's working context.** North star: **no context loss**, not "no restart." Restarts are fine; losing context is the pain.

Two update classes, two mechanisms:

| Update type | Stays current (download) | Applied to a *running* bot | Restart? | Cadence |
|---|---|---|---|---|
| Selected plugin cache | `claudlobby fleet reload` refreshes plugins through `claudlobby/_runtime_scripts/reload-fleet.sh` | Keepalive sends `/reload-plugins` + `/reload-skills` when idle | **No** | Daily, `03:30`, + on-demand |
| Claude Code binary | `npm install -g @anthropic-ai/claude-code@latest` (`claudlobby/_runtime_scripts/update-claude-code.sh`) | new `claude` process at next start | **Yes** (binary swap) | Downloaded daily at `04:00`; applied via natural restarts + a weekly worker-only restart, `Sun 05:00` |

Binary updates take effect on the next session start. Framework and authored configuration updates use the common Git updater, which saves and stops affected consumers before publishing shared runtime files. Plugin refresh remains a separate idle-gated operation.

## Managed Git delivery

Use the stable installation CLI (`INSTALL/bin/claudlobby`) and the explicit, separate data root; [Getting started](getting-started.md) defines `CLI`, `INSTALL` and `DATA`. There is one managed installation per host (`INSTALL/src` and `INSTALL/env`), so a framework change moves that installation, never a per-release copy. Edit reviewed authored configuration/library inputs, then run:

```bash
"$CLI" --root "$DATA" host update apply FULL_COMMIT --json
"$CLI" --root "$DATA" host update status --json
"$CLI" --root "$DATA" config diff CONFIG_FINGERPRINT --json
"$CLI" --root "$DATA" host update continue --json
```

`apply` is an effectful operator command that prepares and dispatches the update; it is not a preview-only command. Inspect its retained operation and exact pending render fingerprint. First-fleet setup has the explicit `fleet setup --plan-only` preparation gate described in [Getting started](getting-started.md). After interruption, continue the original operation rather than request another target or resend unknown work.

`config diff FINGERPRINT` reads the exact retained Git render. `config diff` without a fingerprint compares committed owned generated outputs with current disk state; it does not silently adopt pending authored edits. `--bot` limits that current report to the selected bot. Both reports expose metadata/digests, never secret bytes. `content` means bytes/kind/link/tree entries differ; `mode` means permissions alone differ; `unchanged` means the compared owned state matches. Ensured directories own their mode, not mutable children.

New configuration commits before consumer startup. A failed consumer remains visible in update health without revoking the committed configuration. Existing jobs/starts participate in the runtime-use fence; the updater drains admitted work and preserves stopped/disabled consumers and schedules. A canary needs its own installation/data root and native labels, not another fleet inside a production root.

The sealed-release routes (`host activate`, its resume, rollback, adoption and repair/abort modes, `config plan --release` and `migration plan`) are retired as public execution paths; this guide does not describe them as instructions. Historical release/activation/migration evidence remains inspectable with `host releases`, `host status` and `migration status`. Do not delete pending historical evidence to bypass an existing-host conversion or restore requirement.

### While an update runs

Once an update is prepared, its fence turns new native starts away, and the updater waits for the work it admitted before that.

- **A scheduled job that meets the fence skips its tick.** It prints `claudlobby: skipped UNIT: native startup is fenced while the update quiesces consumers` and exits 0, so its `OnFailure=` alert never starts. A resident unit exits 75 and still reads as failed. The native shell that `keepalive.sh` and `start-bot.sh` use exits 75 too, which `keepalive-all.sh` logs as a pause. Maintenance, a busy runtime lock and an unreadable update record still exit 6 (#2741).
- **The updater waits for readers of the runtime lock.** At both of its gates, before it quiesces consumers and before the cutover, it tries every 50 ms and takes the first free moment. After 300 s it posts one notice to the alert chat naming up to three holders as `<unit> (<command> pid <n>, running <s> s)`, and how many more, and goes on waiting: nothing needs running. From then on, `host update status` shows them all as `runtime_wait`. After 6 hours it refuses, naming them, and `host update continue` starts a fresh wait. Each wait, taken or refused, leaves a row in `runtime_waits` (#2739).
- **A stage stops only when its units stop finishing.** Quiesce and restore run for as long as units keep finishing. 1,200 s with none finished stops the stage: `stage_error` ends `interrupted: no unit finished within 1200 s; the stage process was stopped`, and the job's notice says so. A unit that records a restore error has not finished. Every other stage stops after 1,200 s in all. Either way, continue the same operation (#2740).

The update that installs these changes still behaves as before: a job run keeps the coordinator it started with, and its fenced jobs run the old launcher until its publish stage installs the new one. If its restore is cut at 900 s, `host update continue` finishes it under the new coordinator. The next update is the first to skip ticks, wait for readers and count units.

### Converting an existing sealed host

Conversion is a one-time mode of the same updater, not a second activation service. It requires a separately prepared managed installation bound to the existing data root; do not run first-fleet initialization against that root. From its stable CLI, prepare the exact conversion without stopping consumers:

```bash
"$CLI" --root "$DATA" host update apply FULL_COMMIT --convert-sealed --prepare-only --json
"$CLI" --root "$DATA" host update status --json
"$CLI" --root "$DATA" config diff CONFIG_FINGERPRINT --json
```

Review the retained configuration and native consumer scope before `host update continue`. Conversion preserves the existing database, host/fleet/bot identities, native manager domain, enrollment policy and observed stopped/running state. It normally refuses unfinished historical operations or authored fleet/project inputs that differ from their active version. The bounded Linux exception is one upgrade interrupted after producer parking and during per-bot handoff, before configuration or ingest publication: every original parking journal and before-image must match, and current authored inputs must equal that operation’s retained pending plan. Conversion preserves its unknown receipts without replaying them, observes current live sessions mechanically, and leaves quiet bot masks and preexisting masks/stops intact without publishing a higher-priority native definition over preserved masks. A producer mask proved to belong to the completed old pause restores that producer’s original intent after the Git commit: resident services recover their prior activity, timer-owned services stay inactive without catch-up, and timers restore last. Any other unfinished operation or input drift still refuses; never delete evidence to bypass it.

Immediately before stopping anything, the updater rechecks the original evidence under the old admission locks and archives the exact old selector. This closes the old startup path even if the updater exits. The original update then owns continuation; do not restore the old selector by hand or replay unknown handoffs. Later updates use the ordinary Git path. This transition still requires isolated native acceptance on each supported platform before a production rollout; passing source tests is not that acceptance.

### Refreshing a skill's installed copies

A bot's skills are copied into its generated runtime through the same Git configuration publication. Change their authored library source and apply the reviewed Git/configuration update; never hand-edit generated copies. `config diff --bot` includes the committed owned skill tree. Verify the published copy and actual bot behavior separately.

Claudlobby never writes a user-level `~/.claude/skills/<name>/`. A copy there was put
there by hand or by the operator's own sync, so no Git update refreshes it
and nothing reports when it drifts. Every bot whose Claude config directory is
`~/.claude` (the default) loads it too, beside its own copy, and which of two
same-named skills a session runs is not established here. After a skill changes,
compare each such copy with a bot's activated one, then replace or delete it:

```bash
diff -r ~/.claude/skills/<name> <data-root>/<fleet-overlay>/runtime/bots/<bot>/.claude/skills/<name>
```

## The rule as the root CLAUDE.md stated it

The root `CLAUDE.md` stated the rule in this paragraph until #2035; it now keeps a summary. The paragraph predates sealed releases: where it says `generate`, read the Git update's configuration publication.

**Changing a running fleet:** a change reaches a bot by one of several carriers, and they differ on whether they reach a *running* process and whether they survive a restart. **The discriminator is WHEN THE ARTIFACT IS READ, not what type of file it is** (#1310). Read **once at session start** — composed `CLAUDE.md` text, `bot.conf` env, `.mcp.json` — and a canary window exists. Read **on demand, per use** — skill symlinks, hook scripts — and there is **no canary window at all**: the change is live on every bot the instant `generate` writes it. Sorting by artifact type collapses those two, and a composed *skill* is a composed *file*: it was filed under the restart row and has no restart gate, which is how a live-estate `generate` got run without a canary. `settings.local.json` **splits the two**: the session process does not re-read it (strace, two bots), yet a composed `deny` is **enforced on the very next tool call** — measured mid-session, both directions, three bots, Read tool only, `claude 2.1.240` on Linux. So permissions have **no canary window**; *where* that read happens is unestablished. The leaf-manager check-in default is a worked instance of the same rule: the `checkin` protocol's prose is composed `CLAUDE.md` text, read once at session start, so equipping or un-equipping a manager reaches a *running* manager only at its next restart, while the protocol's `requires:`-linked skill symlink and grants are live the moment `generate` writes them.

## Artifact read timing

The read timing below explains why the updater must control publication, including
files that a running session can observe immediately. The historical probes that
follow measured those mechanisms on the earlier mutable installation.

**Sort by WHEN THE ARTIFACT IS READ, never by what type of file it is.** That is
the discriminator, and getting it wrong is not a missing row — it is a *mis-sorted*
one, which is worse. A composed skill is a "composed file" by artifact type, so the
old table promised it a restart-shaped canary window. It has none. Someone reasoning
correctly from that row ran `generate` across a live estate without a canary (#1310).

| Carrier | Read | Reaches a *running* process | Survives restart | Delivered by |
|---|---|---|---|---|
| **Dispatch** (tmux message) | on arrival | yes — but with no delivery-time guarantee | no | sending a message |
| **Composed instructions** (`CLAUDE.md`, `bot.conf` env) | **once, at session start** | **no** — arrives at the next restart | yes | Git configuration update |
| **Composed MCP config** (`.mcp.json`) | **once, at session start** | **no** — servers are bound at startup | yes | Git configuration update |
| **Repo `CLAUDE.md`** (project instructions) | once, at session start | no | yes | a project checkout update (separate from the managed installation) |
| **Composed skills** (`.claude/skills/` symlinks) | **on demand, per use** | **YES — live the instant the symlink lands** | yes | Git configuration update |
| **Hook script** (`claudlobby/_runtime_scripts/*.sh` wired via `PreToolUse` etc.) | on demand, per call | yes — at the next tool call | yes | Git update (framework source) |
| **Composed permissions** (`settings.local.json`) | **reads: not during a turn. enforcement: on demand, per tool call — see below** | **YES for enforcement — live if rewritten during a session. NO for reads** | yes | Git configuration update |

Anything resolved on demand can become live when published. Preparing an update does
not publish it; the updater's publication step now controls that exposure.

### What was measured, and how

| Row | Method | Result |
|---|---|---|
| **Skills — on demand** | `account-relog` entered the live available-skills registry of three bots mid-session, no restart; symlink mtime hours into an unrestarted session | **live at compose time**, confirmed on three bots |
| **Skills — not keepalive-gated** | `.reload-pending` is written only by `reload-fleet.sh:139` (zero hits in `claudlobby/*.py`), and **zero** RELOAD entries appear in any of the 21 bots' `keepalive.log` — yet the skills activated | activation is **immediate and unconditional**, not deferred to pane-idle. A busy bot is exposed the moment the symlink lands |
| **`.mcp.json` — session start** | The MCP server child process of a live session started **4 s after** that session (`23:08:11` vs `23:08:07`) and is **still the original process**, while `.mcp.json` was rewritten **24 h later** with no respawn | **restart-gated**, measured rather than assumed |
| **`settings.local.json` — not re-read in-session** | Two complementary probes on two long-running bots (~20 h uptime, `claude 2.1.240`, Linux): `strace -f -e trace=openat` across a real turn → **0** reads by either session process (positive controls fired: transcript, hook and skill reads; the 3 settings opens per trace were short-lived children walking the cascade). And `/proc/<pid>/fd` → **0** cached descriptors, which closes the `openat`-only blind spot a same-inode rewrite would otherwise hide | **restart-gated for reads.** Enforcement is a separate, still-open question |

**Scope on the `.mcp.json` result:** it establishes that the *server process* is bound
at session start and is not respawned when the file changes. It does not establish
that nothing ever re-reads the file — an explicit reconnect is out of scope here.

### `settings.local.json`: ENFORCEMENT is MEASURED — live, per tool call

**RESOLVED 2026-08-24 — enforcement is live, per tool call.** The probe below was inert for
a reason that is now known, and the same probe run with a *working* rule returns a clean
negative.

| step | action | result |
|---|---|---|
| control | Read tool on a scratch file | **allowed** |
| treatment | `Read(//<dir>/**)` deny added **mid-session** | **DENIED** |
| reverse | the same rule removed | **allowed** again |

Both directions, no restart, on **two bots independently** — ravi, and dara, which ran the
same three cells on itself and verified its restore byte-identical by md5. A third bot, vera,
had already shown a `//` deny composed *at session start* blocking a real sibling read
(#970 comment 5402891714).

**Why the old probe below returned nothing — the part worth keeping.** It tested a rule
shipped 2026-07-18 by #644 P2: a **bare-absolute** path, which #1312 later showed is inert,
because a single leading slash anchors at the settings source rather than the filesystem root.
The rule under test therefore could not block whatever the re-read behaviour was. **The
reasoning in that section was sound; the instrument was broken.** `af940aa` (2026-08-22)
switched the composer to the `//` form, and the probe became able to return a negative.

**Scope — do not read this wider than it was measured.** These cells exercised the **Read
tool** only. They say nothing about `Edit`, `Write`, `Bash`, MCP-tool denies, or how
`--permission-mode auto` treats *unmatched* calls. `Grep`/`Glob` are uncovered by inspection —
no deny entry names them — but were not exercised.

**Pinned to a version, for the same reason the read measurement below is.** Measured on
`claude 2.1.240`, `Linux 6.12.75+rpt-rpi-2712` (aarch64) — one host, one shared binary, all
three bots, so this is three sessions rather than three environments. A release that changes
when or where permission rules are consulted would make this **yes** stale with nothing to
announce it. Re-measure on a major bump.

**This does not overturn the file-read result below. Both stand, and they are complementary.**
The session process performs zero `openat` on `settings*.json` across a turn, *and* a composed
deny takes effect on the next tool call. Those are compatible only if the read backing
enforcement happens somewhere other than the session process — the trace itself observed
short-lived children opening settings files. **Which process performs that read is NOT
established here**, and is the honest remaining gap.

The original record follows, because a probe that could not return a negative is exactly the
failure this document exists to keep visible.

The obvious probe was inert on this estate, so no result was obtained. Recording the
failure rather than sorting the row by assumption:

- **An `allow`-shaped probe cannot discriminate.** Every bot measured runs
  `--permission-mode auto` with `defaultMode: auto`, so an ungranted call is
  auto-approved. Measured on a monitor bot whose allow list contains only
  `Bash(git *)`, `Bash(gh *)`, `Bash(jq *)`, `Bash(python3 *)`, `Bash(rg *)`:
  `cp`, `stat`, `find`, `sed`, `wc`, `ps`, `tmux` and `rm` all ran. On the four
  manager bots the same probe is inert for an *independent* reason — bare `Bash`
  sits at entry 1 of three allow lists and entry 4 of the fourth, so
  `Bash(anything)` is held by construction.
- **A `deny`-shaped probe did not fire either.** Two rules were tested on a live
  session: one predating it by a month (sibling isolation, shipped #644 P2,
  2026-07-18) and one added mid-session. **Neither blocked.** A probe that cannot
  return negative cannot tell "read at session start" from "read on demand".

So on **enforcement** the honest state is **UNMEASURED**, not "restart-gated". The
old table asserted the latter on no evidence, and that is exactly the
confidently-wrong cell this rewrite exists to remove.

**Two questions were being collapsed here, and only one is still open.** *Is the file
re-read by a running session?* and *is a composed `deny` honoured under
`--permission-mode auto`?* are independent. The permission-shaped probe above cannot
answer either, because it cannot return a negative. A file-read probe answers the
first — see the section below — and leaves the second exactly where it was.

**This is tracked separately and is not a docs question.** Whether composed `deny`
rules are enforced at all under `--permission-mode auto` is a live security question
— sibling isolation (#644 P2) is composed onto every bot for exactly this purpose.
Until it is answered, **do not rely on a composed `deny` as a control**, and do not
assume a composed permission change is inert on a running bot either. The missing
instrument is a probe run in a **fresh session with the deny present at start**,
which is the control that separates "not enforced" from "not yet re-read".

**MECHANISM — why a `deny` is honoured in every mode and an `allow` is not.** A
**bare-name** deny (`Write`, `Bash`) is enforced by **removing the tool from the
session's toolset**, not by gating a call. Measured on a throwaway bot with
`deny: ["Bash"]` composed at the project tier *and* bare `Bash` present in the
user tier: the model answered *"I don't have a shell/Bash tool in this session,
so I can't run `date`"* and emitted **zero** `Bash` tool calls. That is the same
shape as the production error a reviewer bot hit reaching for `Write` — *"No such
tool available: Write. Write is disabled for this session, in subagents as well
as here."* A removed tool is absent before any mode logic runs, which is why the
deny holds under `-p` and under `--permission-mode auto` alike; an **allow** is a
gate, and auto opens it (already recorded above — see the inert `allow`-probe
bullet; that finding is **not** restated here). It also explains the shape
asymmetry measured separately on 2026-08-24: a **path-scoped** deny — the rule
actually measured that day was `Read(//<dir>/**)`, not an `Edit` form — *cannot*
remove a tool, because it constrains an argument rather than a capability, so it
must be evaluated per call. That per-call evaluation reaching `Bash` as well as
the named tool is an **inference here and a measurement elsewhere**: see the
evidence trail below. **The practical consequence: a bare-name deny
on one tool does not bind a different tool.** A `Write` deny leaves `Bash`
untouched, so a heredoc writes freely — nothing is circumvented, because at that
layer there is no rule to circumvent.

**Pinned, and measured on a throwaway.** `claude 2.1.240`,
`Linux 6.12.75+rpt-rpi-2712` (aarch64). A disposable project directory and two
isolated `CLAUDE_CONFIG_DIR`s, auth+trust pre-seeded (without the trust key the
composed `settings.local.json` is dropped wholesale). **No production bot was
probed and no real settings file was modified** — `~/.claude/settings.json` was
copied, never edited, and verified unchanged afterwards at 223 allow entries with
`deny` absent; the throwaway tree, its credential copies and its tmux server were
destroyed at the end.

**Evidence trail — an independent later measurement, recorded separately.** The
probe quoted above was run the morning of 2026-09-01 and its session is gone. A
fuller permission ladder was run the same afternoon on the ai-platform fleet and
**does** survive as raw verbatim rows — `2026-09-01-1406-arm-E-grid.psv`, beside
`2026-08-24-permissions-effectiveness-baseline-970.md`, in that fleet's shared
planning and knowledge trees. It is not the same probe and does not carry the
sentence quoted above; it is an independent run that lands on the same mechanism.
Three of its cells matter here. A bare `Bash` deny returned `NO_TOOL` with the
verbatim *"No such tool available: Bash. Bash is disabled for this session, in
subagents as well as here."*, and a bare `Write` deny returned the same shape. A
path-scoped rule returned a **per-call** refusal instead — *"File is in a
directory that is denied by your permission settings."* — confirming the
asymmetry above. And the practical consequence is not inferred there but
**observed with effect**: under a bare `Write` deny, a `python3` heredoc routed
through `Bash` wrote the target file successfully.

**Reachability bound on that trail — read it before treating the citation as
load-bearing.** Those two files are **not present on this Pi** (`claude 2.1.240`,
`Linux 6.12.75+rpt-rpi-2712`, aarch64): a `local/`-wide search returns zero matches
for either name, and zero `.psv` files anywhere in that tree. Run twice, by two
bots independently, each with a positive control proving the search reached the
named tree. What that licenses is narrow — **the rows are unreachable from here,
which is not the same as absent.** Other hosts on the estate were not searched, and
this document cannot tell you which one holds them. A reader elsewhere should
confirm presence before citing these rows; **no reader should treat this paragraph
as the sole support for anything above it.** It is not — the two paragraphs below
reach the same conclusions without it.

**The path-scoped half needs no external file: this document already measured it.**
The RESOLVED 2026-08-24 table above ran a **control** cell (Read tool on a scratch
file → allowed) beside its treatment (`Read(//<dir>/**)` → DENIED). Had that deny
removed the `Read` tool, the control would have failed with it. It did not, so the
rule was evaluated **per call** — the asymmetry above, from this document's own
cells, on two bots, with no dependence on the trail.

**A second route needs no file at all, and any session can run it on itself.**
Compare a session's composed `deny` list against the tools that session actually
has. Both sides have now been observed on this host at `claude 2.1.240`, in two
different bot sessions:

| the session's deny for that tool | is the tool in the session? | observed |
|---|---|---|
| **bare name** (`Write`, `Edit`) | **absent** — not in the active tool list, and not findable in the deferred pool by exact name | navi, 2026-09-14 |
| **path-scoped only** (`Edit(//<dir>/**)`) | **present** — declared and usable on paths the rule does not name | branden, 2026-09-14 |

A gate would leave the tool declared with a schema and refuse at call time; in the
first row there is no schema to find. Same host, same binary, differing only in the
shape of the rule — which is the asymmetry this section claims, reproduced without
either cited file. The removal side is corroborated again by an unrelated
production incident months earlier, where a bot under a bare `Edit` deny hit the
same error shape quoted above for `Write` (*"No such tool available: Edit. Edit is
disabled for this session, in subagents as well as here."*) — reported on that
bot's authority from its own memory, not re-run here. **The second row is presence
only:** it does not re-measure that a per-call refusal fires, which is what the
2026-08-24 cells above already show.

**What this does NOT establish.** Whether a bare-name deny also removes the tool
from *subagents* (the production error string claims it does; not measured here).
Whether `Bash(cat *)` prefix-matches a heredoc redirect. Whether a bare allow
entry bypasses hook pattern checks. And **where** the removal is performed — whether the
toolset is trimmed at session construction, at each request assembly, or somewhere
else entirely. Nothing in this document establishes that, and this measurement does
not close it either.

### Partial result — a different instrument, and what it does *not* answer

The probe above is permission-shaped and inert here. A **file-read** probe is not, and it
needs no fresh session: attach `strace -f -e trace=openat` to a long-running bot's `claude`
process and drive a real turn through it.

**Measured 2026-08-23 on two bots, each up ~20 h:**

```
vera  854 openat lines, 164 distinct paths   session pid: 0 reads of settings*.json
otis  885 openat lines                       session pid: 0 reads of settings*.json
```

Both windows covered a genuine turn — positive controls fired in both: transcript writes,
hook-script reads, skill-file reads. **The three `settings*.json` opens in each trace all came
from short-lived child processes** (open libc, open one settings file, exit — a shell walking
the cascade), never from the session process itself.

So the running session did **not** re-read `settings.local.json` across a full turn, on two
independent bots. That moves the "live?" column off UNMEASURED and onto **no**.

**The obvious hole in an `openat`-only trace was found in review and is closed by a second
measurement, not by argument.** `-e trace=openat` sees a *fresh* open. A session that opened
the file once at launch and kept the descriptor could re-read it with `read`/`pread64` and
produce trace output identical to "never touched again" — and that is live rather than
theoretical here, because the composer's write is `Path.write_text()`
(`claudlobby/composer.py:2717`), which truncates and rewrites the **same inode** rather than
the tmp-then-rename this codebase uses elsewhere. A cached descriptor would therefore see the
new content. Verified same-inode behaviour directly: `write_text` twice, inode unchanged.

So both mechanisms were measured, and they are complementary:

| re-read mechanism | would appear as | measured |
|---|---|---|
| transient re-open | an `openat` in the trace | **0** |
| cached descriptor from launch | an entry in `/proc/<pid>/fd` | **0** (vera 25 fds, otis 26; zero referencing `settings*` or any `.json`) |

Both bots had been running ~20 h with no restart, so a descriptor cached at launch would still
have been present. Credit: the blind spot was identified in review, with the same-inode write
path cited from source rather than inferred.

**What this does NOT establish, stated so the row is not read wider than it was measured:**

- **The window is 75 s and one turn per bot.** Absence within it is not proof the file is
  never re-read. A `/reload`, a settings-file watch, or some other trigger is untested.
- **It says nothing about the enforcement question above.** That question is whether a
  composed `deny` is honoured under `--permission-mode auto`; this one is only whether the
  file is opened. They are independent, and the security question remains open.
- **Launch-read is untested** — both processes were already running when the trace attached,
  so the result is consistent with read-once-at-start but does not demonstrate it.
- **Linux only.** `strace` is unavailable on macOS, and this framework supervises launchd
  fleets as well as systemd ones. The row is unconfirmed there; the `/proc/<pid>/fd` half is
  likewise Linux-specific.
- **Pinned to a version, and this is the one doc whose subject is binary churn.** Measured on
  `claude 2.1.240`, `Linux 6.12.75+rpt-rpi-2712` (aarch64). A future release that adds
  settings file-watching would make this **no** stale with nothing to announce it — the same
  expired-verdict case Mechanism 2 exists to manage. Re-measure on a major bump.

**Practical consequence, which is the reason this was measured:** treat a composed
`settings.local.json` change as **restart-gated for reads** — setting a new *config key*
fleet-wide does not change a running bot's behaviour. That was measured before wiring
`outputStyle`, precisely because a wrong assumption there would have made the feature silently
inert.

**This does not extend to permission rules.** A composed `deny` is enforced on the next tool
call with no restart — see the enforcement section above. Reads and enforcement move on
different clocks, which is why they are two columns and not one.

The cost of each is what makes this worth reading:

**Dispatch — two failure modes, not one.** It can arrive *too late*, and it can
be *erased*. There is no record: a bot that restarts mid-task loses the message
silently. And "queued" is a third state, not a synonym for delivered — a send
queued behind an erroring turn is discarded (#1048), so delivery is conditional
on the current turn ending cleanly.

It could also arrive **partially**, and for a week on this estate it usually did:
a payload over 1 KB handed to one `tmux send-keys` overflowed the pane's 1024-byte
pty input queue, which on macOS FLUSHES rather than drops, so 85 of 180 large
sends arrived with their head — envelope and task id included — gone, all of them
recorded `pane_submitted` (#1493). `pane_send_verified` now writes the payload in
400-byte chunks with a short settle between them (900 until #1876, which found
the receiving TUI frames any read over 800 bytes as pasted text), and `harness/send-size-probe.sh`
measures the result against the recipient's own transcript. The wider point stands
unchanged and is the reason this paragraph is in a lifecycle doc: `pane_submitted`
is a **sender-side inference**, not an observation of arrival.

Sender visibility is **manual, instantaneous, and absent by default** — not
impossible. Polling the recipient's pane at that instant does show it; nothing
shows it to you afterwards.

The race is a property of **delegated guidance**, not of dispatch itself. A
sender who is also the actor has no window to lose. So the precondition is
checkable, and a reader should ask it rather than be told to take care:

> **Am I sending guidance to someone who is about to act, or am I acting myself?**

**Composed artifacts — where the misleading part actually is.** *"It is composed"*
does not mean *"they have it"*, and it does not uniformly mean *"they will have
it after a restart"* either. Which of the two it means depends on **when the
artifact is read**, so this splits three ways:

- **Composed instructions** (`CLAUDE.md`, `bot.conf` env) — read once at session
  start. A bot already running does *not* have it, and will not until it
  restarts. This is the one people mean when they say "composed".
- **Composed MCP config** (`.mcp.json`) — same, and for a concrete reason:
  servers are spawned as children at session start and are not respawned when
  the file changes. Measured — a live session's MCP child started 4 s after the
  session and was still the original process 24 h after `.mcp.json` was
  rewritten.
- **Composed skills** (`.claude/skills/` symlinks) — read **on demand, per use**.
  These are live **the instant `generate` writes the symlink**, on every running
  bot, with **no restart and no canary window**. Confirmed on three bots
  mid-session.

**The trap is the middle case being read onto the third.** A composed skill is a
composed file, so the undifferentiated version of this paragraph promised it a
restart-shaped safety gap it does not have — and that reading was used to justify
running `generate` across a live estate without a canary (#1310).

**Hook script — no canary *window*.** One pull and it is live on every bot at its
next tool call, so staging cannot be applied *around* the change; it has to be
built *into* it, via a dormant flag. Two precedents exist — cited rather than
generalised, because copyable beats abstract. `claudlobby/_runtime_scripts/spin-down-bot.sh:22-27` states
the problem, the solution and the procedure:

> `DEFAULT 0 (dormant): the flags above still parse and the teardown is
> unchanged, but no ledger row is written. claudlobby/_runtime_scripts/ is a shared install, so this is
> what keeps a root-pull from making new behavior live on a destructive door
> without a canary. Arm per fleet in fleet.yaml `env:`, canary on a throwaway
> first.`

The digest hook retired in #2145 P3. Its model-spending switches now live in
`claudna:` composition and take effect at the next session open. The spindown
receipt remains a session-door example; switch carriers are in `host doctor --switches`.

A stronger form of this row — "no staging by construction" — was proposed,
refuted and retracted within the hour (#1078). The surviving claim is about
*where* staging has to live, not whether it is possible.

**Repo `CLAUDE.md` — coverage is positional, not declared.** Every other carrier
reaches a *stated* set of bots. This one reaches whoever happens to be standing
in the right directory, because project instructions load from the cwd upward.

Coverage here is currently **total and currently accidental**: every bot receives
it only because the fleet overlay lives at `local/<fleet>/` *inside* the
compositor repo, so walking up from a bot dir reaches it. That is a property of
the install layout, not of any design decision — and nothing would flag the day
it stopped being total. **That is worse than partial coverage, because partial
coverage is visible.**

Whether a fleet installed *outside* the repo tree would receive it is
**inference, not measurement** — it follows from how project instructions load,
and it has not been tested by anyone. There is no such fleet on this host to
test against.

What actually lives only here, in the form that survives re-checking:

> The dormant-flag **rationale** — the shared-install / root-pull argument —
> appears only in the repo `CLAUDE.md` and in no composition source, so no
> regenerate can produce it.

That is deliberately narrower than "the concept is absent". `library/` *does*
document that things are dormant by default (2 files); only the repo `CLAUDE.md`
documents *why* — that a pull would otherwise make new behaviour live on every
bot at once (`root-pull` and `silently activating`: 0 files in `library/`, 1 each
in the repo `CLAUDE.md`).

**If you re-measure this by grep, expect an ambiguous answer**, and this is why:
`SPINDOWN_RECEIPT_ENABLED` appears in 0 of 21 composed bot `CLAUDE.md` on this
host in that historical snapshot. The old monitor caveat mixed coverage with
rollout rationale; that hook is now retired. Search for rationale, not a flag.

**And take the denominator from the manifest, not the filesystem.** 21 is the
union of bots declared across every `fleet.yaml`; a `find` over `runtime/bots/`
also returns test fixtures nested inside a checked-out copy of this repo, which
are not bots this or any carrier reaches. Enumerate from the manifests and look
each one up — the same rule `claudlobby/_runtime_scripts/selfstart-snapshot.sh` already applies, and for
the same reason: a denominator drawn from directories silently changes when
something unrelated appears on disk.

## The manifest is a fifth input, and it has its own read-time (#1722)

The carriers above answer "when does a change to `library/` or `claudlobby/_runtime_scripts/` reach a
bot". A change to the fleet's **manifest** — `fleet.yaml`, `projects.yaml`, the
mission file — is a different question with the same discriminator: **when is
the artifact read?**

(`generate` below names the composition step; today the managed Git updater performs it.) A manifest is read by `generate`, and only by `generate`. Nothing a bot executes
opens `fleet.yaml`. So a manifest change reaches a bot in **two** steps, and
neither happens on its own:

| step | what it does | until it runs |
|---|---|---|
| `generate` | re-composes `CLAUDE.md`, `bot.conf`, `.mcp.json` from the new manifest | the runtime still reflects the OLD manifest |
| restart | the bot re-reads its composed files | a **running** bot still holds the old ones |

That second row is the one that surprises people: `bot.conf` and `CLAUDE.md` are
read **once, at session start**. A `generate` after a manifest change leaves a
running fleet composed correctly on disk and still *behaving* from what it read
at boot.

**Every `generate` now records what it composed from**, in
`<fleet runtime>/composed.json`: the sha256 of each input, and — when the fleet
directory is inside a git checkout — the branch, commit, whether the tree is
dirty, and whether git is mid-rebase or mid-merge. `bot.conf` carries exactly
one stamp, `FLEET_MANIFEST_SHA256`, and nothing volatile, because `diff`
compares `bot.conf` as exact text and a timestamp there would read as permanent
drift on every bot.

Two rungs read that record:

- `claudlobby --fleet <name> host doctor` → `manifest-provenance`: **warns** when the
  manifest on disk differs from what the running fleet was composed from, and
  names the restart step above.
- `claudlobby --fleet <name> config diff` prints one line first: `manifest: unchanged
  since compose (<when>)`, or `manifest: CHANGED`. Without it, a diff body
  cannot say whether the *runtime* drifted or the *inputs* moved — they look
  identical.

`generate` also warns, loudly, when it composes from a checkout that is
mid-operation or off its default branch: those are the two states in which the
files on disk may not be the ones that were committed.

### What this record can and cannot attribute — read this before trusting it

**`composed.json` is a compose-time SNAPSHOT, not an audit trail**, and the
difference decides which cases it can explain. Its git half describes the
checkout *at the instant `generate` ran*, and the next `generate` overwrites it
unconditionally — no history, no append.

So a manifest **reverted by a git state change** is attributable **only while
that state is still present when `generate` runs**:

| order of events | attributable? |
|---|---|
| vault wedges → `generate` runs → operator investigates | **Yes.** The compose is flagged `interrupted` / off-branch, and `doctor` says so afterwards. This is the shape of the outage this exists for — that stopped rebase persisted for twelve days, so every compose in the window is flagged. |
| vault wedges → operator repairs it (`git rebase --abort` is the reflex) → `generate` runs | **No.** The clean compose records a clean state, overwrites the evidence, and a still-wrong manifest is indistinguishable from a legitimate edit. |

**What still answers after that window** is the change attribution `doctor` and
`diff` print when the manifest has moved, which asks the tree as it is *now*:

- *"the input is modified in the working tree"* — an uncommitted local edit.
- *"the input matches its committed state"* — the content arrived **through
  git**: a commit, a checkout or a branch switch.

It deliberately does **not** claim to separate a commit from a checkout. Nothing
readable afterwards can, and naming what the answer excludes is what lets a
reader go and look instead of trusting a word.

A durable per-generate trail would close the second row and is **not** built
here — tracked as #1732.

## Mechanism 1 — daily selected-fleet plugin refresh

The `reload-fleet` timer calls the installation's `claudlobby fleet reload` at `03:30`. The public command uses the committed configuration’s fleet roster and plugin list, then calls the packaged `claudlobby/_runtime_scripts/reload-fleet.sh` owner.

1. Under a fleet-wide lock (`with_lock`), runs `claude plugin update` for each `FLEET_PLUGINS_REQUIRED` — refreshes the shared host plugin cache (`~/.claude/plugins/cache/`).
2. Drops `data/.reload-pending` on each **running bot in the committed configuration**. It does not send any keystroke itself.

The timer never composes or enrolls native units. Changes to authored configuration, composed skills, or native enrollment go through the same managed Git updater.

Every step is announced in `state/reload-fleet.log` before it runs (`reload-fleet[<fleet>] pid N step: …`), and its output streams there as it runs. A run killed or aborted mid-step raises `reload_failed` naming the step: at once on SIGTERM, SIGINT or SIGHUP, from its EXIT trap, and at the next run for a SIGKILL, which runs no trap (each run keeps a record under `state/reload-fleet.inflight/` until it ends).

Activation is consolidated in `claudlobby/_runtime_scripts/keepalive.sh`: on its next idle-classification tick (each watchdog pass), if `data/.reload-pending` exists, keepalive sends `/reload-plugins` then `/reload-skills` and clears the marker. A bot mid-task is never interrupted by *this* path, and there's no separate broadcaster racing the idle check. Convergence lag is bounded by the keepalive tick interval (on the order of a minute), which is immaterial for a daily reload.

> **The idle gate does not cover composed skill symlinks.** A Git configuration update that replaces a skill symlink makes it visible on demand immediately, even while a bot is busy. This timer no longer writes those symlinks.
>
> The idle gate covers the plugin-cache refresh. Whether `/reload-skills` is required for another purpose has not been established.

Runnable **on-demand** as `claudlobby --root DATA --fleet FLEET fleet reload`; it does not apply a Git update.

**Applies to every running bot, managers included** — live reload is free and lossless, so there's no reason to exclude managers here (contrast Mechanism 2).

**Loud-failure contract:** a failed `claude plugin update` aborts before any marker is dropped and raises `emit_failure_alert` (fleet event + manager tmux nudge + Telegram escalation on critical failure).

## Mechanism 2 — weekly lossless worker restart (binary)

For one explicit operator run, use `claudlobby host update runtime`; the daily
host timer enters that same selected operation before the private updater runs.

`claudlobby/_runtime_scripts/update-claude-code.sh` is **download-only**: it installs the latest `claude` binary daily (`claude-update` job, `04:00`) and does not restart any bot. A failed install raises the same `emit_failure_alert` primitive Mechanism 1 uses — and "failed" is measured on the **staged binary, not on npm**: the install worked only when the binary the fleet launches runs and prints a parseable version, because npm can exit 0 while omitting the platform-native optional dependency and leave a stub that cannot run (#1767). A binary that already cannot run when the job starts raises `binary_unrunnable` before the reinstall and `binary_repaired` if the reinstall fixes it. This is detect-and-alert only: nothing keeps the previous binary, so a failed install still leaves every bot that starts or restarts unable to launch until the host is repaired (#1768).

The binary cannot hot-reload, so it reaches a running bot only via restart. `claudlobby/_runtime_scripts/weekly-worker-restart.sh` (job `weekly-worker-restart`, `schedule: "Sun *-*-* 05:00:00"`) bounces every **worker** bot once a week to pick it up:

```
bot restart          (waits for the exact native session to become idle)
  → native stop      (proves the private endpoint quiet)
  → start-bot.sh     (resumes that exact Claude session UUID)
```

**Managers are excluded** — identified by `MANAGER_TMUX == BOT_ID` (`bot_is_manager()` in `claudlobby/_runtime_scripts/lib-common.sh`) — because a manager's long-horizon orchestration context is the least summarizable. Managers still get Mechanism 1's daily reload; they just never get auto-restarted for the binary. They pick up a new binary on any natural restart or a deliberate human-initiated one. A worker's binary staleness is bounded to ≤1 week.

This job is **composed-but-dormant by default** (`enroll: false` in `claudlobby/system.yaml`) — bouncing workers is disruptive enough that a fleet opts in explicitly:

```yaml
fleet:
  defaults:
    jobs:
      weekly-worker-restart: { enroll: true }
```

**Loud-failure contract:** a worker that doesn't come back raises `emit_failure_alert`, same as Mechanism 1.

## Native conversation continuation

Routine Git updates and planned restarts do not ask the model to write a
checkpoint or invoke a restore Skill. Claude Code continuously persists the
conversation; the lifecycle owner records its exact native session identity in
`data/native-session.json`. A composed SessionStart hook binds that identity to
the configured account, bot working directory, private tmux process and transcript.
A `/clear` hook replaces the UUID only for that same native process. Headless
child sessions cannot replace the bot's receipt.

Before a planned stop, the shared owner waits within its existing bounded wait
for an empty idle prompt, without sending a message. Identity or account drift
refuses immediately. It retains a bounded transcript prefix proof and rechecks it
after the native stop has proved quiet. The updater keeps the same request while
waiting; `host update status` reports its consumer checkpoint state. Deliberately
stopped bots and timer schedules retain their original intent.

`start-bot.sh` resumes only the retained UUID with `--resume`, preserving current
composed model, permissions and MCP flags. `--system-prompt-snapshot off` refreshes
current composed instructions; Claude Code 2.1.257 or newer is required. A matching
process-bound SessionStart confirms continuation before readiness is reported.
Continuation sends neither a restore prompt nor the authored startup prompt. A
new bot instead receives an explicit fresh UUID and its normal startup prompt.

A planned restart can choose a fresh session instead: `bot restart BOT --fresh`,
which the `restart` skill uses. It waits for the same idle prompt and session
proof, then, last before the unit restarts, retains a new UUID in the receipt in
place of the current one. The launch starts it with `--session-id`, as it retries
any launch that never reached SessionStart; the retired process cannot claim it,
and its transcript stays on disk. The restart succeeds only once SessionStart
has confirmed the new UUID. Every fresh UUID, this one included, first submits
`SESSION_RESUME_COMMAND` when `.claude/session.md` is under `RESUME_MAX_AGE_S`
(86400 s) old, then the authored startup prompt. A self restart into a fresh
session also requires a handoff under 300 s old, when it is requested and again
after the idle wait. If the unit restart fails after the new UUID is retained,
the old session runs on and a restart that would continue it refuses; `bot
stop`, then `bot start`, starts the fresh session.

Missing or invalid transcript/session evidence refuses before stopping a running
bot. For a running pre-feature bot without a receipt, the same checkpoint owner
can adopt its exact PID-keyed Claude provider record. It checks the configured
account, cwd, UUID, interactive process and native process start, then rechecks
that binding before recording the ordinary continuation proof. Adopted receipts
retain their provider-process provenance until a validated SessionStart hook
confirms continuation. Later checkpoint attempts re-read the same PID record,
including a same-process `/clear`, and the stop owner performs a final
currentness check before its native action. This does not make external terminal
input atomic with a stop. Darwin binds provider process start to UTC native process time. Linux binds it
to `/proc/PID/stat` start ticks and the exact machine-ID/PID-namespace domain
recorded by Claude. Missing or mismatched kernel evidence refuses. The Linux
record shape has read-only native evidence; a full Linux update trial remains
a separate acceptance step. Existing malformed receipts are never
replaced through adoption. Already stopped older bots remain stopped; selecting
their historical conversation for a later start is outside this slice. No path
guesses the newest transcript or silently falls back to model handoff. Native continuation does not read or change optional handoff
receipts; pending, unknown, malformed or unreadable receipts do not block it.
`bot handoff` remains an optional explicit tool with its own no-resend protection,
rather than a prerequisite for routine updates. Crash recovery can resume only what
Claude durably saved; it does not claim interrupted work completed or replay
unknown tool effects.

## Relationship to PR #399

PR #399 added `claudlobby/_runtime_scripts/update-claude-code.sh` with a daily **fleet-wide bounce** — every bot, managers included, restarted once a day whenever the binary changed. That bounce is the literal daily-reset context-loss pain this lifecycle removes. It is **retired**: `update-claude-code.sh` is now download-only. Its daily binary download **survives unchanged**. The bounce it used to perform is replaced by the weekly worker-only lossless restart (Mechanism 2) plus natural restarts — both now resume-on-start instead of cold-starting.

## Reference

| Script | Role |
|---|---|
| `claudlobby/_runtime_scripts/reload-fleet.sh` | Mechanism 1: selected plugin update + mark reload-pending |
| `claudlobby/_runtime_scripts/update-claude-code.sh` | Daily binary download only (no restart) |
| `claudlobby/_runtime_scripts/weekly-worker-restart.sh` | Mechanism 2: weekly worker-only lossless restart |
| `claudlobby/_runtime_scripts/keepalive.sh` | Consumes `data/.reload-pending` at each idle tick; also the crash-restart entrypoint |
| `claudlobby/_runtime_scripts/start-bot.sh` | Starts an explicit native UUID or resumes its exact retained transcript with current composition; startup injection is fresh-session only |
| `claudlobby/native_session.py` | Process-bound native metadata and bounded transcript proof shared by update and restart |
| `claudlobby/_runtime_scripts/pre-stop-handoff.sh` | Optional explicit model handoff delivery; not used by routine continuation |

Full design history, decision forks, and rationale: `documentation/plans/archive/2026-06-14-fleet-skill-plugin-update-lifecycle.md`.
