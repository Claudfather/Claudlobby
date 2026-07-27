# Per-org git credential routing — defect report + design proposal

**From:** a downstream consumer fleet. We found this; you own the fix shape.
**Status:** defect reproduced and root-caused. Phases 1–3 implemented in this PR as a *proposal*.
Forks F1/F2 open. One open question below (§Open question) where our explanation is inferred from
behaviour rather than from a documented contract — if you know the answer, it is a one-line reply
and we would rather be corrected than shipped around.
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

### Open question — the part we did not derive from a documented contract

We verified the *behaviour* above repeatedly, but two git semantics we are relying on were inferred
from observation, not from git's documented contract:

1. **Does an empty `helper` value reset the list across config keys, or only within the same key?**
   Observed: an org-scoped helper set in an *earlier* section is discarded by a later empty
   `credential.https://github.com.helper`. That implies one list per credential lookup, reset
   globally. We did not confirm this is specified rather than incidental.
2. **Is "first helper that answers wins" guaranteed**, or does git have a specificity rule we are
   accidentally satisfying by ordering? Observed: an org-scoped section placed *after* the generic
   one never fires, which suggests no specificity rule — but we are asserting a negative.

Both matter, because the whole design below is ordering-sensitive. If either is incidental rather
than contractual, the composed file is fragile across git versions and you will want a different
shape. **We are not claiming to have settled this.** A team living in this code may recognise it
instantly.

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
	useHttpPath = true                      # else an org-scoped section never matches
	helper =                                # discard what the include installed
[credential "https://github.com/OrgA"]
	helper = "!f(){ echo username=x-access-token; echo password=$ORGA_GITHUB_PAT; };f"
[credential "https://github.com"]
	helper = !<gh> auth git-credential      # default for every undeclared org
```

Four ordering properties are load-bearing (`useHttpPath`; include first; reset after the include;
org helper before the generic fallback). Each is pinned by its own test, because a reorder breaks
routing silently.

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
| `claudlobby/validator.py` | warn (never fail) on a declared var unset in every `.env` tier |
| docs | `fleet-yaml-schema.md`, `environment-variables.md`, `library/tools/README.md` (the env-name contract), `fleet.yaml.example` |

23 tests: the four ordering properties, secret hygiene (name referenced, never expanded; pasted-token
prefixes rejected), multi-org, opt-out inertness, both config tiers with provenance-correct errors,
and a real `git credential fill` behavioural check with stubbed helpers (no network, no real tokens).

Notable: `SHELL_IDENT_RE` alone cannot reject a pasted token — `ghp_xxx` *is* a valid shell
identifier — so there is an explicit GitHub-token-prefix guard. A test found that.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `git config --global` in-session lost on regenerate | Medium | `diff` reports `.gitconfig` drift (implemented + tested); generated-header warning |
| Token leaked into a world-readable composed file | **High** | helper embeds `$VAR`, never a value; token-prefix guard rejects a pasted secret; both pinned by tests |
| Adding `git_credentials` needs a **restart**, not `/reload` | Medium | `bot.conf` is sourced at pane creation; `reload-fleet.sh` deliberately does not restart. Documented in `environment-variables.md` |
| PAT reaches the pane non-exported if a bot's tmux **server** outlives a restart | Low/narrow | `start-bot.sh` sources `.env` tiers *before* its `set -a` block; works today because the parent exports and a fresh server inherits. Not fixed here — it is a pre-existing `start-bot.sh` ordering issue, one line, and changing bot-launch env ordering deserves its own empirical validation |
| PAT expiry presents as this same `403` | Medium | §2's `POST /git/refs` probe distinguishes them; documented |
| Ordering assumptions turn out to be incidental | **See §Open question** | unresolved — this is the one we want your read on |

## 9. Verification

- [ ] `grep -c 'password=\$' <bot_dir>/.gitconfig` ≥ 1 and zero literal-token matches
- [ ] `git credential fill` per org returns the declared token; an undeclared org returns the host default
- [ ] a fleet declaring no `git_credentials` composes **no** `.gitconfig`, no `GIT_CONFIG_GLOBAL`, and `diff` reports no drift
- [ ] `git config --get user.email` under the composed config equals the operator's global value
- [ ] hand-edit the composed file → `claudlobby diff` reports `.gitconfig drift`
- [ ] `pytest tests/test_composer.py tests/test_config.py -k "GitCred or GitConfigLifecycle or GitCredentials"`

All six were run. Full suite: zero new failures against a pristine-`main` baseline (identical
FAILED/ERROR sets; the pre-existing failures are macOS host gaps — systemd, GNU coreutils).
