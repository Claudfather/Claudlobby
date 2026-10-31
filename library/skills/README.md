# library/skills/

Slash-command-style action packages. Each skill is a directory with a `SKILL.md` (the prompt) and optional helper files. Activation copies each selected skill into the bot's `.claude/skills/` directory, frozen at that release.

## What belongs here

One directory per skill. Each must contain at least `SKILL.md` — the prompt Claude Code executes when the user invokes `/<skill-name>`. Helper scripts, templates, or config files go alongside it.

```
library/skills/
├── _telegram-formatting.md
├── dispatch/
│   └── SKILL.md
├── fleet-status/
│   └── SKILL.md
└── prs/
    └── SKILL.md
```

## SKILL.md frontmatter

`SKILL.md` uses Claude Code's own native frontmatter — `name:`, `description:`, `argument-hint:` — **not** the library-wide `title:`/`description:` convention used elsewhere in `library/`:

```yaml
---
name: status
description: "Use when the human asks a manager what's the latest, what moved, or what is waiting on them. Manager readout for the human: what moved, and what is waiting on their decision."
argument-hint: "[24h|7d|since <ISO>]"
---

# Status
```

The compositor also reads three optional lists of permission rules from this frontmatter: `tool_grants:`, `ask_grants:` and `deny_grants:` (see Grant contract below).

**Open the description with when to use the skill.** Claude Code lists every installed skill to the model by its description, so the description is what gets a skill invoked without someone pointing at it. Say the situation first, in a sentence that opens with `Use when` (or `Use before`, `Use to`, `You MUST use this before`), then what the skill does. `tests/test_skill_descriptions.py` fails any skill here whose description does not open with one (#2330).

The H1 is a human-cased version of `name` (`status` → `# Status`, `fleet-pulse` → `# Fleet Pulse`), but unlike every other library category it is **not** stripped or demoted — `composer.py::link_skills` only creates a filesystem symlink into the bot's `.claude/skills/`; it never reads or parses `SKILL.md` content, so it never passes through `load_library_item`/`_demote_headings`. Claude Code reads the file natively at invocation time, H1 and all.

## Grant contract (`tool_grants:`)

A skill may declare the tools and commands it invokes as an additive grant contract in `SKILL.md` frontmatter, alongside the native fields:

```yaml
---
name: dispatch
description: "..."
tool_grants:
  - "mcp__github__*"      # an mcp__ glob (trailing * only)
  - "Bash(tmux *)"        # a scoped Bash(<command pattern>) grant
  - "Read"                # a bare tool name
---
```

Each entry is one of three shapes (the grant grammar): an `mcp__<server>__*` glob, a `Bash(<cmd> *)` pattern, or a bare CamelCase tool name. The compositor validates each entry's shape and warns on a malformed grant or an over-broad bare `Bash` (declare a scoped `Bash(<cmd> *)` instead). For a multi-file skill the contract lives on `SKILL.md` only — sibling files in the folder are ignored.

Two more lists in the same frontmatter take entries in the same grammar and are validated the same way: a malformed entry is a `grant-malformed` warning, a path rule with a single leading slash is an error, and a bare `Bash` is not flagged in either list.

- **`ask_grants:`** — composed into `permissions.ask` in the bot's `.claude/settings.local.json`. Claude Code asks before running a matching call in every permission mode, auto included, and an ask rule wins over an allow rule that also matches. Use it for a verb that must stay a person's decision until the fleet grants it. The fleet grants one by naming the same rule, in exact text, under `tool_permissions.allow` (fleet defaults or the bot); the compositor then drops it from `permissions.ask`, so the call runs without a prompt. A broader allow rule, or another library item's `tool_grants`, does not drop it. `permissions.ask` is written only when a rule survives, so a bot with none keeps the same file. An ask rule that carries an absolute path is held to the same anchoring rule as an allow grant.
- **`deny_grants:`** — appended to `permissions.deny`. A matching call is refused in every permission mode, and no allow rule overrides it. Use it to stop a hand-written route around a skill's helper: `gitguardian/SKILL.md` denies `Bash(curl *gitguardian.com*)` and `Bash(wget *gitguardian.com*)`.

A rule matches the command's text, so it stops the plain form of a call and not every way of making it (another client, an absolute path, `bash <script>`). Do not present one as the only gate on a change.

## Shared partials (leading underscore)

A flat `_<name>.md` file (leading underscore, no directory) at the top of `library/skills/` is a shared partial — prose meant to be pulled in by reference from inside one or more `SKILL.md` bodies, not an invocable skill itself. The only current example, `_telegram-formatting.md`, uses the library-wide `title:`/`description:` frontmatter (unlike its `SKILL.md` siblings) and is referenced via a relative markdown link, e.g. in `status/SKILL.md`: `` [_telegram-formatting.md](../_telegram-formatting.md) ``. This works specifically because skills are symlinked rather than copied (see Composition below) — the symlink at `<bot>/.claude/skills/status/SKILL.md` still points back into `library/skills/status/SKILL.md`, so the relative `../_telegram-formatting.md` resolves back to the shared library file. `fleet.yaml` never lists `_telegram-formatting` itself; it's pulled in implicitly by whichever skills link to it.

## Shell helpers

A skill may ship an executable shell helper alongside its `SKILL.md` — a script the body invokes rather than inlining the logic as prose. `printify/` is the reference: `printify_api.sh` plus `fixtures/` and its own `test.sh`. (The mechanisms below key on the `.sh` extension; a helper in another language is not covered yet.)

```
library/skills/printify/
├── SKILL.md            # the prompt — invokes the helper
├── printify_api.sh     # the executable (0755 in git)
├── mcp-vs-api.md       # supplementary in-skill doc, linked from SKILL.md
├── test.sh             # the helper's own suite
└── fixtures/           # synthetic data backing the hermetic assertions
```

Invoke it through the bot's skill directory, never a library path — the symlink resolves the same whether the skill is a fleet overlay or shared library content:

```bash
"$BOT_DIR/.claude/skills/<name>/<helper>.sh" <args>
```

**Helper here, or `library/tools/`?** `tools/` exists for **compose-time parameterization** — a `tool.yaml` manifest and a Jinja template rendered per-bot into `$BOT_DIR/tools/`. A helper with no compose-time params gains nothing from that machinery and splitting one capability across two library categories costs cohesion. The test: does the script need a value baked in at `generate` time? Yes → `library/tools/`. No, it reads everything from runtime env → keep it in the skill dir.

Two things `tools/` does for free that a skill-dir helper owns itself:

**Execute bit.** `tools/` chmods its rendered output 0755; a skill is *symlinked*, so the helper runs at whatever mode git recorded — checked in 100644 it cannot run on a fresh clone. Verify with `git ls-files -s library/skills/<name>/`, fix with `git update-index --chmod=+x <path>`, and pin it in a test so the next author does not have to remember.

**Audit surface.** `claudlobby config validate --runtime` audits rendered `tools/`; a skill-dir helper sits outside that sweep. If `SKILL.md` declares `tool_grants` for the helper, scope them (`Bash(<cmd> *)`, never bare `Bash`) and re-run `claudlobby --fleet <f> config validate --runtime` to confirm no `orphan_grant` or over-grant finding. If the helper is the intended route to a service, the skill can also declare `deny_grants` that refuse a hand-written call around it, as `gitguardian` does (see Grant contract above).

The parse gate you get for free either way: `tests/test_bash_parse.py` runs `bash -n` over `claudlobby/_runtime_scripts/` **and** every `library/**/*.sh`. Just mind the bash 3.2 rule from the root CLAUDE.md — no apostrophes in comments inside `$( )`, because macOS `/bin/bash` does not strip them and one corrupts quoting for the rest of the file.

**Test the helper, and wire it into pytest.** A shell suite that CI never runs is not a gate. Ship the suite next to the helper, then pick the right runner:

- `tests/test_sh_suites.py` already globs every `tests/test_*.sh` and runs it with the ambient env, on the contract that a suite never reaches a real service. Put the suite there when it can meet that contract.
- When the suite needs a *scrubbed* env — because it has a live arm that must stay dormant in CI, or it would otherwise pick up real credentials from a developer's shell — it cannot use that glob. Keep it beside the skill and add a `tests/test_<skill>_skill.py` wrapper that `subprocess.run`s it with the credentials stripped, as `tests/test_printify_skill.py` does. Drive the CI-facing arm from `fixtures/` and dry-run paths, and skip the module when a binary it needs (`jq`) is absent.

Live credentials never belong in CI. If you take the second path, say why in the wrapper's docstring so the divergence reads as deliberate.

## Composition

In a sealed release, activation copies the selected skills into each bot as frozen bytes; only the development-only composition the test harness uses links them. So an edit to `library/skills/<name>/SKILL.md` reaches a running fleet only through a release, `config plan` and `host activate` (`documentation/fleet-update-lifecycle.md`, "Refreshing a skill's installed copies"). Listed in fleet.yaml: `skills: [dispatch, fleet-status, prs]`.

## Naming

Lowercase, hyphenated directory names matching the slash command: `fleet-status/` → `/fleet-status`. A leading underscore (`_name.md`) marks a shared partial rather than an invocable skill (see Shared partials above).
