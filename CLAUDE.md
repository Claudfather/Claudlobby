# claudlobby

Compositor for Claude Code agent fleets. Transforms `fleet.yaml` + `library/` into runnable bot directories with isolated identities, MCP servers, skills, and systemd/launchd supervision.

**North star:** Trivial to run a fleet of distinct, cooperating bots on cheap hardware — and to point that fleet at a goal (fleets know the mission they serve, pick work that advances it, and close it at each project's declared rigor).

**What that is for:** specialist agent teams that take on the work a team of people would otherwise do. Such a team **communicates in plain terms to the human while keeping the rigor behind it intact** — simplified for the reader, never simplified in the doing (`library/principles/simple-outside-rigorous-inside.md`). It works toward a business goal with enough autonomy to parallelize its own workstreams and fill its own idle time, and sustains that over days rather than a session. **Long-running is the precondition for the rest**: a fleet that cannot survive a restart with its context intact cannot hold a goal. Tracked as #974.

**New here?** See [`documentation/getting-started.md`](documentation/getting-started.md) for the clone-to-fleet walkthrough and [`documentation/fleet-yaml-schema.md`](documentation/fleet-yaml-schema.md) for every config field.

## Ecosystem boundary

Claudlobby is the stack's **composition system**: it turns `fleet.yaml` into wired bots —
identities, plugins, env, permission grants, supervision — and owns fleet *policy* (what each bot
may do, including vault writer topology). Engineering-workflow behavior is clauDNA's; the durable
knowledge corpus is Claudron's. The local rules:

- **Skills here are fleet operations.** `library/skills/` operate the fleet itself (dispatch,
  restart, pulse) — runtime content that happens to use the skill format. Engineering-workflow
  skills belong in clauDNA.
- **Durable knowledge lives in the vault.** `library/` composes context into bots; it is not the
  corpus. Learned-the-hard-way content accrues through the Claudron door (`/claudna:capture`),
  not as new library files (see `library/CLAUDE.md`).
- **Consume siblings by contract, never by assertion.** Wire what is shipped (the `claudron` CLI
  door; the `claudron_compat.py` floor); never validate or warn about a sibling surface that does
  not exist at the pinned version. The canonical vault env var is `CLAUDRON_VAULT_PATH` (Claudron
  CLI contract).
- **Placement test** (one line): does it *wire, grant, or supervise*? → here. Behavior → clauDNA;
  knowledge → the vault. Full algorithm: Claudron repo,
  `documentation/plans/2026-07-20-claudfather-boundary-separation.md` §10.3.

## Architecture

```
fleet.yaml          →  claudlobby generate  →  runtime/bots/<name>/
library/                                         ├── CLAUDE.md      (composed instructions)
  expertise/                                     ├── bot.conf       (env vars, sourced at startup)
  skills/                                        ├── .mcp.json      (MCP server config)
  mcp/                                           ├── .claude/       (settings.local.json, skills/ symlinks)
  guardrails/                                    ├── *.service      (systemd unit, Linux)
  protocols/                                     ├── *.plist        (launchd unit, macOS)
  integrations/                                  ├── memory/        (bot-owned persistent state)
  resources/                                     ├── data/          (bot-owned data — mutable, never regenerated)
  lessons/                                       ├── tools/         (composited scripts — generated, never hand-edited)
  principles/                                    ├── logs/          (bot log files)
  permissions/                                   └── projects/      (git checkouts, gitignored)
  post_actions/
  tools/
voices/
templates/claude.md.j2
```

The compositor reads `fleet.yaml` (which declares bots, their expertise, skills, MCP servers, guardrails, etc.) and assembles each bot's directory from the shared `library/`. The template `claude.md.j2` owns all top-level structure — library files supply slot content.

### Key concepts

- **Expertise** — Role definitions (e.g. `software-engineering`, `orchestration`, `code-review`). Each bot gets one or more. This is who the bot is.
- **Skills** — Composable slash-command packages in `library/skills/<name>/SKILL.md`. What the bot can do.
- **MCP fragments** — JSON wire configs in `library/mcp/` with `${ENV_VAR}` placeholders. Never real tokens.
- **Guardrails** — Safety rules composed per-bot (e.g. `no-push-main`, `snowflake-read-only`).
- **Protocols** — Reusable workflow patterns (dispatch, review-flow, context-management).
- **Tools** — Composited bot scripts in `library/tools/<name>/` (`tool.yaml` + Jinja template), rendered per-bot into `<bot_dir>/tools/` (0755) with compose-time params; secrets stay runtime env reads. See `library/tools/README.md`.
- **Plugins** — Claude Code plugins installed fleet-wide. `claudna@Claudfather` is a built-in default; extras via `fleet.plugins.additional`. Auto-installed on bot start.
- **Voices** — Optional personality overlays from `voices/`.

### Runtime model

Bots run as supervised processes: systemd user units on Linux, launchd LaunchAgents on macOS. Each bot lives in its own tmux session on its **own** tmux server (a private `-L <socket>` == `BOT_SERVICE`), so one server's death drops only that bot, never the whole fleet. The manager dispatches work via the socket-aware `lib/dispatch.sh` helper (which resolves the worker's socket); workers report back via `lib/report-back.sh`.

**Changing a running fleet:** a change reaches a bot by one of four carriers — dispatch, composed file, hook script, or this repo's `CLAUDE.md` — and they differ on whether they reach a *running* process and whether they survive a restart. A composed file does not reach a bot until it restarts; a hook script is live on every bot at its next tool call with no canary window. See [`documentation/fleet-update-lifecycle.md`](documentation/fleet-update-lifecycle.md) before assuming a merged change is in force.

Key lifecycle scripts in `lib/`:

| Script | Purpose |
|--------|---------|
| `start-bot.sh` | Launches a bot's tmux session with env vars from bot.conf |
| `spin-up-bot.sh` | Idempotent: enrolls bot as supervised service, then starts |
| `spin-down-bot.sh` | Inverse of spin-up: guaranteed teardown/reaper for canary/throwaway bots — removes supervision, kills the tmux server, drops the bot's fleet-state key; `--purge` also removes the bot dir. Emits a `bot_teardown_started` receipt (action/actor/fleet/bot_dir/expected_return/reason) to the **fleet** ledger so it outlives `--purge`, written *before* the legs run so a crash mid-teardown still leaves a record. **Dormant by default** — `lib/` is a shared install that cannot be staged per-bot, so a fleet arms the receipt with `SPINDOWN_RECEIPT_ENABLED=1` in its `fleet.yaml` `env:` rather than a root-pull silently activating new behavior on a destructive door. Also the lever for RAM pressure — check for uncommitted WIP first: `systemctl --user stop` does not hold (the 60s keepalive walks a stopped bot back up within a minute), and de-enrolment is the one thing keepalive cannot undo; reconcile surfaces the result as `unsupervised-down` (#828) |
| `keepalive.sh` | Per-bot watchdog — restarts if tmux session dies |
| `keepalive-all.sh` | Fleet-level watchdog — runs keepalive for all bots |
| `reconcile-fleet.sh` | Audits supervision state: healthy, orphan, missing, unsupervised-down, unbound |
| `report-back.sh` | Worker → manager structured reporting via tmux |
| `fleet-state-update.sh` | Atomic state/fleet-state.json updates with flock locking |
| `workstream-update.sh` | Single-writer mutator for the per-fleet `workstreams.json` registry (open/progress/renew/block/close/prune); reads go through `claudlobby workstreams` |
| `pre-stop-handoff.sh` | Graceful context handoff before service stop |
| `lib-common.sh` | Shared helpers: OS detection, bot.conf loading, safe mktemp, and `pane_send_verified` — the ONE send-keys/settle/Enter/verify-retry primitive every keystroke injector routes through (#763). Its verify anchors to the last prompt-glyph line, never a fixed `tail -N`: Claude Code draws border, hint and mode footer *below* the input line, so a fixed depth either never reaches the prompt (the retry silently dies) or reaches up into the transcript (a submitted command is still visible there, so Enter re-fires at an idle prompt). It also waits for the input box to exist before sending at all (`pane_await_input_box`, #860): a send issued before the TUI draws is lost outright, and the glyph-anchored verify reads that loss as a *clean send*, because a glyph-less pane holds nothing unsubmitted. Bridge readiness is not a proxy for the box (poller up at 3-9s, box drawn at 10-19s). Readiness is decided by a **pair** of signals rather than one predicate — the current frame plus a verdict latched before the keystrokes went out — since a glyph-less pane means either mid-turn or never-drawn, those need opposite responses, and a capture of the alternate screen buffer can only ever describe the present. Only the unambiguous `never-drawn` verdict changes behaviour, and its repair resends the whole payload, not a bare Enter. Derivation, the rejected discriminators, and the stated bounds live beside the verdict constants in the source. Also `install_error_trap`, which arms `set -E` on behalf of every caller (#844) — a bare ERR trap is not inherited by shell functions, and `lib/` does nearly all its work in functions, so without errtrace the fleet's error breadcrumb only ever saw top-level failures. A script that hand-rolls its own `trap … ERR` rather than calling the helper is not covered. Also `declared_bots_strict` and `boot_start_class` (#1043) — the first is the LOUD roster door that must never be swapped for `parse_fleet_bots`. **The discriminator is what the caller does with an EMPTY result, not whether it is an "action" or a "measurement"** (#1146): a soft-fail is right wherever empty means *do nothing*, and wrong wherever empty **licenses a write or a delete**, because there absence of evidence is being read as evidence of absence. The old action/measurement framing endorsed a live destructive defect — a prune is an action, so the rule blessed it, and a CRLF-drifted sibling manifest let one fleet delete another fleet's live state row at rc 0 (#1143). Note also that `bot_in_fleet` **inverts** an empty roster into "every directory is declared", so six callers reach their work through a predicate that turns *do nothing* into *do it to every bot on the host*; the second types how a session came to life and **denylists the invariant rather than allowlisting the variant** — startup payloads are authored per bot and vary without limit, while the channel injection and the `tool_result` record have exactly one shape each, so those are matched and a payload is whatever is left |
| `log-rotate-fleet.sh` | Fleet-wide log rotation |
| `log-rotate.sh` | Single-bot log rotation |
| `git-pull-all.sh` | Pull all repos in a bot's projects/ directory |
| `tg-post.sh` | Bash helper for posting to Telegram |
| `disk-monitor.sh` | Daily disk-usage check, FLEET ALERT past threshold — runs as the `disk-monitor` host job |
| `fleet-memory-check.sh` | Daily fleet RSS vs available-RAM check, FLEET ALERT past reserve floor — runs as the `fleet-memory-check` host job |
| `transcript-digest.sh` | SessionEnd hook — distils each finished session into one structured JSONL row via a cheap Haiku pass (#785 Phase A ingestion; the ai-platform monitor reasons over digests, never raw transcripts). Two-stage input reduction: distil to user/assistant text + tool *names* (drops tool results/attachments — 36× on a real transcript), then tail-cap. Schema reuses clauDNA `capture`'s session-mode rubric (context/worked/failed/would_change/reusable) — shared schema, not shared mechanism; SessionEnd is not clauDNA's to hook (clauDNA #203). A session below the qualifying gate still gets a `skipped` row at zero model cost, distinct from an `ok` row with empty fields (the model finding nothing notable). Non-blocking: every failure path still writes a row and exits 0. Composed fleet-wide via `system.yaml` `defaults.hooks.SessionEnd` but **dormant by default** — a fleet arms it with `SESSION_DIGEST_ENABLED=1` in its `fleet.yaml` `env:`, so rollout is per-fleet rather than estate-wide on the next generate |
| `orphan-browser-reaper.sh` | Daily reap of browser processes a dead automation task orphaned (#807) — matches on the `comm` basename, never a cmdline pattern; "orphaned" means the parent is init *or a subreaper* (systemd `--user` adopts what a bot session orphans, so `ppid == 1` alone misses the common case); reaps the whole descendant tree, FLEET NOTICE on reap. `--dry-run` reports without killing or notifying. Runs as the `orphan-browser-reaper` host job |
| `bench-cold-start.sh` | Cold-start timing benchmark — logs CSV rows to `bench-results.log` (no automated regression detection) |
| `check-npx-cache.sh` | Verify npx package cache state for MCP servers |
| `sprint-trigger.sh` | Schedule-driven autonomous sprint nudger |
| `briefing-trigger.sh` | Fire a bot's scheduled briefing as a real slash command — run by the composer-generated per-(bot,slot) briefing timer |
| `creds-check.sh` | Credential validation for fleet secrets |
| `bot-sweep-cron.sh` | Periodic bot sweep via cron |
| `code-audit-sweep.sh` | No-LLM rolling code-audit selector — picks the stalest repo via GitHub `auto-audit` issues, hands off to the owner bot's session — runs as the opt-in `code-audit-sweep` fleet job |
| `install-code-audit-sweep.sh` | Code-audit-sweep timer enrollment (launchd, thin wrapper) |
| `install-code-audit-sweep-systemd.sh` | Code-audit-sweep timer enrollment (systemd, thin wrapper) |
| `install-bot.sh` | Bot service enrollment (launchd) |
| `install-bot-systemd.sh` | Bot service enrollment (systemd) |
| `install_fleet_timer.sh` | Generic fleet/host timer enrollment (systemd) — copies composed units + enables |
| `install_fleet_timer_launchd.sh` | Generic fleet/host timer enrollment (launchd) — copies composed plist + bootstraps |
| `install-keepalive-systemd.sh` | Keepalive timer enrollment (systemd, thin wrapper) |
| `install-cron.sh` | Cron job installation |
| `install-creds-check-systemd.sh` | Creds-check timer enrollment (systemd, thin wrapper) |
| `install-fleet-pulse-systemd.sh` | Fleet-pulse systemd timer enrollment (systemd) |
| `setup-system` | Setup backbone: host prereqs + system.yaml host-job enrollment (cross-platform) |
| `setup-fleet` | Setup backbone: per-fleet apply+enroll — default jobs (dormant opt-ins skipped), atomic legacy-keepalive swap (enable-new → verify → disable-old), bots (skips healthy), reconcile; root mode when invoked without a fleet |
| `setup-fleets` | Run setup-fleet for every fleet on the host |
| `bot-vitals.sh` | Bot vitals collection for observability |
| `fleet-pulse.sh` | Fleet-wide heartbeat / status monitoring |
| `tail-fleet.sh` | Fleet-wide log tail + grep filter |
| `ci-health-check.sh` | Pre-push CI health canary for target branch |
| `data-sweep.sh` | Weekly per-bot data/ ephemeral purge — only `events/*.jsonl`, vetted text-log names, and `*.bak` age out (30d default, fleet-overridable); durable files are never swept — runs as the `data-sweep` fleet job |
| `dispatch.sh` | Dispatch helper for manager → worker |
| `dispatch-task.sh` | Task dispatch helper |
| `dispatch-overdue.py` | Finds overdue dispatches — the matcher behind the `fleet-pulse.sh` watchdog (age-capped via `DISPATCH_OVERDUE_MAX_AGE_S`). Also owns the two #835 doors: `--open-task` (the id `report-back.sh` resolves when a worker omits `--task`, so an id-less report still closes its dispatch) and `--orphans` (past-deadline rows whose worker respawned after dispatch — inert for alerting because the session holding the id is gone, but listed, since work lost to a restart is actionable). `--open` (#904) is the read door's list form: every still-open id'd row, **deadline-blind**, so it is a strict superset of `--all`'s and "carrying three tasks, none late yet" becomes expressible — a state no other mode could say. `--open-task` is now literally its head rather than a second loop, because a resolver that could hand back an id the list does not contain is the same desync class `_terminal_reported_ids` exists to prevent. A row missing `expected_by` reports it as `-` and is still listed: filtering it out would hide work the resolver can still close |
| `who-reviewed.py` | Attributes a PR's reviews to the bot that wrote them (#1155 follow-on). The fleet shares one GitHub PAT, so **every** review on every PR reads `chrisrogers37` — the identity is not recorded on GitHub's side at all, so querying GitHub harder can never recover it. It lives only in the per-fleet report-back ledgers, and recovering it is a JOIN that every reader was hand-rolling: two people did it independently on the same PR one evening, and one first concluded from the review PROSE that the reviewer was on another fleet and told that fleet's manager so, when the reviewer was a bot they had dispatched themselves. Two rules, both from how the manual version actually worked: **match `pull/<N>`, never a bare number** — the same real row carries `pr_url: ".../pull/1046"` and `summary: "Request Changes on #1046"`, and only the first may count, since bare digits collide with task ids and have misattributed a PR here before; and **match the timestamp with a tolerance**, because the ledger row lands *after* the review posts (the two real #1046 pairs were +12s and +8s, so an exact join finds nothing). Bounded with a negative lookahead — and `\b` would have been equally correct, contrary to an earlier version of this row: measured, `pull/1046\b` REJECTS `pull/10461`, because the missing boundary makes `\b` fail rather than match. The two differ on exactly one shape, a trailing non-digit word character (`pull/1046a`), where the lookahead is the *more* permissive; no PR URL produces it, so the choice is a wash and the lookahead stays only because it names the real intent (the next character must not be a DIGIT). Pinned by `test_the_two_boundary_forms_are_equivalent_on_pr_shaped_input`, because a mutation swap between the two comes back green — an **inert mutant**, not weak tests, and the two diagnoses need opposite responses. The qualified matcher's lookbehind excludes word characters **only, deliberately not `/`** — the stricter form never matched a single genuine `pr_url`, since the owner in a URL is always preceded by one, silently downgrading every real hit to unqualified and erasing this-repo-versus-another-repo. Unmatched is `UNKNOWN` and multi-matched is `AMBIGUOUS`, with **no nearest-wins tiebreak** — a tiebreak is a guess wearing arithmetic, and a wrong attribution is worse than none: none sends a reader to look, wrong makes them act, which is the original failure. Bot-name collision across fleets (#526) therefore reports AMBIGUOUS rather than resolving. **It does NOT make attribution arrive unbidden** — a reader who never thinks to ask is not helped by a door they do not open; it only makes the answer cheap once asked. Standalone stdlib module (`dispatch-overdue.py` precedent); `--reviews-json` is the seam that keeps the join offline and unit-testable. Wrapped by `tests/test_who_reviewed.py` |
| `telegram-instant-ack.sh` | Telegram instant acknowledgment for inbound messages |
| `fleet-utilization.sh` | Fleet utilization rollup — per-bot busy/idle % |
| `transcript-usage.py` | Per-session token accounting from Claude Code transcripts — sums the four `message.usage` components (flat, never `iterations[]`) over assistant turns, splits sidechains, emits `protocol_sensitive` + `cost_weighted_total` axes and an outbound-comms estimate (`--comms-share`). Prize-sizing instrument for the token-efficiency protocol (#716/#729); #717 extends it |
| `ab-comms-eval.sh` | #729 stage-C A/B comms-eval harness (scaffolding) — also hosts the `--experiment coverage-honesty` mode (#866): guardrail-clause variant axis with a run-blocking composed-diff-equals-clause assertion, headless real cells — runs the same comms-heavy battery against two identical bots differing only in `protocols: [token-efficiency]`, composed by real `generate`, and sums the two gated axes (`protocol_sensitive` + `cost_weighted_total`) per variant via `transcript-usage.py`. Task content + quality rubric are F2 stubs; `--dry-run` is CI-safe (synthetic transcripts, zero model calls); real mode (`AB_EVAL_REAL=1`) is refused pending F2 + P1. Wrapped by `tests/test_ab_comms_eval.py` |
| `ab-comms-verdict.py` | #729 stage-C pass-bar / verdict for `ab-comms-eval.sh` — pairs per-rep deltas by task, runs a seeded bootstrap CI on the median relative reduction per axis, and applies the cost-weighted-co-primary, per-task, INCONCLUSIVE-never-PASS bar (no F2-ratified threshold → INCONCLUSIVE by construction). Standalone stdlib module (`dispatch-overdue.py` precedent) so F2 extends the threshold + scorer in a unit-tested place |
| `ab-recoverability-scorer.py` | #881 A2 recoverability axis — the anti-lossy guard on the routing eval. Per message pair, scores whether a standard follow-up returned the compressed-out detail **in full and unre-summarised** (a shorter summary of the summary is a FAIL — A2 is the empirical gate on `token-efficiency`'s "an explicit request is never re-summarized"). Two tiers, separated so the cheap half runs without spend: STRUCTURAL (zero model calls, no tunables) contributes one sound one-way refutation — a follow-up no longer than the message it expands cannot have returned the detail, so it refutes without a judge and never confirms; SEMANTIC (`in_full`, `unre_summarised`) needs a judge and is consumed from `--judgements`, never called here, so the spend seam stays in the harness behind its existing keys. An unjudged pair is UNSCORED (`None`), never a failure — a missing judge must not manufacture a regression. Emits a comparable per-(task,variant) recovery rate plus the control-vs-treatment delta and **no verdict**: A1's bar is ratified at 25% CI-low, A2's is must-not-degrade and unratified, so inventing one here would pre-empt a ratification that has not happened. Standalone stdlib module (`ab-comms-verdict.py` precedent); `--dry-run` drives the real scoring logic on synthetic inputs at zero cost |
| `ab-recoverability-judge.py` | #881 A2 semantic judge — fills the slot `ab-recoverability-scorer.py` leaves open, and does not redefine its contract: judge emits judgements, scorer consumes them, **neither decides anything**. Per pair, scores whether the follow-up returned the withheld detail `in_full` and `unre_summarised` — two independent booleans, because a response can be complete and still re-summarised, which still fails. Fails **closed**: an unreadable verdict is omitted, never guessed, so a flaky judge degrades coverage (surfaced as UNSCORED) instead of manufacturing a result. Real calls are two-key (`--real` + `AB_JUDGE_REAL=1`) and deliberately NOT the matrix's `AB_EVAL_REAL`, so a judge run can never ride in on an eval gate; `--dry-run` drives the real prompt + parse path at zero cost. `--calibrate` scores it against hand labels and reports agreement **per boolean, never pooled** — `in_full` is the easy axis, `unre_summarised` is the one the anti-lossy guard rests on, and a blended figure hides a coin flip on the second. Balanced 5/5 fixtures keep a constant-answer judge at 50% |
| `ab-coverage-verdict.py` | #866 coverage-honesty A/B analysis — pre-registered endpoints over the experiment rows: paired relative deltas (length primary, frozen-regex verification density secondary), seeded bootstrap CI imported from `ab-comms-verdict.py`, manipulation-check disclosure rates, INCONCLUSIVE-first decision rule; zero-baseline pairs and invalid rows disclosed, never silently dropped |
| `validate-bot-change.sh` | Empirical validation harness for bot behavior changes |
| `coldstart-harness.sh` | Mechanical half of the cold-start simulation — `prepare` exports a history-free tree via `git archive` (never a clone) and snapshots the host; `reap` tears down everything the run created; `status`/`transcript` inspect and harvest. Design is **instrument-and-reap, not fence**: a blind run cannot be told "do not enroll supervision" without revealing that supervision exists, and the escape is usually the finding. Reap only ever removes units/sockets/processes *absent from the pre-run snapshot*, so a production fleet on the same host is invisible to it; its state lives outside the exported tree because the tree is what gets deleted. Driven by the `simulate-cold-start` skill — the cold arm itself must be a fresh interactive session, since a subagent inherits the parent's skill registry and can never invoke `/setup` |
| `freshbox-boot-gate.sh` | #644 P4 real-boot gate — boots a scoped bot on a fresh empty `CLAUDE_CONFIG_DIR` (auth+trust seeded before first contact) and asserts the composed perms hold: clean boot, zero prompts, transcript ⊆ allow. Gated job (deps absent → skip); wrapped by `tests/test_freshbox_boot_harness.py` (opt-in `FRESHBOX_REALBOOT=1`) |
| `boot-strand-sampler.sh` | #843 real-boot STARTUP_PROMPT strand sampler — N genuine `claude` boots of a disposable MCP-parity probe through the real `start-bot.sh` injection path (freshbox sibling; `validate-bot-change.sh` stubs the binary, so it cannot measure this). Clean = user-role marker record in the session transcript (pane-independent ground truth); strand = deadline + `pane_holds_unsubmitted` (last-glyph anchored, #843); anything else = `other`, never counted clean. Probe is a declared tokenless canary (`EXPECT_NO_TOKEN=1`) — injects earlier than production's poller-gated READY, at-least-as-hard on the send race; per-boot process trees evidence parity. Summary via `boot-strand-summary.py` (stdlib): exact Clopper–Pearson interval printed next to the n=4 pre-fix baseline. Wrapped by `tests/test_boot_strand_sampler.py` (real smoke opt-in `BOOT_SAMPLER_REALBOOT=1`) |
| `selfstart-snapshot.sh` | #1002 pre-registration measurement — how many bots SELF-STARTED after a host boot, recorded rather than inferred. Run BEFORE any rescue, which is what removes the trade between a clean experiment and leaving bots stranded (#1043); classifying by the fast/slow gap only separates if nobody rescues quickly. Emits **two counts with opposite blind spots** and never silently prefers one: RAW (assistant records in the newest transcript) scores a service-failed bot as a self-starter, because `ls -t` returns its pre-crash file; FILTERED (records after the boot instant) drops a real self-starter whose timestamps fall inside the stale-clock window this RTC-less host opens at every boot. **A disagreement is itself the stale-clock signal**, so the clock assertion is measured — `uptime -s` (true boot) against the journal boot record (what the clock *said*) — not assumed, and it gates how a disagreement is read: SANE resolves it, STALE and UNKNOWN fail closed to ADJUDICATE rather than picking a side. The counts are deliberately **independent in file selection** (RAW reads one file, FILTERED reads all), which is also what catches the tail case where a stale clock leaves the fresh file with an older mtime than the pre-crash one and `ls -t` picks wrong. **Denominator is the union of bots DECLARED across every `fleet.yaml`**, never directories on disk: a bot that never wrote a transcript vanishes from *both* lists under a per-transcript loop, so 6 of 21 silently becomes 6 of 19 and the baseline stops being comparable — it prints as a strand instead, and an undeclared leftover directory is reported but never counted. An unparseable manifest is **fatal, not skipped** — that is the same silent-denominator bug one layer up, and it is why `composer.py::_fleet_bot_count()` must NOT be copied here: soft-failing to 0 is right where an empty result means *do nothing*, and wrong here, where it licenses a **wrong number** that will be compared against a baseline and believed (the general rule, and the correction to the older action/measurement framing, is #1146). **Every fatal condition owns its own override** (`SELFSTART_ALLOW_PARTIAL`, `SELFSTART_ALLOW_DUPLICATE_NAMES`), each with its own banner and headline stamp — a single flag honoured by two unrelated checks is how the silent denominator gets back in through the door built to make partiality loud, which is exactly what it did before review caught it. Coverage is also **asserted positively at the end** (every declared bot produced a row, every row got a class; exit 4 + `INCOMPLETE` stamp otherwise), because with `set -e` gone there is no crash left to infer completion from — the two-thirds page just exits 0 instead of erroring. **It also refuses to answer too early**, which is the worst-shaped failure available to it (#1050): a bot whose `ExecStartPre` boot rung has not elapsed has not been *launched*, so it cannot have written anything and is `NOT-YET-DUE` — a separate class from stranded, and one that dominates every negative verdict including the pre-crash-file case. Counting those as strands measures the elapsed clock: at boot+20s the 3s-stagger ladder has released only 7 of 21, and the page renders `0 of 21` against a 6-of-21 baseline — a catastrophe that has not happened, read mid-incident by someone deciding whether to intervene, arguing for exactly the panicked mass-restart the standing posture exists to prevent. So the headline **stops claiming a result at all** (refuse, not caveat), keeps the provisional number visible but labelled, and names the instant to re-run at. The hard gate is *derived* (per-bot rung read from the composed unit, never a hardcoded stagger, since that is a composer constant that will move); the softer first-turn allowance is *stated with its provenance* and overridable, because 17–34s at load 31 is a measurement rather than a fact. A bot whose rung cannot be read is disclosed, not silently gated. **Presence of a record cannot see a RESCUE, and that is the bigger hole** (#1043): a carried bot and a woken one have identical records, so both counts are blind to it by construction — this host read 7 of 21 before a rescue and 19–20 of 21 three minutes after one, both stamped `result valid: yes`. The defect was never the number; it was the validity claim printed over it. So the first post-boot user record is **typed before any instant is compared** (`boot_start_class`), and a run a `fleet_rescue` receipt covers **refuses with its own exit code 6** — precedence 4 > 6 > 5, since re-running fixes early and can never un-contaminate a boot. The receipt's **two independent facts are both used** and a real one disagreed with itself, so named-as-rescued against a boundary that says clean is refused BY NAME, never resolved toward either; a receipt that declares itself recorded after the fact has a *typed* stamp, so the comparison is suppressed while its name list still stands. The boundary always comes from the receipt, **never from a gap in the data** — the separation the populations showed was an artifact of when the rescue landed, and a rescue at boot+90s closes it. Two classes fall out that no liveness signal can see: `INBOUND-WOKEN` (a human messaged it, so it runs real work and reports normally while never having started — the bot that ran the 2026-08-08 measurement and repaired twelve others was itself in this class) and `PARTIAL` (a boot is TWO sends, and asserting merely that *something* startup-shaped arrived passes a bot whose composed prompt was still unsubmitted 39 minutes later, or never arrived at all). **The prior 6-of-21 figure is printed as NOT COMPARABLE rather than as a target**, because it was measured by presence-of-record and counts bots this classifier does not; it must be re-derived before anyone claims better, worse or flat. It sources `lib-common.sh` for exactly two doors — `declared_bots_strict` and `boot_start_class` — since a private copy of a shared predicate is how a fleet-wide fact quietly forks; it still calls no CLI, touches no network and needs no bot up. **Sourcing re-arms `set -e`** (lib-common.sh sets it at source time), which aborted the sweep on the first run after wiring, so `set +e` is restored immediately and deliberately — `set -e` is absent here so a mid-run failure cannot print a two-thirds page that reads as complete. Wrapped by `tests/test_selfstart_snapshot.py`; the two roster doors are gated by `tests/test_roster_doors.py` |
| `rehearse-keepalive-swap.sh` | Phase 6 gate 1 — rehearse the atomic legacy-keepalive swap on a throwaway fleet with real 60s timers; journal-derived no-gap assertion |
| `update-claude-code.sh` | Daily Claude Code binary download (download-only; no fleet bounce) — runs as the `claude-update` host job (system.yaml `host.jobs`, enrolled by `setup-system`) |
| `notify-behind.sh` | Daily source-currency **report** (notify-only, never pulls) across every framework checkout, not just claudlobby (#1009) — the watched set comes from `discover_framework_checkouts`, not a path list. Reports two distinct distances: behind the newest **release** (`source_behind`, fixable by pulling) and on the newest release while **main has moved** (`source_release_gap`, fixable only by cutting one). Conflating them is what hid a 16-commit-stale Claudron carrying two data-integrity fixes — it was on the newest tag, so every release-shaped check read green. Repos with no tags track their default branch instead, because that is how they ship — runs as the `notify-behind` host job |
| `update-siblings.sh` | Weekly source-currency **apply**: fast-forwards stale SIBLING checkouts to their newest cut release (a repo with no release tags tracks its default branch instead — `repo_newest_tag` owns that rule for both scripts). `$CLAUDLOBBY_ROOT` is deliberately excluded: pulling the compositor is root-self-update, which `system.yaml` says ships behind its own toggle, and this script lives in that repo — bash reads a script incrementally, so a self-pull rewrites it mid-execution. Guarded (`repo_pull_blocker` — never a dirty tree, unpushed commits, or a detached HEAD) and `merge --ff-only` never `pull`, so `pull.rebase=true` cannot rebase a checkout. Every movement emits `sibling_updated`; an auto-updating sibling that says nothing is #1009 inverted. **Dormant by default** — the first host job that mutates operator source must not arrive switched on via a root pull; a fleet arms it with `jobs: { update-siblings: { enroll: true } }`. Runs Sun 04:30, 30min ahead of `weekly-worker-restart`, the stage-then-apply-at-a-restart split `update-claude-code.sh` uses for the binary — editable installs mean a pull swaps the CLI for the next subprocess call |
| `gh-mention-guard.sh` | PreToolUse hook: rewrites `@<botname>` out of GitHub-bound tool calls before they run (#1019). Every fleet bot name is also a real GitHub account — all 21 resolve, 19 of them to real people — so a teammate reference emails a stranger, and one asked us to stop. There is no safe name: the two organizations are still accounts we do not control. Whether a person-valued field accepts or rejects an organization is untested — same reason as above. Covers **both** surfaces: `gh issue/pr comment|create|edit|review`, `gh api …body=`, and every `mcp__github__*` writer — a Bash-only guard would miss half the fleet. The two surfaces get DIFFERENT replacements and that asymmetry is a safety property: MCP gets `` `name` `` (house style, safe in JSON) while Bash gets bare `name`, because a comment body sits in a double-quoted shell string where a backtick is **command substitution** — the naive backtick rewrite turns a notification bug into arbitrary execution. Name list is **composed host-wide** by `compose_host_bot_handles` into `runtime/_host/bot-handles`, never hardcoded (a literal list re-breaks on the next bot, #1009's class) and never fleet-scoped (cross-fleet mentions dominate). **Telegram is untouched** — tagging there is correct and load-bearing. Fails open loudly on a missing manifest: blocking every GitHub write fleet-wide is worse than the bug it guards. **It guards TEXT, not person-valued fields** — `--add-reviewer`, `--assignee`, `gh api …/requested_reviewers`, MCP `assignees` are all uncovered and reach a real account (#1062), and the two failure modes are opposite: with no `@` in the payload the zero-fork prefilter allows the call *before* the guard runs, while *with* an `@` anywhere the whole-command rewrite (`:253-255`, `--style bare`) turns `@vera` into `vera` in the command it hands onward. Measured: `gh api users/vera` resolves, `gh api users/@vera` is HTTP 404 — so the rewrite manufactures a valid login out of a string that is not one. **Whether `gh` itself normalises a leading `@` client-side is untested, and is implied neither way here** — settling it would need a real assignment attempt, which is the banned action, so it stays untested deliberately. Widening the prefilter is therefore not a fix on its own: it routes more payloads into a rewrite that must not touch these fields at all. A person-valued flag is not text and admits no safe rewrite; it has to be refused or stripped whole. **Until #1062 lands, route reviews by tmux dispatch and never pass a bot name to a GitHub field that means a person** |
| `mention-rewrite.py` | The rewriter behind `gh-mention-guard.sh` (#1019 follow-up). **Allowlist inversion**: every `@handle` in GitHub-bound text is rewritten unless explicitly declared in `runtime/_host/mention-allowlist`, because the harm class is *any* `@word` that happens to be a real account — `Botfather`, `latest` and `216` are real users we notified and none is a fleet bot, so no denylist could reach them. A composed bot name is an **un-allowlistable deny-override**, or someone eventually allowlists a bot name meaning our bot and re-arms the bug. **THE INVARIANT — when the parser is unsure whether text is inside code, it REWRITES.** Skipping genuine code is legitimate (GitHub does not linkify mentions in fences or spans, which is why backticks are the fix); the hazard is wrongly *believing* something is code, and rewrite-only bounds false positives but not that. So an unterminated fence opens nothing and an unmatched backtick opens nothing. Cost of getting it wrong either way: a corrupted code sample (visible, fixable) versus a stranger emailed (neither). Standalone stdlib module — `dispatch-overdue.py` precedent — so a fence parser inside a security control is unit-testable; `sed` could not carry that invariant legibly. Invoked only after the hook's zero-fork `@` prefilter, so the common tool call never spawns Python (3.5ms vs 137ms measured on a Pi) |
| `reload-fleet.sh` | Daily live plugin/skill reload — `claude plugin update` + generate, then mark running bots for a keepalive-driven `/reload` (no restart) |
| `install-reload-fleet-systemd.sh` | Reload-fleet daily timer enrollment (systemd) |
| `weekly-worker-restart.sh` | Weekly lossless restart of worker bots (managers excluded) to apply a staged binary |
| `install-weekly-worker-restart-systemd.sh` | Weekly-worker-restart timer enrollment (systemd) |
| `rolling-restart.sh` | Serial fleet restart — bots one at a time, each gated on a fresh Telegram `BRIDGE_READY` before the next, so a mass restart cannot land the fleet inbound-dead (#689) |
| `host-health-check.sh` | Host-hardware early-warning — FLEET ALERT on under-voltage/thermal throttling (Pi `vcgencmd`) and SD/MMC storage stalls (kernel log), de-duped via a state file — runs as the `host-health-check` host job |
| `migrate-fleet-to-system.sh` | Staged, reversible migration of a flat `local/<fleet>/` into a nested vault system container (`local/<system>/<fleet>/`), re-pointing every bot supervision unit at the new path |
| `rehearse-debounce-recipient.sh` | Prove on real tmux that a debounced FLEET-PULSE alert survives a manager restart (#831) — drives the real `fleet-pulse.sh` against a throwaway bot with an unresolved condition, restarts the manager session, and asserts the push lands in the new one; isolated sockets + a fake escalation chat so it can never page the real operator. Wrapped by `tests/test_debounce_recipient_harness.py` |
| `rehearse-briefing-timer.sh` | Rehearse the composed briefing-timer chain on a throwaway fleet — asserts the equippable `<prefix>.briefing-<bot>-<slot>` unit fires and that removing a slot prunes its units on both platforms; sibling of `rehearse-keepalive-swap.sh` |

## Repository Hygiene — MANDATORY

### What goes in git (shared, reusable, generalized)

Everything in these top-level directories is committed and shared:

- `library/` — All composable building blocks (see Architecture above)
- `voices/` — Personality overlays
- `templates/` — Jinja2 templates for CLAUDE.md generation
- `lib/` — Lifecycle and utility scripts
- `claudlobby/` — Python compositor source
- `documentation/` — Architecture docs, schema reference, setup guides
- `fleet.yaml.example` — Template manifest (committed; `fleet.yaml` is NOT)

### What stays local (gitignored, fleet-specific, secret)

These are ALL gitignored — never commit them:

- `fleet.yaml` — Your active fleet config. Copy from `fleet.yaml.example`.
- `local/` — Fleet overlays. Each `local/<fleet>/` contains fleet.yaml, local library overrides, voices, and runtime output. **All fleet-specific content lives here.**
- `local/<fleet>/runtime/bots/` — Generated bot directories
- `local/<fleet>/library/` — Fleet-specific library content not general enough for shared
- `.env` — Secrets (tokens, PATs, OAuth credentials). Never committed.
- `runtime/` — Root-mode generated output (if running without fleet overlays)
- `*/projects/` — Git checkouts in bot directories

### The bright line

**If it contains a real token, API key, credential, org ID, database UUID, or fleet-specific path → it goes in `local/` or `.env`.** If it's a reusable pattern that any fleet could benefit from → it goes in `library/`.

When in doubt: would another person running claudlobby find this useful? Yes → library. No → local overlay.

### No PII in committed assets

No personally identifiable information in any checked-in file. This includes:

- Real email addresses, phone numbers, physical addresses
- Real Telegram chat IDs, user IDs, or bot tokens
- Real API keys, OAuth tokens, or credentials
- Real database UUIDs, org IDs, or project IDs
- Real names tied to personal details (author names in pyproject.toml are fine)
- Real IP addresses (localhost/examples are fine)
- Financial account numbers or identifiers

Documentation and examples must use obviously fake placeholders (`ghp_xxxxxxxxxxxxxxxxxxxx`, `"-1001234567890"`, `8888888:AAAAAAAAAAAAAAAAAAAA`). If you need to reference a real service, use generic descriptions, not real account details.

### Before committing, always verify

```bash
git status           # nothing from local/, runtime/, .env should appear
git diff --cached    # no secrets, no fleet-specific UUIDs, no hardcoded paths
```

## Working on This Repo

### Adding library content

Each library category has its own format. Check the category's `README.md` for specifics. General rules:

1. Create the file in the appropriate `library/<category>/` directory
2. Use YAML frontmatter with `title:` and `description:` fields
3. Add an H1 heading (`# Title`) matching the frontmatter title — the loader strips it to avoid duplication in composed output
4. Use `{{BOT_NAME}}`, `{{FLEET_NAME}}`, `{{CLAUDLOBBY_ROOT}}` Jinja2 placeholders where appropriate
5. Test: `claudlobby --fleet <your-fleet> generate` and verify the content appears in the right bot's CLAUDE.md
6. Commit to a branch, PR, review

**Heading levels matter.** The template renders library content inside `##`/`###` sections. The loader runs `_demote_headings` to shift all headings down. An H1 (`#`) in your file becomes H2 in the output. If you start with H3, it becomes H4 — which may be too deep.

### Adding compositor features

1. Edit Python source in `claudlobby/`
2. Run tests: `python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]'`, then `./.venv/bin/pytest`
3. Test against your local fleet: `claudlobby --fleet <name> validate` then `generate`
4. Run `claudlobby --fleet <name> diff` to verify no unintended drift
5. Commit to a branch, PR, review

**Three things about the test suite that will otherwise cost you an hour.**

*Run it unsandboxed — and do not diff sandboxed runs either.* `lib/` scripts call `mktemp -d`
into the real `$TMPDIR`. Under a restrictive agent sandbox those calls return `Operation not
permitted` and roughly **250 phantom failures** appear across every bash-script suite. They are
not real.

It is tempting to assume a *diff* of two sandboxed runs is still sound, since the sandbox
penalises both equally. **It is not.** A test the sandbox already breaks fails in the before run
*and* the after run, so it cancels out of the diff — and any regression you introduce inside it
is invisible. That is not hypothetical: it is exactly how a `claudlobby_cli` regression reached
CI in #947, after a sandboxed diff reported one clean delta. Take the baseline unsandboxed or
not at all.

*Know the baseline.* The suite is **not fully green** on macOS — as of 2026-08-01 it is
**34 failed / 2125 passed**, 24 of them the `tests/test_setup_backbone.py` cluster (#951). Do not
assume your change caused a failure, and do not assume it didn't because the *count* matched —
compare the failing test **names**:

```bash
git stash push -u
./.venv/bin/pytest --tb=no -ra > /tmp/run_before.txt 2>&1; rc_before=$?
awk "/short test summary info/,0" /tmp/run_before.txt | grep -E "^(FAILED|ERROR)" | sort > /tmp/before.txt
git stash pop
./.venv/bin/pytest --tb=no -ra > /tmp/run_after.txt 2>&1; rc_after=$?
awk "/short test summary info/,0" /tmp/run_after.txt | grep -E "^(FAILED|ERROR)" | sort > /tmp/after.txt
comm -13 /tmp/before.txt /tmp/after.txt      # failures YOU introduced
tail -1 /tmp/run_before.txt                 # and compare the counts —
tail -1 /tmp/run_after.txt                  # "N failed, M passed"
```

*An empty diff is not the same as a clean one.* The naive `pytest | grep ^FAILED` this
recipe used to print was wrong in **two independent directions**, and it could also fail
*as a mechanism* while looking fine. **Three checks, all load-bearing, each covering what
the other two cannot** (#1035). Drop any one and a live hole reopens.

| rc | situation | what the naive check did |
|---|---|---|
| 0 / 1 | passed / real failures | missed every failure — `addopts = "-rs"` **replaces** pytest's default `fE` reportchars (`-r` is store, not append), so no `FAILED` line is ever printed |
| 2 | collection error, suite aborted | printed `ERROR`, not `FAILED` — and the count line reads `1 error` rather than `N failed, M passed`, so counts do not catch this one either; the rc gate does |
| 4 | bad path or flag | **invented a failure** — `ERROR: file or directory not found:` matches, so a typo in the *after* run fabricates a regression |
| 5 | nothing collected | nothing to match — reads clean |
| 127 | no `.venv` (e.g. run from the shared install, not your checkout) | nothing to match — reads clean |

1. **The `rc` gate — catches *the run did not complete*.** `rc` not in {0,1} → the diff is
   not evidence. Only this sees rc 2 / 5 / 127, where zero or partial tests ran. Scoping
   cannot: those emit no summary block to scope to.
2. **Scoping to the summary block — catches *phantoms inside the evidence band*.** Captured
   logs and `log_cli` output appear *above* that banner; only pytest's verdict lines appear
   inside it. Only this sees the rc 0 / rc 1 phantoms; the gate cannot, because they exit
   inside the band it certifies. Also makes `--tb=no`, verbosity and `log_cli` irrelevant
   by construction.
3. **The count line — catches *how many*, the only axis that moves on an already-red suite.**
   Re-read the baseline three paragraphs up: **34 failed / 2125 passed.** Our suite is not
   green, so `rc = 1` is the **normal, expected state of both the before run and the after
   run**. That makes the gate structurally incapable of telling a healthy 34-failure
   baseline from a 38-failure baseline-plus-four-regressions — both are `rc 1`. The gate can
   only ever discriminate *did not complete* (rc 2 / 5 / 127). On a suite that is already
   red, counts are not a hedge; they are **the** signal on the only axis that changes.

   They are also the last survivor when the names mechanism itself breaks — which is exactly
   what #1012 did here. Measured on this suite: `1 failed, 20 passed` printed correctly
   while `grep "^FAILED"` returned **0 lines** and no summary banner was emitted at all.
   Names dead, `rc` still 1, diff lands inside the evidence band and reads clean. **A count
   change with an empty name diff is evidence the names mechanism is broken, not a clean
   run.** Counts are a cross-check rather than a substitute — they miss rc 2, where the gate
   covers. Ravi validated counts-plus-names on ai-platform under this same bug, catching
   **four** real regressions the old recipe called clean; clog established the
   baseline-is-red reasoning.

**Never pipe pytest into grep.** You would capture *grep's* status, which is only ever 0 or
1 — every broken mode laundered into "this is evidence" and the gate passes silently.
Redirect, read `$?`, then grep the file. Same `${PIPESTATUS[0]}` trap this file documents
for `gh api ... | head`.

### Adding or modifying lib/ scripts

1. Source `lib-common.sh` for shared helpers (OS detection, bot.conf loading, safe mktemp)
2. Always use `set -euo pipefail` — use `|| true` for intentional failures
3. Quote all variables. Use `printf '%s'` instead of `echo` for values that may contain tokens
4. Test on both Linux and macOS where applicable (use `lib-common.sh` OS detection helpers)
5. Never hardcode fleet names, user home dirs, or Homebrew paths — use env vars and detection
6. No apostrophes in comments inside `$( )` — bash 3.2 (macOS `/bin/bash`, the shebang target) does not strip comments while scanning a command substitution, so a stray apostrophe corrupts quoting for the rest of the file (gate: `tests/test_bash_parse.py`, which covers `lib/` and every `library/**/*.sh`)

### Validating changes to how a bot behaves — MANDATORY

Any change that affects **how a bot behaves at runtime** (lib/ supervision & observability scripts, hooks, skills, protocols, guardrails, principles, composed `bot.conf` env) must be **empirically validated** before merge. Unit tests prove *composition* — that the env var lands in `bot.conf`. Only running the code proves *behavior* — that the event actually fires, the alert actually sends, the bot actually does the thing. Follow the loop:

1. **Deliver** — make the code/library change.
2. **Add config** — set the relevant field(s) in `fleet.yaml`.
3. **Recompose** — `claudlobby --fleet <fleet> generate`; confirm the change landed in the composed `bot.conf` / `settings.local.json` / `CLAUDE.md`.
4. **Observe** — run it and watch the real behavior:
   - For observability/trust-loop behaviors: `bash lib/validate-bot-change.sh` stands up a throwaway bot + tmux sessions and asserts the events fire end-to-end. Extend it when you add a new event/check.
   - For other behavior: spin a bot (`lib/spin-up-bot.sh`), drive the affected path, and watch `data/events/*.jsonl` / `keepalive.log` / the pane.

**Cite the observation in the PR body** ("ran `validate-bot-change.sh` → activity_stuck + overdue_dispatch fired; manager notified") — claimed evidence is not evidence. This is also how latent bugs surface: the harness above caught a `fleet-pulse.sh` sweep-abort that every unit test missed.

**This gate proves the code; it does not prove the rollout.** Clearing it is mandatory for every runtime change. Separately, when a change to the framework itself (claudlobby, clauDNA, claudron) ships **live fleet-wide** — supervision/`lib` scripts, plugins, the bridge, composed `bot.conf` — the manager should *by default* canary the rollout on one production bot before rolling the fleet: a strong default for fleet-wide framework changes, not a universal mandate (skip it for single-bot, product-repo, or non-runtime work). See the `canary-rollout` protocol.

### Validating changes to the onboarding path — MANDATORY

Any change to **what a brand-new user is told to run** — `README.md`, `documentation/getting-started.md`, `.claude/skills/setup/SKILL.md`, `lib/setup-system`, `lib/setup-fleet`, `fleet.yaml.seed`, `.env.seed.example` — must be validated **on a cold host**, not from your checkout.

**Why this is its own gate.** A warm checkout cannot detect onboarding rot, and neither can CI. The maintainer checkout has had a `.venv` for months, so the documented path is never re-run. CI installs on `ubuntu-latest`, where `pip` exists and `setup-python` provides a non-externally-managed environment — so PEP 668 never fires there. Both blind spots pointed the same way, and the result was that `pip install -e .`, **the first command in the README**, failed outright on both first-class hosts for months against a green suite (#947).

The loop:

1. **Export, do not clone** — `git archive <ref> | tar -x -C <dir>`. A clone carries `.git`, and the commit messages describe the very defects you are trying to rediscover.
2. **Run the documented commands verbatim.** Not the ones you know work — the ones the doc prints. Copy-paste them.
3. **Log every exploration event** — any time you read source, grep, guess a flag, or apply knowledge not on the page. *Each one is a documentation defect whether or not you solved it.* Report the count.
4. **Stop at the credential gate.** Essentially every onboarding defect lives before it, so needing real tokens is not a reason to skip the exercise.

`tests/test_cold_start_contract.py` gates the mechanical half of this (no bare `pip`, a venv accompanies every install, all entry points name one template, the CLI resolver probes a submodule and prefers `.venv`). **It is a floor, not a substitute** — it cannot tell you that a doc is confusing, only that it is inconsistent.

**Cite the cold run in the PR body**, same rule as the runtime gate: claimed evidence is not evidence.

For the strongest version — blind agents on both the fixed branch and `main`, so improvements are attributable rather than assumed — see [`documentation/validating-cold-start.md`](documentation/validating-cold-start.md).

### Never hand-edit generated output

Files in `runtime/bots/<name>/` are generated by `claudlobby generate`. Hand-edits will be overwritten on the next generate. To change a bot's config:

1. Edit `fleet.yaml` (fleet-level config) or `library/<category>/` (content)
2. Re-run `claudlobby generate`
3. If the bot drifted during a session (`claudlobby diff` shows changes), use `claudlobby promote` to extract the drift back into library

## Key Commands

```bash
# Composition
claudlobby validate                    # check fleet.yaml against library
claudlobby generate                    # compose runtime/bots/ from fleet.yaml
claudlobby generate --bot <name>       # compose one bot
claudlobby host-timers                 # compose host-global timer units from system.yaml
claudlobby diff                        # show drift between runtime and generate
claudlobby promote <name>              # extract bot drift back into library
claudlobby list-library                # show available building blocks
claudlobby new-bot                     # interactive bot scaffolding

# Operations
claudlobby status                      # fleet health dashboard
claudlobby status --bot <name>         # detailed status for one bot
claudlobby doctor                      # pre-flight fleet health diagnostic
claudlobby freshbox                    # fresh-box self-containment audit (over-grant/orphan, denied source values, externals report, fleet-tier .env, rendered tools/; --strict, --bot, --reap)
claudlobby report-back                 # query bot work event ledger
claudlobby report-back --since 24h     # filter by time window
claudlobby uptime                      # per-bot uptime, MTBR, restart-rate
claudlobby events                      # tail/filter JSONL events across all bots
claudlobby workstreams [list|show <id>] # read-only fleet workstream registry
claudlobby brief --bot <name>          # one read door over fleet state for a bot
claudlobby brief --bot <name> --json   # schema-1 envelope (what other tools consume)
claudlobby brief --bot <name> --ack    # advance that bot's unacked-report cursor
claudlobby warm-cache                  # pre-download npx packages for MCP servers
claudlobby move-bot <bot> --to <fleet> # move a bot between fleets

# Scaffolding
claudlobby new-bot                     # interactive bot scaffolding
claudlobby new-skill                   # scaffold a new skill directory
claudlobby new-guardrail               # scaffold a new guardrail file

# Migration (from legacy layouts)
claudlobby env-migrate                 # migrate .env files into fleet structure
claudlobby data-migrate                # migrate bot data directories
claudlobby cron-migrate                # migrate crontab entries to new paths
claudlobby memory-migrate              # copy memory files from ~/.claude/projects/ to per-bot dirs
claudlobby lessons-migrate             # migrate referential library/lessons/ into the Claudron vault (dry-run by default)

# Testing (the venv is required — PEP 668 refuses a bare install on Homebrew/Debian)
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/pytest                     # run test suite (unsandboxed; baseline is not green)
```

Use `--fleet <name>` for overlay mode: `claudlobby --fleet <your-fleet> generate`

### Fleet operations (lib/ scripts)

```bash
# Bot lifecycle
lib/spin-up-bot.sh <bot-dir>           # enroll + start (idempotent)
lib/reconcile-fleet.sh <fleet>         # audit fleet supervision state
lib/reconcile-fleet.sh <fleet> --enroll # fix orphan bots

# Maintenance
lib/log-rotate-fleet.sh --fleet <name> # rotate all bot logs
lib/git-pull-all.sh <projects-dir>     # pull all repos in a directory
lib/disk-monitor.sh                    # check disk usage, alert if high
lib/fleet-memory-check.sh              # fleet memory planning and monitoring
lib/bench-cold-start.sh               # cold-start timing baseline
lib/check-npx-cache.sh                # verify npx cache state

# After an unplanned reboot — RUN THIS BEFORE RESCUING ANYTHING
lib/selfstart-snapshot.sh             # how many bots self-started (#1002 measurement)
```

## Python Package Structure

```
claudlobby/
  __main__.py         — Thin CLI entry point (~55 lines); argparse setup + subcommands live in commands/
  commands/           — CLI command implementations: argparse registration, core ops, migrations, scaffolding, move-bot, events (12 files)
  config.py           — fleet.yaml parsing, BotConfig/FleetConfig dataclasses
  known_values.py     — Known-good value sets for fleet.yaml fields (SSOT for config + validator)
  composer.py         — CLAUDE.md/bot.conf/.mcp.json/systemd unit generation
  mcp_resolve.py      — MCP fragment ${VAR} env-var / instance resolution (shared by composer + validator)
  tool_resolve.py     — Library tools manifest/template/param resolution (shared by composer + validator)
  loader.py           — Library file loading, frontmatter parsing, heading demotion
  validator.py        — Fleet validation (env vars, MCP refs, scope checks)
  newbot.py           — Interactive bot scaffolding wizard
  newskill.py         — Skill directory scaffolding
  newguardrail.py     — Guardrail file scaffolding
  prompts.py          — Shared interactive prompt helpers for the scaffolding wizards
  diff.py             — Drift detection and promotion
  dotenv.py           — .env file handling
  paths.py            — Path resolution helpers
  doctor.py           — Pre-flight fleet health diagnostic
  freshbox.py         — Fresh-box self-containment audit (#644 P4): over-grant/orphan + under-grant + Tier-A composed-not-inherited; deny-by-default rungs (#703): denied source values, externals report + unused-declaration WARN, fleet-tier .env guard, rendered tools/ (backs `claudlobby freshbox`)
  status.py           — Fleet health dashboard (tmux/systemd/fleet-state)
  uptime.py           — Per-bot uptime, MTBR, restart-rate metrics
  utilization.py      — Fleet utilization rollup — per-bot busy/idle % over rolling windows
  path_audit.py       — L1 path/grant provenance audit (source-side ownership + external_paths)
  conformance.py      — L4 conformance gates (rename-map drift, boundary invariants)
  workstreams.py      — Read-only view of the per-fleet workstream registry (workstreams.json)
  brief.py            — The fleet's one read door (#904 / epic #1102 R1): mission pointers, dispatches via the #835 doors, workstreams + stall flags, unacked reports, recent critical events. Skills consume THIS, never raw state files. Read-only but for one write, the per-viewer `--ack` cursor. **It never serves a number it knows is wrong**: where an R0 trust prerequisite is unlanded the field is LABELED (present, bound stated) or OMITTED (absent, because the value would be an artifact), and both land in the envelope's `degraded[]` — a field neither present nor listed does not exist. Each gate is DETECTED, never assumed, or the disclosure outlives its defect and becomes its own untruth: #911 is MEASURED (re-scan the two ledgers, count rows the shared readers dropped — self-clearing when the writers escape), #903 is keyed on its own deliverable (the `known_values` event-type registry) because the missing alerts are exactly the ones the filter cannot return, #891/§6 is a standing omission so "where is utilization?" has an answer. It consumes the #835 doors **defensively — their behaviour unchanged, which is not the same claim as the file untouched** (`lib/dispatch-overdue.py` gains the `--open` mode #904 specifies, and `open_task_id` becomes that list's head rather than a second loop over the same join; the join itself, `_classify_all`, `overdue_all`, `orphaned_all` and the three CLI contracts are byte-identical). Defensively, because they fail open: an **absent** report ledger makes the matcher answer with nothing to join against, so every past-deadline dispatch in history returns overdue at rc 0 silently (measured: 5 closed dispatches → 5 overdue rows; 178 on a real fleet), while an **unreadable** one raises out of it entirely. The realistic route is #526, not a typo — host-global dispatch log against per-fleet report ledgers means another fleet's bots read as permanently overdue. So both ledgers are probed first and the section is OMITTED when either is absent or unreadable: not zero (a false all-clear), not everything (a wall of finished work shown as outstanding). **The line is presence, not emptiness** — an existing ledger with no rows is a fleet that has not reported yet, for which "every dispatch is still open" is true. Same shape for reports (`unacked (0)` from an unreadable ledger is #949/#1024 re-created) and for the orphan list, which is empty *by construction* without a bots dir (#1014) and so is labeled. The matcher is loaded from the INSTALL's `lib/`, not the importing checkout, so the two version independently — a root predating the open-list door degrades `dispatches.open` alone rather than raising. Text output is capped per section and says so (a live brief rendered 335 unacked reports; a section that must be scrolled past has already failed to route attention); `--json` is never capped
  claudron_compat.py  — Claudron compatibility floor — min capability per integration surface
```
