# Claudron Integration Guide

How claudlobby connects to [Claudron](https://github.com/Claudfather/Claudron) — the ecosystem's knowledge hub. Claudron owns the long-lived knowledge corpus in a **tenant-owned vault** (a private git repo of plain markdown); claudlobby consumes it. Claudlobby never stores the corpus, never forks Claudron's `SCHEMA.md` (the tri-repo schema SSOT), and never writes `runtime/`, `.env`, or operator PII into a vault.

The full receiving-side plan is `documentation/plans/2026-07-07-claudron-consumption.md` (EPIC #509); Claudron's side is Claudfather/Claudron#14.

## What works today (at v0.2.0)

- **Vault-based fleet overlays.** A `.claudron` file at the claudlobby root (shell-sourceable, gitignored) points at a vault; `claudlobby --fleet <name>` resolves the fleet overlay from `vault/<name>/` before falling back to `local/<name>/`. Uses claudron's `claudron.vault.detect` / `Vault.fleets` API when the `[vault]` extra is installed, with a manual bridge-file fallback otherwise.
- **Per-bot vault env.** `claudron_vault_path` in fleet.yaml (per-bot or `defaults:`) composes `CLAUDRON_VAULT_PATH` into `bot.conf`.
- **CLI lookup + the dispatch query wedge.** `claudron lookup` works over a vault, and `dispatch-task.sh` can use it as a query-before preflight: set `CLAUDRON_QUERY_BEFORE=1` (plus `CLAUDRON_VAULT_PATH`, claudron CLI on PATH) in the manager's env and every dispatched task gains a single-line `[fleet memory: <title> (<path>); …]` pointer prefix — titles and paths only, never note bodies; the worker reads the files itself. The ledger's `claudron_hits` field counts injected pointers per dispatch (`""` = preflight off, `"0"` = ran with no hits) — the fleet query-volume evidence Claudron's Gate G1 asks for. Note the dispatch ledger self-reaps (default `OBSERVABILITY_REAP_DAYS=7`), which is shorter than a two-week soak window: sample the counts weekly, or raise the reap window on the dispatching manager for the soak. Off by default; any lookup failure degrades to a plain send. The wedge reads the CLI-contract `{ok, data:{results}}` lookup envelope (falling back to the pre-envelope flat `{results}`, so version skew between the repo pin and a fleet's installed CLI degrades gracefully). It passes **no `--vault`**: `CLAUDRON_VAULT_PATH` is the canonical vault address and the CLI reads it itself ([CLI_CONTRACT.md §Environment](https://github.com/Claudfather/Claudron/blob/main/docs/CLI_CONTRACT.md#environment), row 2), so bot and CLI cannot resolve two different vaults. An unresolvable vault exits 3 with nothing on stdout; the wedge validates output before injecting either way.

## Fleet session loop (plan L2)

A vault-wired bot (`claudron_vault_path` set) runs the Claudron **session loop** by default: the composer installs the engine's three lifecycle hooks into the bot's `settings.local.json` and grants the narrow CLI verbs the model may call itself. Claudron owns *what fires when* — this section is the consumer-side pointer; the normative contract is [CLI_CONTRACT.md §Session-loop protocol](https://github.com/Claudfather/Claudron/blob/main/docs/CLI_CONTRACT.md#session-loop-protocol).

**What fires when** (the three composed hooks; budgets are contract):

| Event | Engine op | Budget | On expiry |
|---|---|---|---|
| `SessionStart` | `sync --pull`, then the recall brief on stdout (injected into context) | `2.0s` pull | brief renders from local state |
| `PreCompact` | the single capture prompt (block-and-instruct once) | — (no I/O) | — |
| `SessionEnd` | `sync --push` | `10.0s` push | push abandoned; **commits travel on the next session's push** |

Hooks **fail open**: any error (unresolvable vault, missing git, a stalled network) emits nothing on stdout, logs to `.claudron/hooks.log`, and exits 0 — a hook never breaks a session. The `10s` SessionEnd budget bounds the **whole `sync` call including any write-lock wait** (on an engine that holds one — see contention below), not just the network push.

**One prompt on fleets (F1 is structural).** The composer sets **no claim env** — there is no `CLAUDRON_CAPTURE_OWNER` or holder variable. The single `PreCompact` capture prompt is claimed structurally: clauDNA's hook (the fleet default plugin) detects the engine's registered `hook pre-compact` entry and defers to it, so exactly one prompt fires. Bots without clauDNA get the engine's neutral prompt directly.

**Grants — narrow verb allowlist only.** The composer grants `Bash(claudron lookup *)`, `Bash(claudron recall *)`, `Bash(claudron capture *)`, `Bash(claudron status *)` — the read/write verbs a bot legitimately self-serves (the query wedge, `/claudna:capture` shelling the CLI). It **never** emits `Bash(claudron *)`: the wildcard would grant `promote`/`plug`/`unplug`/`config`/`migrate` and defeat human-gated curation (boundary spec §8). The settings-installed hooks are harness-executed and pass through no permission check, so they need no grant. A vault-wired bot therefore **cannot** run `claudron promote`.

**Enable / disable.** `claudron_session_loop` is tri-state: default **on** when `claudron_vault_path` is set, **off** otherwise; set it explicitly only to override (e.g. `false` on a vault-wired bot meant to reach the vault by hand-run CLI alone — it then composes neither hooks nor verb grants). `claudron_session_loop: true` with no vault path is a `claudlobby validate` error.

**The composed hook block is a rendered copy of an owned surface** (register rule R3): its shape is fixed by the contract's normative snippet, and `tests/test_claudron_loop.py` carries the drift gate comparing the composer's output against the pinned engine's `claudron.hooks.settings_snippet()` (the L4 parity gate, runnable here).

**Contention and push loss.** N bots on one host sharing one vault fire concurrent `SessionEnd` pushes. Engines that ship the vault write-lock (`locking.py`, post-0.2.0) serialize them on a `flock` bounded by the same `10s` budget; the pinned **v0.2.0 has no write-lock**, so concurrent syncs serialize only on git's own `index.lock`. The guarantee that holds at **every** engine version is the load-bearing one: **the hook never exits nonzero (fail-open), and `sync` commits before it pushes — so a raced or timed-out push loses no work; the local commit travels on the next `SessionEnd`.** The N-bot contention test (`tests/test_claudron_loop.py::TestSessionEndContention`, ≥8 bots) asserts exactly this and records the completed-vs-deferred push accounting. Operationally, the fleet's **weekly-restart stagger** spreads SessionEnd bursts so contention stays low; if a soak shows real push loss, the escalation is the host-level sync-timer variant (F2(b), tracked as Claudron #43), not a change here.

## Version pin and bump policy

The `[vault]` extra in `pyproject.toml` is **pinned** to a released Claudron tag — `@v0.2.0` today (git tag; Claudron is not on PyPI yet, so the pin stays a `git+…@<tag>` URL rather than a `claudron>=0.2,<0.3` range until a PyPI publish lands). The extra tracks the **compositor's API consumption** (currently `claudron.vault.detect` / `Vault.fleets`); bump it per Claudron release *after* claudlobby's vault-mode tests (`tests/test_paths_integration.py`, run with claudron installed) pass against the new tag. Never revert to a bare git URL — `tests/test_claudron_compat.py` enforces the pin.

The MCP *server* install is deliberately **not** coupled to this extra: bots don't run in claudlobby's venv. The server is a host-level install (see Gated surfaces below).

## Compatibility floor

SSOT: `claudlobby/claudron_compat.py` — `claudlobby doctor`'s `check_claudron` reads it and renders one row per entry; a unit test keeps this table in sync. Claudron's release numbers are **ordinal** — if their Gate G1 re-orders epics, version numbers follow ship order — so every gate below is the *capability*, with the release number only an annotation. Doctor honors that: it decides met/unmet by **probing the capability** (the `[vault]` import seam, or `claudron <verb> --help` on the host CLI), never by comparing version strings. `engine_version` from Claudron's capability probe is reported as engine *health*, not used as a floor trigger.

| Claudlobby surface | Requires (capability) | Slated release | Doctor state |
|---|---|---|---|
| vault-based fleet overlay resolution (paths.py .claudron bridge) | claudron.vault.detect / Vault.fleets API | 0.2.0 | probed (`[vault]` extra) |
| CLI query wedge (dispatch-task.sh preflight) | claudron lookup CLI | 0.2.0 | probed (`claudron lookup`) |
| fleet session loop — engine hooks installed per bot (plan L2) | claudron hooks install + per-event hook dispatch | 0.2.0 | probed (`claudron hooks`) |
| MCP fragment library/mcp/claudron.json | claudron-mcp stdio server | unbuilt — demand-gated | **parked (decision C)** — never "unmet" |

**Parked ≠ unmet.** A parked row is a surface deliberately *not built* under a recorded decision. Doctor never probes it and never renders it as a deficiency: warning about a door nobody shipped is the failure this check exists to stop making. The `librarian review sweep` row was **dropped** in this revision — `claudron review` does not exist at Claudron head, and its Claudlobby consumer (`lib/claudron-review-sweep.sh`) does not exist either, so the row gated nothing. It re-enters the floor when Claudron's E5 review queue ships and a consumer is built; it is tracked below until then.

## Decision C — amendment pointer (trigger 1 re-scoped)

Decision C (`documentation/decisions/2026-07-18-claudron-consumption-door.md`) parks the MCP fragment as demand-gated. Boundary spec §10.5.2 (`Claudron:documentation/plans/2026-07-20-claudfather-boundary-separation.md`) **re-scopes its trigger 1** after #644, and this is the Claudlobby-side record of that amendment:

- **Was:** "a fleet policy that needs per-tool vault-access permission scoping" — premised on a CLI-skill door being one blanket grant.
- **Is:** *adversarial-grade, non-circumventable per-verb enforcement.* #644's `tool_grants` + `tools.deny` already express the cooperative-grade split (`Bash(claudron lookup *)` allowed, `Bash(claudron capture *)` denied; deny wins) — that is **pattern**-grade, not structural, since an agent can spell an invocation many ways. Trigger 1 survives only where a fleet policy must hold against an agent trying to evade it.
- Trigger 2 is unchanged: a concrete non-clauDNA MCP consumer.
- The monitor is now a **named check**, not a human habit: this floor table's parked row plus the validator's CLI-door check (`validator.py`, `_validate_bots`).

The decision doc itself is amended in Claudron's boundary phase C1; this section is the pointer, kept where the monitor lives.

## Gated surfaces (not yet available)

Tracked, not built. Nothing here is a defect in a fleet that lacks it:

- **MCP fragment** (#511/#513, #251): `library/mcp/claudron.json` and the `setup-system --with-claudron` host-level server install. **Parked under decision C** with the re-scoped trigger above — not merely "awaiting a release".
- **Librarian standing job** (#514): `lib/claudron-review-sweep.sh` draining `claudron review --json` weekly into a designated librarian bot. Waits on Claudron's E5 review queue.

## References

- Plan: `documentation/plans/2026-07-07-claudron-consumption.md` · EPIC #509 (children #510–#514)
- Claudron roadmap: Claudfather/Claudron#14 · MCP contract: Claudfather/Claudron#17
- Schema SSOT: `SCHEMA.md` at the Claudron repo root (their E1)
