# Per-org git credential routing — defect report + design proposal

**Defect found and root-caused by:** a downstream consumer fleet, who proposed the fix shape.
**Owned by:** crog-eng-team, who reviewed it, resolved the open question, and carried the fixes.
**Status:** open question **resolved** (both semantics are git's documented contract — §Open
question); review findings addressed; forks F1/F2 still awaiting a ratifier.
**Branch:** `feat/per-org-git-credential-routing` off `main` @ `e413675`.

Org names below are placeholders (`OrgA`, `OrgB`) except where the real target matters:
`Claudfather/Claudlobby` is this package's own repo, which is where the failing push went.

---

## 1. The defect

A bot pushing to a second GitHub org gets `403` on `git push`. Nothing is misconfigured.

```
$ git push origin HEAD:refs/heads/probe
remote: Permission to Claudfather/Claudlobby.git denied to <user>.
fatal: unable to access 'https://github.com/Claudfather/Claudlobby.git/': The requested URL returned error: 403
```

The message reads as a token-scope problem. It is not.

## 2. Evidence

**The token has write access.** `POST /git/refs` with an existing ref returns `422`, not `403`, and
GitHub echoes the granted permission:

```
HTTP 422  {"message": "Reference already exists"}
x-accepted-github-permissions: contents=write
```

**git is not using that token.** `git credential fill` for `github.com` returns a *different* one:

```
$ printf 'protocol=https\nhost=github.com\n\n' | git credential fill
username=<user>
password=<token B>
```

**Token B is `gh`'s stored token.** Compared by `sha256` prefix (never the values):

| source | sha256[:12] |
|---|---|
| `gh auth token` | `6513b6b448` |
| the second org's PAT (`GITHUB_PAT`) | `6513b6b448` |
| the PAT we intended for this push | `a47c7d9d6d` |

`gh auth token` and the *other* org's PAT are the same token. Fine-grained PATs are
single-resource-owner, so it cannot carry write access to both orgs.

**Where it comes from.** `gh auth setup-git` had written into `~/.gitconfig`:

```
$ git config --show-origin --get-regexp '^credential'
file:/Users/<u>/.gitconfig   credential.https://github.com.helper
file:/Users/<u>/.gitconfig   credential.https://github.com.helper  !/opt/homebrew/bin/gh auth git-credential
```

Two values for the same key; the first is **empty**.

**Reproducer / fix, control vs treatment.** Same command, same repo, no `-c` flags:

```
default config                                  -> 403 denied
GIT_CONFIG_GLOBAL=<per-org config>  git push    -> * [new branch]  probe
```

**Ruled out:**

- **Token scope** — see the `422` + `x-accepted-github-permissions` above.
- **A stale/expired credential in the macOS keychain** — `security find-internet-password -s github.com`
  returns nothing. There is no `github.com` git credential in the keychain at all; only `gh`'s own
  `gh:github.com` item. `osxkeychain` is configured as a *generic* `credential.helper` in the
  Xcode gitconfig and is never reached for this host.
- **Branch protection** — `PATCH /repos/...` with the same credential also `403`s, so it is not
  ref-specific.

## 3. Mechanism

Two facts compose into the failure:

1. `gh`'s helper is registered for `https://github.com` and answers with the *other* org's token.
2. Anything we add via `-c credential.helper=…` is read **after** global config, and git uses the
   **first** helper that returns a credential — so ours was never consulted.

This also explains the workaround that circulated internally: `-c credential.helper=` (empty)
followed by `-c credential.helper=<ours>` works because an empty value **resets the accumulated
helper list**, discarding `gh`'s and leaving ours the only one. That is why it looked like a
keychain problem — the reset appeared to "clear a cached credential" when it was clearing a
*config list*.

### Open question — RESOLVED: both semantics are contractual

The proposers flagged that two git semantics this design leans on were inferred from behaviour
rather than a documented contract, and that if either were incidental the composed file would be
fragile across git versions and need a different shape. **Both are contractual**, on three
independent lines of evidence, so the shape stands.

1. **An empty `helper` resets the list across config keys — yes.** There is no "across keys" to
   begin with: `urlmatch_config_entry()` (`urlmatch.c`) strips the URL from the key and
   *synthesises* a generic one (`credential.helper`) before dispatch, so a URL-scoped and a generic
   helper entry are indistinguishable by the time `credential_config_callback()` sees them — the
   URL is a match filter only. That callback then calls `string_list_clear()` on an empty value,
   clearing the single accumulated list whatever contributed to it. Documented in
   `gitcredentials(7)`: *"If `credential.helper` is configured to the empty string, this resets the
   helper list to empty."*
2. **First helper that answers wins, and there is no specificity rule — yes.** `credential_fill()`
   returns as soon as one helper yields both a username and a password. Documented: *"Once Git has
   acquired both a username and a password, no more helpers will be tried."* The no-specificity
   half does not require asserting a negative — git states it positively:
   `credential_apply_config()` sets `select_fn = select_all` (which returns 0 unconditionally),
   **explicitly opting credential config out** of urlmatch's `cmp_matches()` most-specific-wins
   arbitration that its other consumers use.

**Stability:** the `gitcredentials` wording for both paragraphs is byte-identical at `v2.20.0`
(2018) and `v2.39.5`. Confirmed empirically with 9 stub-helper probes on git 2.39.5.

Two further properties this shape silently relies on, now pinned by tests:

- **The reset is URL-filtered** — a non-matching url-scoped empty helper does *not* clear the list,
  so scoping the reset to `https://github.com` leaves the operator's helper intact for every other
  host. An un-scoped reset would silently break GitLab pushes.
- **Org sections match a path prefix only at a `/` boundary** — `Claud` does not capture
  `Claudfather`. Real org names collide this way and the failure would be a wrong token, not an
  error.

### Correction: `useHttpPath` is not what makes an org section match

The original draft listed `useHttpPath = true` as the first of four ordering properties, on the
reading that without it "git matches on protocol+host only and an org-scoped section never matches
at all". That is not what it does — routing is identical with the line stripped.

`credential_apply_config()` builds its match URL from `c->path` and runs the config read (and so
the URL matching) at `credential.c:197`; the `!use_http_path` strip is at `:205`, *after*. The
transport populates `c->path` from the remote URL, so the path is present at match time either way.

The line is **kept deliberately**, for a different reason: that read-then-strip order is a git
*internal*, where the two semantics above are documented, so setting `useHttpPath` turns the one
undocumented dependency in this design into a configured guarantee. Its actual effect — forwarding
`path=` to helpers — costs nothing here, because the reset drops every storage-backed helper for
this host, leaving none that would key credentials per-repo.

## 4. Proposal

Declare the binding, compose the routing, activate per bot:

```yaml
fleet:
  defaults:
    git_credentials:          # org -> env var NAME
      OrgA: ORGA_GITHUB_PAT
  bots:
    somebot:
      git_credentials:        # merges over fleet, per-org
        OrgB: ORGB_GITHUB_PAT
```

Composes `<bot_dir>/.gitconfig` + `GIT_CONFIG_GLOBAL` in `bot.conf`:

```
[include]
	path = <operator ~/.gitconfig>          # identity/aliases preserved
[credential "https://github.com"]
	useHttpPath = true                      # forwards path= to helpers (see correction above)
	helper =                                # discard what the include installed
[credential "https://github.com/OrgA"]
	helper = "!f(){ echo username=x-access-token; echo password=$ORGA_GITHUB_PAT; };f"
[credential "https://github.com"]
	helper = !<gh> auth git-credential      # default for every undeclared org
```

Three ordering properties are load-bearing (include first; reset after the include; org helper
before the generic fallback). Each is pinned by its own test, because a reorder breaks routing
silently. `useHttpPath` is a fourth line but not one of the three — see the correction above.

Verified per-org resolution through one composed file:

```
OrgA/repo.git  -> a47c7d9d6d   (declared org's token)
OrgB/repo.git  -> 6513b6b448   (host default, via gh)
```

`x-access-token` as username is GitHub's documented placeholder for PAT auth; verified against a
real push. A real login would be both PII and wrong for any other operator.

## 5. Judgment calls — defer to your house pattern

These are our opinions, not findings. If you have an existing convention, ours should yield.

**J1 — Multi-org credentials as a first-class `fleet.yaml` field, rather than a tool or a
per-checkout git config.** We initially scoped a `library/tools/claudlobby-push/` wrapper. We think
that is wrong: a wrapper only fixes pushes that remember to call it, and agents run bare `git push`
(and `gh`) constantly. The root cause is credential *selection*, which git already solves
declaratively. But "declare it in `fleet.yaml`" versus "leave it to the operator's `~/.gitconfig`"
is a boundary question about what the compositor should own, and you own that boundary.

**J2 — Shared package, not a fleet overlay.** Our reading: env var *names* are contracts and belong
in git; only *values* belong in the gitignored `.env`. We inferred this from `library/mcp/*.json`
being tracked while declaring `${VAR}`, and from the tools README's own `env:` example. We have
written it down explicitly in `documentation/environment-variables.md` because it is currently
implicit and at least one of us reasoned to the opposite conclusion from first principles. **If that
inference is wrong, that doc change is the first thing to revert.**

**J3 — `x-access-token` hardcoded rather than configurable.** GitHub ignores the username for PAT
auth, so a config field could only ever hold one useful value. If you support non-GitHub remotes
here, this needs to become a parameter.

**J4 — Per-bot granularity.** Two bots on one fleet get byte-identical composed files. We think
that is correct (it matches every other per-bot artifact, and it permits different bots holding
different tokens for the same org), but it is a duplication call you may weigh differently.

## 6. Forks needing a ratifier

**F1 — Activation mechanism.** `GIT_CONFIG_GLOBAL` (our lean) is bot-scoped, deterministic, and
needs no operator edit. Cost: `git config --global` *inside* a bot session writes to the composed
file and is lost on regenerate. Mitigated by `diff` (below), not eliminated.
Rejected alternatives: an `include` added to the operator's `~/.gitconfig` cannot express per-bot
credentials at all, and is defeated by the include-side reset (verified); per-repo `.git/config` in
each checkout is not composed, so a fresh clone silently reverts to broken.

**F2 — Generic fallback resolution.** We resolve `gh` at compose time and omit the fallback line
when absent, rather than emitting a bare `gh` a non-login git process may not resolve. The
alternative is requiring every org to be declared — more explicit, breaks undeclared-org pushes.

**F3 — Binding key.** Locked to a dedicated field rather than reusing `scope.org`: `scope.org` is
singular and the motivating case needs two orgs.

## 7. What we changed

| File | Change |
|---|---|
| `claudlobby/config.py` | `git_credentials` on `BotConfig`; `_parse_git_credentials(raw, *, where)` per tier, merged at the call site |
| `claudlobby/composer.py` | `compose_bot_gitconfig(bot)`; `GIT_CONFIG_GLOBAL` in `bot.conf`; registered in `collect_env_contracts` (fleet tier) so scaffold/doctor/freshbox see the var |
| `claudlobby/diff.py` | `.gitconfig` drift detection — F1's mitigation, so an in-session edit is not silently lost |
| `claudlobby/validator.py` | warn (never fail) on a declared var unset in every `.env` tier; and on an `[include]` target that is missing or carries no `user.email` — probed once per run via `git config --file --includes` |
| `claudlobby/path_audit.py` | `.gitconfig` added to `_WIRING_STATIC`, so the L2 scan covers the new artifact |
| `claudlobby/freshbox.py` | `_externals_report` extended — the include target and resolved `gh` join the declared-by-construction externals (INFO, beside mount targets and the vault path), and an absent include target is a FAIL |
| docs | `fleet-yaml-schema.md`, `environment-variables.md`, `library/tools/README.md` (the env-name contract), `fleet.yaml.example` |

37 tests: the three ordering properties, secret hygiene (name referenced, never expanded;
pasted-token prefixes rejected), multi-org, opt-out inertness, both config tiers with
provenance-correct errors, and real `git credential fill` behavioural checks with stubbed helpers
(no network, no real tokens) covering per-org resolution, the URL-filtered reset, the org-name
prefix boundary, and both halves of what `useHttpPath` does and does not do.

Notable: `SHELL_IDENT_RE` alone cannot reject a pasted token — `ghp_xxx` *is* a valid shell
identifier — so there is an explicit GitHub-token-prefix guard. A test found that.

Also found by a test: the identity probe needs `git config --file --includes`. `--includes` is off
by default for a `--file` read but ON when git reads the same file as global config — which is how
the bot reads it — so omitting it warns about identities that resolve perfectly at runtime.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `git config --global` in-session lost on regenerate | Medium | `diff` reports `.gitconfig` drift (implemented + tested); generated-header warning |
| Token leaked into a world-readable composed file | **High** | helper embeds `$VAR`, never a value; token-prefix guard rejects a pasted secret; both pinned by tests |
| Adding `git_credentials` needs a **restart**, not `/reload` | Medium | `bot.conf` is sourced at pane creation; `reload-fleet.sh` deliberately does not restart. Documented in `environment-variables.md` |
| PAT reaches the pane non-exported if a bot's tmux **server** outlives a restart | Low/narrow | `start-bot.sh` sources `.env` tiers *before* its `set -a` block; works today because the parent exports and a fresh server inherits. Not fixed here — it is a pre-existing `start-bot.sh` ordering issue, one line, and changing bot-launch env ordering deserves its own empirical validation |
| PAT expiry presents as this same `403` | Medium | §2's `POST /git/refs` probe distinguishes them; documented |
| Ordering assumptions turn out to be incidental | **Closed** | resolved: both semantics are git's documented contract, wording unchanged v2.20.0 → v2.39.5, and specificity is explicitly disabled in `credential_apply_config()`. See §Open question |
| Operator's `~/.gitconfig` absent or identity-less | **High** | git ignores a missing `[include]` silently, so routing works while `user.email` is unset and every commit dies `rc=128 Author identity unknown`. `freshbox` FAILs (`missing_external`) and `validate` warns with that exact symptom; `generate` still proceeds (operator gap, not a compose error) |
| A composed artifact drifting outside the L2 path guard | Medium | `.gitconfig` is in `path_audit._WIRING_STATIC`, so a fleet-shaped absolute in it is a hard error like one in `bot.conf`. Its two external targets (operator gitconfig, resolved `gh`) are surfaced by freshbox's externals report rather than left silent |

## 9. Verification

- [ ] `grep -c 'password=\$' <bot_dir>/.gitconfig` ≥ 1 and zero literal-token matches
- [ ] `git credential fill` per org returns the declared token; an undeclared org returns the host default
- [ ] a fleet declaring no `git_credentials` composes **no** `.gitconfig`, no `GIT_CONFIG_GLOBAL`, and `diff` reports no drift
- [ ] `git config --get user.email` under the composed config equals the operator's global value
- [ ] hand-edit the composed file → `claudlobby diff` reports `.gitconfig drift`
- [ ] `pytest tests/test_composer.py tests/test_config.py -k "GitCred or GitConfigLifecycle or GitCredentials"`

All six were run. Full suite: zero new failures against a pristine-`main` baseline (identical
FAILED/ERROR sets; the pre-existing failures are macOS host gaps — systemd, GNU coreutils).

### Canary for the review fixes (owning team, Linux)

Composer changes get a canary before rollout. A throwaway fleet on a clean `/tmp` root with a fake
`HOME` and a stub `gh` first on `PATH` (so compose-time resolution picks it up and the real `gh` is
never executed), driving the real `claudlobby` CLI:

- routing unchanged by the fixes — declared org → its own token, undeclared org → host default,
  operator identity survives the include, no literal token in the composed file
- **fix #2, before/after on the same planted flat path** — PR head: `0 finding(s): []`; with the
  fix: `1 finding(s): ['.gitconfig']`
- **fix #3, before/after on an absent operator gitconfig** — PR head: `freshbox --strict` reports
  *"Self-contained"* while `user.email` is unset; with the fix: `freshbox` FAILs `missing_external`
  and exits non-zero, `validate` warns naming `Author identity unknown`, and `generate` still
  succeeds (an operator gap must not block composition)

Full suite on Linux: **2066 passed, 0 real failures**. The single `FAILED`
(`test_tmux_env::test_no_cross_contamination`) asserts bot-name markers do not leak into a bot's
env and trips on a checkout path that literally contains a bot name; 21/21 pass from a neutral
path. Not attributable to this branch.
