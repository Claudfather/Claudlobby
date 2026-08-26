---
title: GitHub App installation-token auth — port from a private org fork, then cut the fork over
type: plan
status: completed
owner: fleet owner
created: 2026-08-19
---

# GitHub App installation-token auth — port from a private org fork, then cut the fork over

**Status:** completed 2026-08-20 — Act 1 shipped in full; Act 2 partially, with P8 parked. Outcomes recorded in §Outcome below; the plan body beneath it is preserved AS RATIFIED and is not retro-edited, so the record shows what was planned next to what happened.
**Owned by:** fleet owner
**Origin:** the org fork's PRs #1–#5 (fork diverged at `cb498e06`, 2026-05-12; a private, non-fork org duplicate of this public repo)
**Cites:** #252 · #806 · #1104 · #1155 · #1169 · #1213 · #1214/#1226 · PROJECT_MISSION.md sprint focus #1–#2 and requires-approval bullets (new dependencies; "Changes to how bot identities are provisioned")
**Review:** ironclad cycle 1 (8 lenses) — findings folded 2026-08-19; review record on epic #1270
**Branch:** plan/github-app-port (this doc, PR #1269); implementation branches per phase

---

## Outcome (recorded 2026-08-20)

Act 1 shipped in full. Act 2 shipped except its resync, which is parked. **The plan body below
is preserved exactly as ratified** — deviations are recorded here rather than edited into it,
because a plan quietly rewritten to match its outcome cannot teach anything later.

| Phase | Issue | Outcome |
|---|---|---|
| P1 helper + mint CLI + setup | #1271 | merged #1281 |
| P2 MCP fragment + wrapper | #1272 | merged #1283 |
| P3 composed-gitconfig seam | #1273 | merged #1284 |
| P4 creds-check fallback | #1274 | merged #1287 |
| P5 runbook + integrations | #1275 | merged #1288 |
| P6 canary + close-out | #1276 | **done** — closed |
| P7 fork content rescue | #1277 | **done** — closed |
| P8 resync | #1278 | **PARKED** at the operator's direction |
| P9 host migration | #1279 | **closed as already-satisfied** — premise was false |

### What the plan got wrong, and what it could not have known

**P9's premise was false.** The phase assumed the fork's fleet ran it and needed a staged
migration. It does not: no checkout of the fork exists on the host, one install tracks upstream
and hosts both fleets on the current per-bot-socket architecture, `validate` exits 0, and all 18
bots are healthy. The whole phase evaporated on contact with the host — the plan asserted a
deployment state it never verified.

**P8's gate is structurally insufficient, and this is the transferable lesson.** The phase reads
*"findings fixed upstream BEFORE the push, so both repos benefit"* — true of the working tree and
false of what the destination receives, because a force-push carries every prior commit. A
working-tree sweep can gate what a reader SEES; it cannot gate what a fork GETS. Any future plan
pairing "sanitise, then force-push history" inherits this and needs a squash or a history
rewrite, not a sweep.

**The sweep found the opposite of what the risk table predicted.** The register rated *"PII in
ported docs"* High, expecting secrets. Eight sweeps over 657 files found **no live credential
anywhere, tree or history** — every token-shaped hit was a placeholder, all 17 MCP configs carried
only `${ENV_VAR}`. What it found instead was identifiers: a non-consenting third party's name
sitting in the very files built to stop strangers being contacted, a real week of the operator's
finances, employer names in personal skills. Right severity, wrong category — and the category
determined the fix.

### What the plan missed entirely

**Per-org commit identity** is not in this document. It emerged from the canary, when the real
requirement surfaced: commit to the framework org as the bot and to the operator's employer as
the operator, with the App holding no permission on the latter. Shipped as #1301/#1302 keyed on
`includeIf hasconfig:remote.*.url`, and it is now the feature's most-used behaviour — the plan's
F-identity fork had modelled identity as one global switch.

### Verification, as run rather than as specified

P6's evidence gate was met with a real push: a commit authored from a production bot that
**GitHub attributed to `claudfather[bot]`** — author and committer — on the App's org, while the
same bot's other checkouts stayed on the operator identity. Branch deleted, clone reaped. The
shared PAT was found **dead** during the same run (#1213's failure class, live), so the App
minted where the PAT could not — an unplanned but conclusive demonstration of the thesis.

### Follow-ons this program produced

#1290 estate-wide adoption · #1286 per-user credential-cache cross-serving · #1304 identity-choice
docs · #1305 alert-routing salvage · #1306 + #1318 the PII remediation.

---

## Summary

Port the working **fleet-scope GitHub App auth stack** from the org fork into upstream — credential helper, mint CLI, MCP refresh wrapper, opt-in MCP fragment, creds-check fallback, runbook — re-seated on the composed per-bot gitconfig seam (#806) instead of the fork's host-global `git config --global` mutations, opt-in and dormant by default. Then **Act 2**: rescue the fork's org-specific content, settle the fork repo's end state (F10), and migrate its live host as a staged, rehearsed rollout gated on Act 1's canary evidence.

**The program invariant (from ironclad B1, generalizing D1):** *every* non-git-initiated token acquisition — the mint CLI, the MCP wrapper, the creds-check probe, any skill — invokes `lib/git-credential-github-app` **directly**, never `git credential fill` against ambient or even composed config. A pathless `fill` cannot match an org-scoped credential section by git's own matching rules, so the fill pattern silently substitutes another identity in the *happy path*; direct invocation is the only shape that survives every context (bot, operator shell, cron). Only git itself, resolving a real URL during push/fetch, goes through the config chain.

**Per-bot-ready (ironclad synthesis):** this plan ships the fleet-scope rung, but every surface is built so #252's per-bot rung swaps *credentials, not architecture* — helper-direct minting everywhere, `github_app:` merging per-bot (F8), per-bot composed gitconfig. Sunset clauses in `attribution-prefix` and `review-flow` fire on **per-bot** Apps; the P6 docs audit's expected conclusion is *keep, amend wording* — fleet-scope must not be read as triggering them, and `who-reviewed.py` stays load-bearing within a fleet.

## Evidence

- Fork stack (port source): the org fork at `63806236` — `lib/git-credential-botfarm`, `lib/mint-github-token.sh`, `lib/github-mcp-wrapper.py`, `library/mcp/github-app.json`, `lib/setup-git-creds-app.sh`, `documentation/runbooks/github-app-setup.md`, `documentation/examples/prevent-main-push-ruleset.json`, a creds-check fallback hunk. The fork wrapper and its creds-check hunk both mint via pathless `git credential fill` — the exact pattern the program invariant bans; both are rewritten in the port, not transplanted.
- Upstream seam: `compose_bot_gitconfig` composer.py:535-649; `GIT_CONFIG_GLOBAL` gate composer.py:1064-1067; ordering pins tests/test_composer.py:4117-4399; parse rejections config.py:866-916; plan doc `2026-07-27-per-org-git-credential-routing.md` (its three ordering properties and J3 username decision are honored unchanged).
- **#1214 Phase 1 is MERGED** (#1226, commit `dc0c1d0`, an ancestor of this branch): `ContractVar` carries `secret`/`source` and `tier` is renamed `default_tier` (mcp_resolve.py:78-112); `library/mcp/github.json` already declares `secret: true, source: "cli:gh-token"`; `known_values.py:164-178` registers a **RESERVED `mint:github-app`** credential source ("adding minting later is one arm rather than a migration") and validator.py:574-581 warns it "resolves nothing yet — supply the value in a .env tier until the minting resolver ships". This plan engages that socket via **F9** rather than creating a parallel provenance surface.
- Fragment mechanics: anchors library/mcp/README.md + path_audit.py:44-48 — **the braced form `${CLAUDLOBBY_ROOT}` is load-bearing**: the unbraced `$VAR` form dodges the `_VAR_RE` placeholder walk (mcp_resolve.py:24) and would silently falsify the audited-path claim; `doctor` PATH-checks `command` unexpanded (doctor.py:130-138); composed server name derives from the ENTRY name (`McpEntry.output_name`, config.py:411-415) → tools are `mcp__github-app__*`.
- creds-check: `check_github_pat` lib/creds-check.sh:266-288 probes `/user` — correct for PATs, **wrong for `ghs_` installation tokens, which 403 there** ("Resource not accessible by integration"); the App probe endpoint is pinned in P4. Skip-escalation ladder :234-245; #1213 names the declared-and-absent gap and the host-global-state defect.
- Live-fleet constraints: `lib/` is a shared install (cannot be staged per-bot) — so P4's rollout uses the estate's **dormant-arming pattern**, not a deploy-to-one-bot canary, which is impossible for a fleet-timer carrier; `library/protocols/canary-rollout.md` governs P6; validation mandate CLAUDE.md ("empirically validated before merge; cite the observation").
- Estate physics: the primary host is an RTC-less Pi with a documented stale-clock window at **every boot** — JWT `iat`/`exp` minting is exactly what that breaks, so first-mint failure is an every-boot class, not an edge case (D12).
- The MCP respawn model's evidence is package-specific: stdio-passthrough respawn (no fresh `initialize`) is validated only against the pinned `@modelcontextprotocol/server-github@2025.4.8` — the same deprecated pin `github.json` uses; in-flight requests fail for ~2s per respawn. Both caveats are disclosed here and in the P5 runbook; any server swap re-validates post-respawn tool calls.

## Decision forks

| ID | Fork | Lean / decision | Ratifier | Status |
|---|---|---|---|---|
| F1 | Helper install seam | (b) composed per-bot gitconfig; helper by baked absolute path; zero host mutation | fleet owner | **locked (b)** — plan-approval gate 2026-08-19 |
| F2 | JWT signing | (b) openssl-CLI RS256; zero pip deps; opt-in dependency (satisfies the mission's new-dependency approval) | fleet owner | **locked (b)** — in-session 2026-08-19 |
| F3 | Naming + env contract | (b) claudlobby-native: `lib/git-credential-github-app`; `GITHUB_APP_ID`/`GITHUB_APP_INSTALLATION_ID`/`GITHUB_APP_PRIVATE_KEY_PATH` in the fragment `_env_contract`; config-file fallback for non-bot contexts | fleet owner | **locked (b)** — plan-approval gate 2026-08-19 |
| F4 | Scope vs #252 | fleet-scope single App now; #252 updated with landed-vs-remaining | fleet owner | **locked** — in-session 2026-08-19 |
| F5 | `gh` stance | (b) per-call minting; PLUS a composed per-bot `tools/` gh shim (P3) so the discipline is mechanical, not remembered; never a boot-time export | fleet owner | **locked (b)** — in-session 2026-08-19; shim added by ironclad fold |
| F-identity | Commit authorship under App mode | (a) bot authorship (`<slug>[bot]` + noreply email) after the operator include; guardrail rationale amendment, not repeal | fleet owner | **locked (a)** — in-session 2026-08-19 |
| F6 | creds-check fallback depth | (b) mint (helper-direct) then probe an authenticated App endpoint | fleet owner | **locked (b)** — plan-approval gate 2026-08-19 |
| F7 | Program scope | AWS probe + playwright are separate follow-ups | fleet owner | **locked** — in-session 2026-08-19 |
| F8 | Config shape | (b) dedicated `github_app:` field, fleet-defaults + per-bot merge | fleet owner | **locked (b)** — plan-approval gate 2026-08-19 |
| **F9** | **Engagement with the reserved `mint:github-app` source** (#1226 shipped `credential_sources:` + registry + validator warning before this plan landed) | **Decided: the use-time helper IS the shipped minting path for fleet scope**; the resolver arm stays RESERVED for #252's per-bot sidecar (F5's per-call rationale argues against a boot-time resolver for 1h tokens — a resolver would put them at rest in the boot env, #1214 F5's own hazard). P2/P5 update the `known_values.py` comment + validator message to point fleet-scope users at the helper; a #1214 comment states the App path deliberately bypasses env-var resolution. Rejected: implementing the resolver arm now. | fleet owner | **locked** — in-session fork question 2026-08-19 |
| **F10** | **Fork repo end state after Act 2** (verified: a private, standalone org repo — no GitHub fork machinery applies; `notify-behind` is structurally blind to repo-pair drift, so a live mirror re-diverges unowned) | **Decided: overwrite-then-retire** (owner's words: "close this and then overwrite it with my version") — the force-push STAYS so the frozen record shows the final upstream tree rather than the May-era fork state, and the repo is then retired, not kept as a living mirror: P7 rescue → archive-tag fork main → force-push upstream over it → close fork PRs #6–#9 → repoint the migrated host's origin to upstream → **GitHub-archive the org repo** (mechanical ordering constraint: archives are read-only, so overwrite must precede archive). Zero standing sync obligation; no sync owner or drift watcher needed. Rejected: live mirror; archive-without-overwrite. | fleet owner | **locked** — in-session fork question 2026-08-19 |

## Architecture

- **Helper** `lib/git-credential-github-app`: bash + openssl + curl + jq. Env-first (`GITHUB_APP_*` via the bot.conf tier chain), config-file fallback (`~/.config/claudlobby/github-app.conf`). Signs the RS256 App JWT with `openssl dgst -sha256 -sign`, **backdating `iat` by 60s** (GitHub's own guidance; first defense for the Pi boot-clock window), exchanges for a ~1h `ghs_` token, emits `username=x-access-token` / `password=<token>`. **On hard failure: emits `quit=1`** (documented git-credential attribute — stops the helper chain so git fails loudly instead of falling through to gh; ironclad B2), plus nonzero exit + empty stdout + stderr reason for direct invocation (D9). Emits an `auth_mint_failed` JSONL event on failure so `claudlobby events`/fleet-pulse see degradation same-day rather than at the next 06:00 creds-check.
- **Composed gitconfig** (App section when `github_app` declared), order preserving the three pinned properties: operator include → `useHttpPath` + helper reset → per-org PAT sections → App block (`cache --timeout=3000`, then the App helper as a baked absolute DIRECT path) → `[user]` identity iff `slug` AND `bot_user_id` → `insteadOf` ssh→https (org-scoped when `orgs:` declared, host-wide otherwise) → gh fallback ONLY when org-scoped (with `quit=1` making App-org failure loud in both scopings). PAT-beats-App falls out of ordering — per-DECLARATION (D2). Gates widen to `git_credentials OR github_app`; `compose_bot_gitconfig` gains `paths`, landing atomically with diff.py:99 (D8).
- **gh shim** (P3, closing ironclad R6): a composed per-bot `tools/` gh wrapper — `exec` real gh with `GH_TOKEN` freshly minted helper-direct — placed on App-mode bots' PATH ahead of system gh, so App identity on the gh surface is mechanical rather than remembered. Still F5: per-call, short-lived, never exported at boot. Residual (documented): operator shells outside bot contexts still choose their own gh identity.
- **MCP fragment** `library/mcp/github-app.json`: `command: /bin/sh`, `args: ["-c", "exec \"${CLAUDLOBBY_ROOT}/lib/github-app-mcp-wrapper.py\""]` (braced anchor — load-bearing). `_env_contract` on the **merged #1226 schema**: the three `GITHUB_APP_*` vars, each `default_tier: fleet` and `secret: true` (the documented test is authenticate-without, and the integration cannot authenticate without any of the three — this opts them into the credential-failure alerting class), no `source` (human-supplied; F9 governs the registry story). NOT `GITHUB_PAT` (owned by `github.json`; an unset declared var is a creds-reconcile shape-1 FAIL). `_permissions_contract` kept; fork's `_global_binary` dropped. Composed tools are `mcp__github-app__*`. Same package pin as `github.json`.
- **Wrapper** `lib/github-app-mcp-wrapper.py`: mints **helper-direct** (program invariant — the fork's fill-based `mint_token` is rewritten, not ported), respawns the child every ~50min. Failure contract (ironclad R2): first-mint failure → retry with backoff *before* first child spawn (the stdio client waits; the session gets a late MCP, not a dead one); re-mint failure → keep serving the live child until a retry succeeds (its token may outlive one refresh window; better a brief 401 than a dead server); every failure emits `auth_mint_failed`. `GITHUB_PAT` env fallback retained, disclosed in `github.json`'s contract prose (the creds-reconcile UNKNOWN shape).
- **Dormancy** (verified structurally): an unreferenced fragment is never parsed; new lib/ scripts are net-new and uninvoked; the ONLY live-path edit in Act 1 is P4's hunk, which is **config-gated** — it attempts a mint only when `GITHUB_APP_*` config is present, so fleets without App config see byte-identical behavior even with the helper installed estate-wide.
- **Threat framing (honest):** the App private key mints indefinitely and never expires — bot-compromise blast radius is *unchanged* vs the shared PAT. The wins are decoupling from the human account, suspend/rotate ergonomics, short-lived tokens in transit, and branch protection that actually binds bots. Key at rest: 0600, fleet-owned path (`secret_files` shape), freshbox FAIL on looser modes; rotation runbook per P5.

## Complexity and Sequencing

| Phase | Issue | Size | Depends on | Parallel with |
|---|---|---|---|---|
| P1 helper + mint CLI + setup script | #1271 | M | — | P2 |
| P2 MCP fragment + wrapper | #1272 | S/M | — (P1 for e2e) | P1 |
| P3 `github_app` field + composed seam + gh shim + harness scenario | #1273 | **M/L** | P1 | P4 |
| P4 creds-check fallback (config-gated, #1213-composed) | #1274 | S | P1 | P3 |
| P5 runbook + ruleset + integrations section + key rotation | #1275 | S/M | P1–P3 | — |
| P6 canary adoption + close-out | #1276 | M | P1–P5 | — |
| P7 fork content rescue | #1277 | S | Act 1 P1–P3 | — |
| P8 PII sweep + overwrite-then-retire (F10) | #1278 | S (+unbounded remediation tail if the sweep finds anything) | P7 gate | — |
| P9 fork host migration | #1279 | M/L | P8; **P6 canary evidence** (ironclad R5 — the fork's live fleet is never the first real-App exercise of the composed seam) | — |

Critical path: P1 → P3 → P5 → P6 → P9. One PR per phase (P1+P2 may combine if reviewably small).

## Implementation Plan

### Dependencies

In-repo: none unlanded — #1226 is merged and this plan builds on it (F9). External: a registered GitHub App (P6 creates it; P1–P5 are fully offline).

### Blocks

Unlocks #252's per-bot rung (credentials swap, not architecture); retires the fork as a divergent line (per F10).

### Steps

**Act 1 — the port**

- **P1 (#1271, M)** — helper (spec per Architecture: openssl signing, iat backdate, quit=1, JSONL event, D9 exit contract), `lib/mint-github-token.sh` (helper-direct — D1), `lib/setup-github-app.sh` (fork's validation ladder kept: PEM shape → `openssl rsa -check` w/ CRLF diagnostic → live test mint → `^ghs_` assert → bot user-ID lookup → noreply-email derivation → end-to-end probe w/ helper-shadowing diagnostic + JWT-401 troubleshooting tree; **plus a closed verification loop** — the script ends by minting through the exact composed path and printing pass/fail, not just values). No global-git writes, no PATH/sudo install. Tests: curl stub answering the exchange endpoint from the `--config` file; argv.log pin (no token/key on argv); `constructed_env`; `call_script_fn`; `sysbin_excluding("openssl")`; quit=1-on-failure pin. CLAUDE.md rows; exec bits; bash-3.2 rule; subagent PII sweep.
- **P2 (#1272, S/M)** — fragment + wrapper per Architecture. Tests: fragment parses; contract enumerates exactly 3 vars **with `secret: true` + `default_tier` on the merged schema**; braced anchor survives into `.mcp.json`; path-audit green; doctor quiet; freshbox/validator green unreferenced AND referenced; wrapper one-shot + refresh-loop with stub child; **mint-fails-then-succeeds boots the child late, never dead**; org-scoped-bot wrapper returns `ghs_` from the App stub with zero gh-fallback consultations (B1 pin); respawn-transparency pin (post-respawn tool call succeeds against the stub). F9 texts: update the `known_values.py` reserved-source comment + validator message to name the helper as fleet-scope's minting path.
- **P3 (#1273, M/L)** — S1 `GithubAppConfig` + parser (strict-bool enabled; `enabled: false` post-merge → None). S2 `compose_bot_gitconfig(bot, paths)` + gates + `collect_env_contracts` registration (deduped vs fragment) + D3 docstring rewrite. S3 diff.py same-commit. S4 validator warns (unset vars; one-of slug/id; D6 reverse-`insteadOf` probe; D4 bot-tier override) + D2 warn-text fix. S5 freshbox (missing-include split; missing key file FAIL; **key mode ≠ 0600 FAIL**). S6 path-audit pin. S7 docs (dormant-by-default; PAT-beats-App per-declaration; restart-not-reload; D5 cache-TTL note). S8 pin-per-property tests + behavioral counting-stub fill runs + **failing-App-helper-stub asserts no fall-through to gh (B2 pin)**. S9 `validate-bot-change.sh` banner-fenced scenario (real composer output, real openssl key, curl stub, fill/approve chain, D9 + quit=1 pins). **S10 the gh shim** in composed `tools/` (tool.yaml + template per library/tools convention) + pins. Composed-diff-empty assertion for non-declaring fleets. Guardrail D7 amendment.
- **P4 (#1274, S)** — config-gated helper fallback in `check_github_pat` before the skip branch: when `GITHUB_APP_*` present, mint **helper-direct** (never `git credential`), probe **`GET /installation/repositories`** (never `/user`, which 403s installation tokens — D13), distinguish 401 (bad mint/JWT) from 403 (scope) in the recorded detail; skip detail becomes "no token and no App config". Rollout honesty: `lib/` cannot be canaried per-bot — the protections are the config gate (absent config = zero change), the pre-merge harness battery, and a one-fleet manual `creds-check` run before the estate's next 06:00 tick.
- **P5 (#1275, S/M)** — scrubbed runbook (**validated by a verbatim cold walkthrough with exploration-event counting** — the house doctrine; the port deletes the fork's install flow, so this is a rewrite, not a scrub); ruleset example; `library/integrations/github.md` App-mode section (identity rationale; failure signals incl. the boot-window JWT-401 symptom; per-call gh + shim; helper-shadowing gotcha; D5 note; threat framing; respawn caveat); **key rotation/revocation section** (overlapping keys: add-new → flip path → revoke-old); README pointer; openssl prereq note. Skip/warn messages name the runbook path.
- **P6 (#1276, M)** — real App on ONE canary fleet per canary-rollout (worker not manager; evidence gate; human go; **burn-in ≥ one weekly-restart cycle**). Observations: MCP authenticates as the App; refresh survives >1h; push routes; `<slug>[bot]` attribution; mint + shim in a skill context; **wrapper RSS/CPU delta on the Pi** (mission resource-efficiency metric). Close-out: #252 status comment; #1214 comment (helper = shipped fleet-scope minting path; resolver arm stays reserved — per F9); #1213 cross-link; sunset-clause docs audit with the pre-stated conclusion **keep, amend wording**; file the estate-wide-adoption follow-up issue (sprint focus #2's "replace" is not delivered by opt-in-available; the remainder needs an owner).

**Act 2 — the fork cutover (operational; P9 gated on P6 evidence)**

- **P7 (#1277, S)** — rescue org-specific fork content into its `local/<fleet>/library/` overlay; map the fork's existing App credentials onto the new contract (zero re-registration); skim fork PR #8's `alert-routing.md` before it dies. Authority stated: the same operator owns both org and fleet — no unnamed stakeholders. **Gate:** nothing unique remains on fork main.
- **P8 (#1278, S)** — **P8a**: subagent PII sweep of the full upstream tree by content class; findings fixed upstream first. Honesty rows: upstream is already public and the fork private, so the load-bearing PII direction was Act 1's fork→upstream (gated per-PR); P8a is belt-and-braces; and neither force-push nor archive expunges pre-resync history (tag + PR refs stay reachable). **Then per locked F10 (overwrite-then-retire), in this order** (archives are read-only, so overwrite precedes archive): archive-tag fork main (`archive/pre-resync-2026-08`) → force-push upstream main over `the org fork:main` → close fork PRs #6–#9 with superseded-by pointers → **GitHub-archive the org repo**. Verify before archiving: fork main == upstream main; tag reachable.
- **P9 (#1279, M/L)** — a migration, never a bare pull: **enumerate the exact command sequence** (which of env-migrate/data-migrate/cron-migrate/memory-migrate/migrate-fleet-to-system apply, in order) and **rehearse it on an exported copy of the fork host's fleet dir** (`rehearse-*`/`coldstart-harness prepare` precedent) before touching the live host; pre-flight dirty-tree/WIP sweep across bot `projects/`; then venv reinstall → `validate` (fix the May-era fleet.yaml) → migrations → `generate` → `setup-fleet` re-enrollment (per-bot tmux servers) → canary ONE bot → `rolling-restart.sh` gated on `BRIDGE_READY` → burn-in + `reconcile-fleet` healthy. End-state origin per locked F10: the host's checkouts repoint to `Claudfather/Claudlobby` (the archived org repo is a frozen record, never a remote). Note: the composed helper reset neutralizes gh's Keychain helper inside bot contexts — dissolving the fork's original no-`gh` rationale.

## Defect ledger

D1–D9 from design validation (pre-ironclad); D10–D13 from ironclad cycle 1.

| # | Finding | Landed in |
|---|---|---|
| D1 | mint via ambient `git credential fill` silently substitutes the operator's token → helper-direct | P1; **elevated to program invariant by D10** |
| D2 | PAT-beats-App is per-DECLARATION (declared org + empty var → empty password fills → 401; App never reached) | S4 warn-text + S9 pin |
| D3 | `cache` falsifies the "no storage-backed helper remains" docstring rationale | S2 |
| D4 | shared per-user cache daemon: bot-tier `GITHUB_APP_INSTALLATION_ID` override could cross-serve tokens → fleet-tier-only v1 + warn | S4 |
| D5 | `approve` re-stores with fresh TTL → cached token can outlive the 1h life; self-healing at one failed round-trip → document | S7/P5 |
| D6 | operator reverse/push `insteadOf` (https→ssh) bypasses the credential layer, uncomposable → probe warn | S4 |
| D7 | `git-identity-no-overrides` needs a rationale AMENDMENT (noreply bot email DOES map to an account) | S5 + guardrail edit |
| D8 | `compose_bot_gitconfig` signature ripple: composer + diff.py in one commit | S2/S3 |
| D9 | helper failure contract: nonzero exit, empty stdout, stderr reason | P1, pinned S9 |
| **D10** | pathless `git credential fill` cannot match org-scoped sections → operator identity in the happy path (fork wrapper + fork creds-check hunk both carry it) → **helper-direct program invariant**; wrapper `mint_token` rewritten | Summary invariant; P2/P4 pins |
| **D11** | gh fallback accumulates for App orgs under `select_all` → silent operator substitution on helper failure, `<slug>[bot]` stamped over operator-credential pushes, ruleset inversion → **helper emits `quit=1` on hard failure** | P1; B2 pins in S8 |
| **D12** | RTC-less Pi stale-clock boot window breaks JWT minting every boot; fork wrapper refuses to start on first-mint failure → retry-with-backoff before first spawn; iat backdate; serve-live-child-through-refresh-failure; `auth_mint_failed` event | P1/P2 |
| **D13** | `/user` 403s for `ghs_` installation tokens → P4 probes `GET /installation/repositories`, 401-vs-403 distinguished | P4 |

## Test Plan

Per-phase batteries named in Steps — Lane-A pytest wrappers with stubs on a private PATH; pin-per-property compose tests; behavioral `git credential fill` with counting/failing stub helpers (no crypto, no network); the S9 harness scenario with a real openssl key and stubbed exchange; the P5 cold walkthrough. Suite-diff protocol on every PR (names + counts + rc, unsandboxed; baseline is red). Real-App validation lives in P6 behind the canary gate.

## Verification Checklist

- [ ] `claudlobby generate` on a fleet with NO `github_app`/fragment reference is byte-identical to pre-plan main
- [ ] Freshbox + validator + doctor green with the fragment unreferenced AND referenced
- [ ] Org-scoped App bot: wrapper and P4 both return `ghs_` from the App stub with ZERO gh-fallback consultations (D10 pin)
- [ ] Failing-App-helper fill does NOT fall through to gh (`quit=1`, D11 pin); direct invocation exits nonzero with empty stdout (D9)
- [ ] Mint-fails-then-succeeds: wrapper boots the child late, never dead (D12 pin)
- [ ] `validate-bot-change.sh` App scenario passes; assertions cited in the P3 PR body
- [ ] creds-check: App-configured tokenless fleet records `ok` via `GET /installation/repositories`; bare fleet still records `skip`; no token in state
- [ ] P5 runbook survives a verbatim cold walkthrough; exploration-event count reported
- [ ] P6 canary evidence: App-attributed MCP call, >1h refresh survival, routed push, `<slug>[bot]` attribution, Pi RSS/CPU delta line
- [ ] P8a sweep: zero findings outstanding before any push/archive
- [ ] P9 rehearsal on an exported fleet-dir copy passes before the live run; post-P9 `reconcile-fleet` healthy for every bot

## What NOT To Do

- Never acquire a token through `git credential fill` from any non-git surface — helper-direct only (D10 program invariant).
- Do not declare `GITHUB_PAT` in the App fragment; do not omit `secret: true`/`default_tier` from the three App vars (merged #1226 schema).
- Do not export `GH_TOKEN` at boot; the shim + per-call minting only.
- Do not let App-org failure fall through to gh (`quit=1`); do not emit the gh fallback host-generically.
- Do not change `compose_bot_gitconfig`'s signature without diff.py in the same commit (D8).
- Do not repeal `git-identity-no-overrides` — amend its rationale.
- Do not retire the sunset clauses at P6 — they fire on per-bot Apps, which this plan does not deliver.
- Do not commit real App IDs, installation IDs, usernames, or org slugs — placeholders + subagent sweep per PR.
- Do not use the unbraced `$CLAUDLOBBY_ROOT` form in the fragment (dodges the placeholder walk).
- Do not run P9 before P6's canary evidence exists; never mass-restart the fork's fleet.

## Context

Area: auth/identity + composition + fleet ops · Effort: Act 1 ≈ 5–6 PRs (M/L overall), Act 2 operational (M/L) · Risk: medium (live-path edits confined to config-gated P4; Act 2 rehearsed, gated, reversible per F10) · Priority: high (mission sprint focus #1–#2).

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Breaking live fleets via shared `lib/` | High | Net-new files inert; P4 config-gated (absent config = zero change); composed-diff-empty pin; one-fleet manual run before the estate tick |
| Silent identity substitution (the program's own failure class) | High | D10 invariant + D11 quit=1, each with dedicated pins; P6 attribution evidence |
| PII in ported docs | High | Scrub list + subagent sweeps both directions + never-stage globs |
| Pi boot-window JWT failures | Med | D12: retry/backoff, iat backdate, serve-through-failure, JSONL signal |
| New dependency (openssl) | Med | F2 ratification; opt-in-only; preinstalled both platforms; absent-binary branch tested |
| Ordering-pin regressions in composed gitconfig | Med | Per-property pins + behavioral stub tests |
| bash-3.2 parse gate | Med | Script audit; suite-diff protocol |
| Fork's live fleet down during Act 2 | High | P9 rehearsal on an exported copy; staged loop; BRIDGE_READY-gated restarts; F10 rollback story |
| Fork-unique content destroyed | Med | P7 rescue gate before P8 |
| Wrapper respawn behavior drifts on a server swap | Low | Package pinned; re-validation note; respawn-transparency pin in P2 |

## Companion plans

- `2026-07-27-per-org-git-credential-routing.md` — the seam this extends; its three ordering properties and J3 are honored unchanged.
- #1214/#1226 — merged Phase 1 is the contract this plan writes against; F9 governs the reserved `mint:github-app` socket; the resolver arm stays #252's.
- #252 — the per-bot destination; every Act-1 surface is built per-bot-ready so its second rung swaps credentials, not architecture.
