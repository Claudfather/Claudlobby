# The naked-bot observation gate (#1168 Phase 3)

**What a bot receives when its fleet declares nothing.** Observed by composing a
real fleet and reading the result — never reasoned about from the source.

This is the gate Phase 2 has to clear. Each Phase 2 PR populates one entity
type's `DEFAULT_*`; this is what those PRs diff against, so a default that lands
shows up as a delta instead of being argued about in a review thread.

```bash
lib/naked-bot-observe.py                                    # human-readable
lib/naked-bot-observe.py --json                             # the record format
lib/naked-bot-observe.py --baseline documentation/baselines/naked-bot-2026-08-12.json
```

The last form is the gate: exit 1 and a per-(arm, entity type) list of what
moved. **A Phase 2 PR is expected to make it exit 1** — the requirement is that
the delta is named in the PR body, not that it is absent.

## The baselines — diff against the newest, cite the anchor

| file | ref | what it is |
|---|---|---|
| `naked-bot-2026-08-11.json` | `b196936` | **The anchor.** Phase 1 merged, zero defaults admitted. Frozen; do not overwrite. |
| `naked-bot-2026-08-12.json` | `a48e60f` | **The current diff target.** Phase 2's first admission (`protocols`). |

Diff a new PR against the **newest**; a PR that diffed against the anchor would
re-report every previously-argued admission as its own delta, and the reviewer
would have to subtract history by hand to find the change under review.

**The anchor is kept rather than overwritten** because it is the only recorded
state in which the estate's pre-registry behaviour is legible as a whole. It is
reproducible in principle — `--ref b196936` re-derives it — but a claim that has
to be re-derived to be checked usually is not checked. Add a dated file per
admission; never edit one in place.

The rest of this section describes the anchor, and holds for both except where
a finding below is marked resolved.

Recorded at `b196936` (Phase 1 merged, zero new defaults shipped), the last
commit at which the pre-change baseline is observable at all.

Fleet under test: one bot, one expertise, nothing else. `expertise` is a
**required** field, so a fleet declaring literally nothing does not validate —
that requirement is itself part of the baseline. The expertise is a single
sentinel line in the probe's own overlay, so any other prose in the composed
`CLAUDE.md` is attributable to a default or the template rather than to the role
that happened to be picked.

| type | tier | registry | composes an instruction | composes a file |
|---|---|---|---|---|
| expertise | INSTRUCT | — | *(no instruction surface — becomes the title + body)* | — |
| skills | INSTRUCT | — | *(no instruction surface)* | — (`.claude/skills/` exists, empty) |
| **protocols** | **INSTRUCT** | `shared-documentation` *(admitted after this baseline — §1)* | **6 sections** | — |
| principles | INSTRUCT | — | none | — |
| post_actions | INSTRUCT | — | none | — |
| guardrails | RESTRICT | `claudlobby-dev-in-projects` | 1 section | — |
| **permissions** | **RESTRICT** | **—** | none | `.claude/settings.local.json` — **7 allow entries, undeclared** |
| mcp | WIRE | — | *(no instruction surface)* | `.mcp.json` (`mcpServers: {}`) |
| tools | WIRE | — | *(no instruction surface)* | — (no `tools/` dir) |
| integrations · resources · lessons | WIRE | — | none | — |

`freshbox --strict` on this fleet: **rc 0, "Self-contained"** — and see §3 for why
that is not a clean gate.

## Findings

Five, all measured on real composes at `b196936`. Each is a Phase 2 blocker or a
correction to the plan's evidence, not a nice-to-have.

Findings 1 and 5 are the same shape and worth reading together: **two entity
types already have a compositor-side default that does not flow through the
registry**, in a phase whose stated position is that no new default has shipped.

### 1. A naked bot receives an INSTRUCT-class instruction that no registry entry accounts for

`composer.py:1554-1557` appends the `shared-documentation` protocol whenever
`paths.shared_docs` is truthy:

```python
protocol_names = list(bot.protocols)
if paths.shared_docs and "shared-documentation" not in protocol_names:
    protocol_names.append("shared-documentation")
```

It lands **six** `###` sections on a bot that declared no protocols: *Shared
Documentation, Pre-Work Checks, Writing Convention, INDEX.md Maintenance,
Lifecycle, Promotion Flow*.

`protocol_names` is the **already-merged** value, so this append happens
downstream of the `_merge_lists` pathway the registry feeds. It is a fourth
resolution pathway for an INSTRUCT type, with no membership test written against
it and no way to switch it off — see §2.

The registry's claim that no INSTRUCT default is present is true **of the
registry** and false **of the bot**. That gap is exactly what the plan's hard
requirement — "spun up and what lands OBSERVED, not reasoned about" — exists to
catch, and reading `defaults.py` alone would never have shown it.

> **RESOLVED — the entry is now registered, and two claims above are corrected.**
>
> `shared-documentation` is `REGISTRY["protocols"]`, `grandfathered`, removable
> with `system_defaults.protocols: false`. The default path composes
> byte-identically: same `sha256` before and after, and the `[baseline]` arm's
> only delta is `registry_entries: [] -> ['shared-documentation']`.
>
> **"Whenever `paths.shared_docs` is truthy" is not "unconditional".** Measured:
> a **root-mode** naked bot (no `--fleet`) composes no shared-documentation
> section at all, because `Paths.shared_docs` is None without a `fleet_dir`. The
> append fired for every *overlay-mode* fleet — which is every real fleet on this
> estate, so the effect was estate-wide — but a default moved into `load_fleet`'s
> merge ungated would have newly instructed every root-mode bot. Hence
> `defaults.AVAILABILITY_GATES`. **This gate observes overlay mode only**, so it
> could not have shown that; see *What the gate does NOT cover*.
>
> **A second, worse instance found while arguing the tier test.** A bot with
> `claudron_vault_path` set composes the template's vault section — *"reached
> through the Claudron door, NOT by reading a raw doc tree"* — and then this
> protocol telling it to hand-scan `planning/active/INDEX.md`. Both land in one
> file; the append never checked for a vault. That is a live contradiction in
> composed instructions on every vault-wired bot, it pre-dates the registration,
> and it is deliberately **not** fixed there: the fix changes composed
> instructions on the default path, which needs its own decision and its own
> before/after. It is why the entry is `grandfathered` rather than argued as
> clearing the INSTRUCT bar — it half-fails it.

### 2. The per-entity-type opt-out surface does not exist for 11 of 12 types

> **PARTLY RESOLVED — now 10 of 12.** `system_defaults.protocols` exists and is
> measured working (`optout:protocols` and `control:kill-switch` are the only two
> arms whose composed instructions move). Ten types still have no opt-out, and
> **an unrecognised key is still silently dropped**, so the "cannot tell a
> working opt-out from a typo" half of this finding stands untouched.

`SystemDefaultsConfig` reads exactly five keys: `enabled`, `hooks`, `timers`,
`observability`, `guardrails`. Of the twelve entity types, only `guardrails` has
an opt-out at all.

Measured across all twelve `optout:<type>` arms — every one is a **no-op**, and
`generate` exits 0 with no error or warning. The `control:unknown-key` arm
(`system_defaults: {not_a_type: false}`) behaves identically, so **a fleet cannot
tell a working opt-out from a typo.**

This corrects the plan's Evidence section, which reads:

> **Opt-out surface generalises**: `system_defaults` is already per-key.

It is per-key, but over a fixed set of five keys, only one of which is an entity
type. The plan's checklist item — *"for each of the 12 types,
`system_defaults.<type>: false` demonstrably removes the default"* — is **not
satisfiable today** for eleven of them. Phase 2 has to build that surface, and
an unrecognised key should be rejected rather than silently dropped.

**Positive control, and it is load-bearing:** `system_defaults.guardrails: false`
*does* remove its section, and so does the `system_defaults: false` kill switch.
Without that control, eleven no-ops would read as eleven findings rather than one
finding plus a working instrument.

### 3. `freshbox` cannot see the tier the gate exists to protect

The plan names `claudlobby freshbox` the **primary** instrument. On the naked
fleet it reports `OK — Self-contained` at rc 0 while the bot is carrying six
undeclared protocol sections.

`freshbox.py` never opens `CLAUDE.md` (zero matches). It audits **grants** —
`settings.local.json`, `.mcp.json`, `bot.conf`, rendered `tools/`. Composed prose
is not a grant, so freshbox is blind to the INSTRUCT class **by construction**.

It remains correct for the WIRE/RESTRICT half and this harness runs it and
records its verdict. But it is a floor, not the gate: an INSTRUCT default could
land fleet-wide with freshbox green throughout.

### 4. Populating a registry constant would change nothing — the wiring is absent for 11 of 12

> **PARTLY RESOLVED — now 10 of 12, and the second wiring is NOT the first one's
> shape.** `protocols` resolves through `defaults.resolve()` in `composer.py`,
> deliberately **not** through `load_fleet`'s `_merge_lists`: its entry is
> availability-gated on `Paths.shared_docs`, and `load_fleet` takes a bare path
> and never learns whether the fleet is overlay- or root-mode. So the
> "compositor-constant pathway" is not one pathway repeated — a Phase 2 type
> whose default is conditional on anything filesystem-shaped needs the composer
> seam, and one whose default is unconditional needs the config seam. Deciding
> which is part of each type's PR, not a detail of it.
>
> **The two seams are not equivalent modulo the condition, and the difference is
> not visibility to a reader — it is validator coverage.** A `load_fleet`
> default lands in `bot.<type>`, so `validator.py` checks that every named entry
> exists in the library, and every `FleetConfig` consumer can see it. A composer
> default never enters `BotConfig`: **`claudlobby validate` cannot see it**, so a
> registry entry naming a missing library file passes validation and fails at
> `generate`, on real fleets, only for the fleets that did not opt out.
>
> `tests/test_defaults_registry.py::test_every_registered_entry_resolves_to_a_library_file`
> covers the shared `library/` for all twelve types, which is the cheap half. It
> cannot see a **fleet overlay** shadowing `library/protocols/`, and nothing does
> today. Choose the seam knowing that, and prefer the config seam wherever the
> default is genuinely unconditional — otherwise the estate ends up split into
> validated and unvalidated halves for reasons nobody chose.

`config.py` imports the registry and consumes exactly one thing from it:

```python
DEFAULT_GUARDRAILS = _defaults.DEFAULT_GUARDRAILS      # config.py:597
```

Nothing feeds `resolve(<type>)` into the merge for the other eleven types. The
`DEFAULT_* → _merge_lists` wiring exists once, hardcoded for guardrails at
`config.py:1587-1597`.

Measured, with both halves of the control:

| what | result |
|---|---|
| `doctor` (a **real** skill) placed in `REGISTRY["skills"].entries` | **no symlink composed**, `.claude/skills/` empty |
| the same `doctor` **declared** in `fleet.yaml` | `.claude/skills/doctor -> …/library/skills/doctor` |

So the registry is the source of the **decision** but not yet of the
**behaviour**. This matters more than it looks: a Phase 2 PR that populates a
constant would pass every unit test asserting on the registry, ship, and change
no bot. The harness reports this as `UNWIRED DEFAULTS` (`baseline_inert_defaults`
in the JSON) so it cannot land silently.

This qualifies the plan's framing — *"add constants to a pathway that already
exists"*. The **fleet-declared** pathway is universal across all 12 types
(verified: `_coerce_bot` merges every type including `mcp`/`tools` via their
type-specific mergers). The **compositor-constant** pathway is not; it exists for
one type.

### 5. `permissions` has a compositor-side default too — same shape as §1, different tier

A naked bot's `settings.local.json` carries **seven** allow entries while
`permissions` is empty in both the registry and the composed section:

```
Glob, Grep, Read,
mcp__plugin_telegram_telegram__{reply, react, edit_message, download_attachment}
```

Provenance: `composer.py:2039` (`BASE_TOOLS = ["Read", "Grep", "Glob"]`) and the
telegram tool list at `composer.py:1881-1884`. Both are hardcoded composer-side
constants, outside the registry.

This is **less alarming than §1 and still in scope**. A base allow-list is
deliberate, it is RESTRICT-tier rather than INSTRUCT, and freshbox confirms every
entry traces to an equipped source. But `REGISTRY["permissions"]` currently reads
`_UNARGUED` — *"no default argued yet"* — while a default demonstrably ships.
That is precisely the ambiguity F4 exists to remove: absence in the registry must
not be able to mean both "decided" and "nobody looked", and here it means neither
— it means "decided somewhere else".

Phase 2's `permissions` PR should either move these into the registry or record
in the disposition that a compositor-side base list exists and is deliberately
not registry-managed. Either is fine; silence is not.

**Only caught because of the content probe.** `.claude/settings.local.json` is
present on a naked bot either way, so a path-only inventory reports "no change"
for anything landing inside it — the gate was blind here for two of twelve types
until the probe was added.

## What the gate does NOT cover

- **Overlay mode only — every arm passes `--fleet`.** Root mode (`generate` with
  a repo-root `fleet.yaml`, no `--fleet`) is never composed, so any default whose
  presence differs between the two modes is invisible here. That is not
  hypothetical: `shared-documentation` composes in overlay mode and **not** in
  root mode, and this gate reported it as unconditional across all 16 arms
  because all 16 are overlay. A root-mode arm would have shown it. Adding one is
  cheap — the probe writes `fleet.yaml` at the export root instead of under
  `local/`, and drops `--fleet` from both subprocess calls.
- **One bot, one expertise.** Role overlays (`Disposition.roles`, `manager`) are
  unexercised — no arm composes a manager. When Phase 2 populates a role overlay,
  this gate needs a manager arm or it will not see it.
- **The composed `CLAUDE.md` is compared by SECTION NAME, not by bytes.** A
  default that changes prose *inside* an existing section moves nothing this gate
  records. The byte-level before/after on the default path is a separate
  measurement and has to be run separately.
- **No bot is spun up.** This observes composition only. A default that changes
  runtime behaviour without changing composed output is invisible here; that is
  `validate-bot-change.sh`'s job.
- **`voices/`, `teams:`, `projects:`, and multi-bot fleets** are all out of scope
  by construction — the probe declares none of them.
- **Content-level change inside `bot.conf`** is not diffed. `mcp` and
  `permissions` get content probes because their files are always present;
  `bot.conf` does not, and a default landing there would show only if it also
  changed a section or an artifact path.

## Method

- **Exported, not composed in place.** `git archive` to a temp tree — history-free
  and attributable to a SHA. Two independent reasons force this: a checkout under
  a bot's `projects/` dir sits inside `…/runtime/bots/…`, which
  `path_audit._fleet_layout_needles` matches as fleet-owned by *shape*, so
  `CLAUDLOBBY_ROOT` reads as a cross-fleet leak and `generate` fails on a
  well-formed bot; and a gate must observe a named ref rather than whatever was
  uncommitted that afternoon. Same mechanism as `coldstart-harness.sh prepare`,
  without its host snapshot — no bot is spun up here, so there is nothing to reap.
- **The compositor under test is asserted, not assumed.** An editable install of
  the same package is normally importable; if the subprocess resolved that
  instead, every arm would compose against a compositor of unknown vintage and
  come back **green having tested nothing**. `_assert_compositor` refuses to run
  in that case. The failure mode being a PASS is why it is checked.
- **Every negative has a positive control.** `guardrails` for the opt-out, a real
  declared skill for the wiring. A probe that has not found something known is
  not yet evidence.

## Re-running it

```bash
lib/naked-bot-observe.py --baseline documentation/baselines/naked-bot-<date>.json
```

When a Phase 2 PR lands a default, the gate exits 1 and names the type. Record
the new inventory as a **new dated baseline** alongside the old one rather than
overwriting it — the point of the pre-change baseline is that it stays readable
after the change.

Bump `SCHEMA` in the harness whenever the record's shape changes. Baselines
across schema versions refuse to compare rather than half-comparing; adding a
field without bumping it let the version guard pass and the diff then raise,
which is why the field access is defensive as well.
