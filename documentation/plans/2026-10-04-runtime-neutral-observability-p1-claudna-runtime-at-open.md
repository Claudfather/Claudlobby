---
title: "P1 — `runtime` at open, `claudna.session/2`, runtime-qualified provenance (clauDNA)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: Claudfather/clauDNA documentation/specs/2026-09-28-session-store-design.md
issue: "#2145"
repos: Claudfather/clauDNA
---

# P1 — `runtime` at open, `claudna.session/2`, runtime-qualified provenance (clauDNA)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P1 (the clauDNA bullets)
> and §10.1 row 3. Code references are to clauDNA `71f983d` (v0.26.0; `main` `d1f70d4` differs only in
> `skills/session/resume.md` and `CHANGELOG.md`). Depends on: nothing in another repo (the Claudron
> boundary-spec PR, §10.1 row 1, should merge first so register rule R2 is satisfied before the export
> contract changes, but no code here waits on it). Waits on canaries: **none** — every value this PR
> records is clauDNA's own vocabulary; the Codex host that will write `runtime: "codex"` is P4.

## Summary

Record which agent runtime opened a session, so every join the epic builds on `(runtime, session_id)`
has its clauDNA half: `session.opened.data.runtime` (optional, top-level, `claude` | `codex`, absent reads as
`claude`), `SessionHandle.open_session(..., runtime=None)`, the Claude hook recording `"claude"`. Carry it
through the projection (`session.json` becomes `claudna.session/2`, with the older-tag handling the
`claudna.segment/2` bump already established) and the export item (`claudna.export/1`, additive), and give
spec §8 the item-field table it never had. Qualify harvest provenance for non-Claude sessions per F9
(`session:<runtime>/<sid>:<seg>`; Claude refs byte-identical). Amend spec §1.1 rule 1 (the plane stops
recording tool calls after P2) and rule 4 (F15: comparison waived, one summarizer, coverage kept by F6), and
the Claude-only wording in §4.1/§4.2/§6.2/§7.2/§10. Release 0.27.0: the P3 plans consume these fields.
Deliberately left to later PRs: `--include-skipped`, `entrypoint.json` and the item's `segment` object
(P3); the `host_claude`/`host_codex` split, `--host` selection and any Codex payload (P4).

## Evidence (clauDNA at `71f983d`)

- `lib/claudna/session_store/events.py:81-103` the `session.opened` `KindSpec`; `:96` `optional={"claude_pid":
  _OPT_INT, "harvest": _DICT}`; `:97` `choices={"source": (...)}` — a closed vocabulary is a tuple per field.
  `:242-247` `optional`/`choices` fire only for keys the spec names and present in `data`; `:254-256` only
  `strict=True` (writers: `check_data` `:267`, `make_event` `:349`) rejects unknown keys. So a 0.23–0.26 reader
  folds a top-level `runtime` (`:16-21`), while `actor`'s constraint is `session.schema.json:55-67`
  `additionalProperties: false` — a key nested there makes an older reader classify the event `invalid`.
- `lib/claudna/session_store/store.py:206-235` `open_session`; `:231-234` sets `claude_pid`/`harvest` only when
  not `None` — the additive pattern `runtime` follows. `:188-202` `_current_session` (every activity append,
  `:174`): `read_projection(..., SESSION_SCHEMA, bytes_before=...)` → on `None`, `rebuild`.
- `lib/claudna/session_store/boundaries.py:144-148` the one `open_session` call; `:49` `INTERACTIVE_ENTRYPOINTS`
  (where the adapter's constants live); `:111-117` `claude_pid_of`.
- `lib/claudna/session_store/project.py:32-38` the tags (`SESSION_SCHEMA = "claudna.session/1"`,
  `OLDER_PROJECTIONS = frozenset({"claudna.segment/1"})`, `_SCHEMA_FILES` keyed by the constants); `:166-175`
  `SessionFacts`; `:290-299` `session_facts` (one loop; `chain_id or ...` at `:294` is the first-wins pattern,
  `claude_pid`/`harvest` at `:295` are latest-wins); `:346-383` `project_session` (`:354` `private =
  session_facts(...).private`, `:365` `"schema": SESSION_SCHEMA`, `:372` `actor` from the **first**
  `session.opened`); `:404-413` `rebuild` writes every projection under this version's tags; `:416-426`
  `stale_projections` — segments only; `:429-446` `read_projection` — `None` on a tag mismatch (`:437-438`),
  on a schema failure (`:439-440`), on a watermark mismatch (`:441-445`); `:449-482` `refresh` — the lifecycle
  path rebuilds when `session.json` is not current (`:468-470`) and **always** rewrites `session.json`
  (`:481-482`); `:560-561` `_current_session_json`; `:584-590` `session_doc` — "Writes nothing".
- `lib/claudna/session_store/schemas/session.schema.json:3` `$id`, `:11` `additionalProperties: false`, `:13`
  `const`; `segment.schema.json:3` `claudna.segment/2`, the precedent for a projection bump.
- `lib/claudna/session_store/cli.py:56` imports `OLDER_PROJECTIONS`; `:133-137` `check_session` warns on an
  older tag and skips validation, validates anything else against the **current** schema.
- `lib/claudna/session_store/retention.py:190-193` the sweep rebuilds once on `stale_projections`; `:184-185`
  it skips fully-retired sessions before that check (a directory listing, no read).
- `lib/claudna/session_store/export.py:46-47` `SESSION_FIELDS`; `:125-128` the item; `:40-42` `CONSUMER`
  regex and `RESERVED = {harvest}`; `:135-149` `ack` (`through` ≤ highest final-or-retired index; `store.py:385-399`
  never moves back).
- `lib/claudna/session_store/filing.py:97-99` `evidence_ref` — the single constructor; `:174` `file_block`'s
  `ref`; `:190` the amend `evidence.ref`; `:139-156` `_subject_finding` (`source_url: ref`, "First filed from
  {ref}"); `lib/claudna/session_store/harvest.py:110-146` `finding_of`, `:141` `source_url=evidence_ref(sid,
  index)`; `:204` `facts = session_facts(lifecycle)`; `:268-270` and `:280-281` the two call sites.
- Claudron `6ca2b94`: `claudron/engine.py:160-161` writes `source_url` as an opaque scalar; `claudron/amend.py:84`
  accepts any evidence ref without `<!--`, `-->` or `·`; `:63` renders it as `  - evidence: <ref> · <date>…`.
  `session:codex/<sid>:<seg>` needs no engine change (F9 forge note confirmed).
- Tests: `tests/test_session_store.py:512-520` the golden fixture byte-for-byte; `:522-538` the `segment/1`
  re-fold/warn precedent; `:548-559` the sweep rewrites once; `:574-590` incremental equals rebuild; `:636-647`
  the vocabulary gate (`schema enum == registry choices + extra`); `:657-661` golden docs validate; `:307-312` a
  `{"schema": "claudna.session/1"}` doc as "tagged but malformed"; `tests/fixtures/session-store/basic/expected/session.json:31`
  the `/1` tag; `tests/test_session_store_export.py:52-60`; `tests/test_session_store_filing.py:86-110,212-241`
  (`FakeVault`, `TestHarvestGate.run`); `tests/test_session_store_harvest.py:66-80` `summarized_session`;
  `tests/test_session_store_hook.py:503-506` the `claude_pid` recorded test; `tests/test_claudron_live.py:107-116`
  `_session`, `:125-136` `_frontmatter`/`_section`, `:165-182` `TestHarvest` (asserts `source_type`, never
  `source_url`); `tests/test_runtime_layout.py:33-57,149-164` the layering gate (no new module here).
- Spec `documentation/specs/2026-09-28-session-store-design.md`: `:32` rule 1, `:33` rule 2 (`sess_<sha256(sid)>`),
  `:35` rule 4, `:92` and `:103` "Claude Code" as the session identity, `:95` "canonical ref `<sid>:<seg>`",
  `:106-108` the §4.2 table, `:217` the `session.opened` row, `:243-266` the §6.4 example (`claudna.session/1`),
  `:414` "evidence `session:<sid>:<seg>`", `:420-440` §8 (no item fields, no grammar, no ack rule), `:453` Hosts.
- Toolchain: `Makefile:23` `check`; `:52-65` `test-runtime` (the 3.9 suites); `/usr/bin/python3` is 3.9.6 on this
  machine; `.github/workflows/ci.yml:31-47` the `runtime-floor` leg; `scripts/check-changelog.sh` gates new
  `[Unreleased]` content; `scripts/release.sh` (`minor` → 0.27.0) bumps both manifests, moves the CHANGELOG, tags.

### The MUST-VERIFY question: does a 0.26 reader rewrite a `/2`-tagged `session.json` back to `/1`?

**Readers: no. Writers: yes.** `read_projection` returns `None` on any tag it does not expect (`project.py:437-438`);
what happens next is the caller's:

| 0.26 code path on a `/2` `session.json` | What it does | Where |
|---|---|---|
| `session list` / `show` / `timeline`, `export`, retention's reads | `session_doc` → `None` → folds from the log **in memory**, writes nothing | `project.py:584-590`, `readers.py:68,99,155`, `export.py:126` |
| `unclosed` sweep's status peek | plain `read_json`, reads `status`, writes nothing | `unclosed.py:96` |
| `check <sid>` | not in 0.26's `OLDER_PROJECTIONS` → validated against the 0.26 schema → **fails** (`must equal 'claudna.session/1'`, `unexpected property 'runtime'`) | `cli.py:133-139` |
| any lifecycle append (a 0.26 hook, summarizer, harvest `resummarize`, `close_abandoned`) | `refresh` → `None` → `rebuild` → **rewrites `/1`** (and every `segment.json`) | `project.py:468-470,404-413` |
| any activity append | `_current_session` → `None` → `rebuild` → **rewrites `/1`** | `store.py:197-201` |
| the retention sweep retiring a segment, or repairing | `handle.rebuild()` → **rewrites `/1`** | `retention.py:122,191` |

And symmetrically a 0.27 writer rebuilds a `/1` file to `/2`. Two plugin versions **writing** one session
flip-flop one full `rebuild` per cross-version write; that is not fixable from 0.27 (0.26's code is what it
is), it is lossless (logs are truth; `runtime` is in the log and every 0.27 read folds from the log on a tag
mismatch), and it ends when the last 0.26 process restarts (a plugin update takes effect at the next session
start; detached 0.26 workers — summarizer, harvest, sweep — are the realistic cross-version writers while bots
still share one `~/.claudna`, F14). The epic's P1 sentence "mixed 0.26/0.27 **readers** then converge instead
of rewriting each other's file" is therefore true as written for readers and must not be read as a claim
about writers; this plan's CHANGELOG entry says so. What 0.27 does so that readers converge and the file
settles on `/2`: (1) readers keep writing nothing (pinned by a test); (2) `claudna.session/1` joins
`OLDER_PROJECTIONS`, so 0.27's `check` warns rather than fails; (3) `stale_projections` covers `session.json`,
so the sweep rewrites a closed session's `/1` once; (4) 0.27 **never trusts a `/1` file's content** — a 0.26
rebuild drops `runtime` from the projection while the log still names it, so folding is the only correct
read. That is why `_SCHEMA_FILES` gains **no** `/1` entry and no `/1` schema file is kept: the only reason
to validate a `/1` document would be to serve it, and serving it could report `claude` for a Codex session.

## Implementation Plan

### Dependencies
None in code. Register order: the Claudron boundary-spec PR (§10.1 row 1) ideally merges first (R2).

### Blocks
P3 clauDNA (`2026-10-04-runtime-neutral-observability-p3-claudna-export-contract.md`: `--include-skipped`,
`entrypoint.json`, the item's `segment` object build on `claudna.session/2` and `SESSION_FIELDS` here); P3
Claudlobby (`session_summary.runtime` reads the item field); P4 clauDNA (`host.name` replaces the literal
`RUNTIME`); the companion #2149.

### Steps

### Task 0: worktree, before-leg, evidence

- [ ] Worktree `~/Projects/claudna-worktrees/p1-runtime` on branch `feat/runtime-at-open` from `origin/main`
  (`d1f70d4`). Evidence dir `~/Projects/claudna-worktrees/p1-runtime-out/`.
- [ ] Before leg, on the base: `python3 -m pytest tests/ -q 2>&1 | tail -3 > …/before-312.txt`, and the 3.9 leg
  `PATH="<venv-39>/bin:$PATH" make test-runtime 2>&1 | tail -3 > …/before-39.txt` where `venv-39` is
  `uv venv --python /usr/bin/python3` + `requirements-runtime-test.txt` (CONTRIBUTING; the sandbox note: never
  expand `$RUNTIME_TESTS` by hand). Record the known sandbox-only failures so the after-leg is compared by name.
- [ ] Use Read/Write/Edit for every file change (repo rule), never shell `cp`/`sed -i`.

### Task 1: `session.opened.data.runtime`, `open_session(runtime=)`, the Claude hook records `claude`

**Files:** `lib/claudna/session_store/events.py`, `lib/claudna/session_store/store.py`,
`lib/claudna/session_store/boundaries.py`, `lib/claudna/session_store/project.py` (`SessionFacts`,
`session_facts` only), `tests/test_session_store.py`, `tests/test_session_store_hook.py`.

- [ ] **Step 1 (tests first):** `tests/test_session_store.py::TestWritersAreStrictAboutDataKeys`:
  `test_runtime_is_an_optional_top_level_choice` — `make_event("session.opened", sid, {...base, "runtime":
  "codex"})` is accepted and `classify` says `ok`; without the key it is accepted; `"runtime": "gpt"` raises
  `EventError` matching `must be one of`; `"runtime": None` raises (the type is `str`: unknown means omit).
  `test_runtime_never_nests_in_actor` — `{**ACTOR, "runtime": "claude"}` as `actor` raises `EventError` (pins
  epic §11 "don't nest `runtime` inside clauDNA's `actor`" with the reason in the test docstring: a 0.23–0.26
  reader would classify it `invalid`). `TestStore::test_open_session_records_the_runtime_only_when_given` —
  `open_session(..., runtime="codex")` puts `"runtime": "codex"` in the first lifecycle line;
  `open_session(...)` with no kwarg writes no `runtime` key (byte-compatible with 0.26 logs); `session_facts`
  returns `runtime == "codex"` / `"claude"` respectively, and the first `session.opened` that names one wins
  across a resume. `tests/test_session_store_hook.py::TestClearLink` (beside `:503-506`):
  `test_the_runtime_is_recorded_as_claude` — after a `SessionStart startup`, the first lifecycle line has
  `data["runtime"] == "claude"`.
- [ ] **Step 2:** `events.py:93-97` — before:
  ```python
          optional={"claude_pid": _OPT_INT, "harvest": _DICT},
          choices={"source": ("startup", "clear", "resume", "fork")},
  ```
  after (the comment at `:93-95` also says "the owning agent process (Claude Code's `$CLAUDE_PID` today)" —
  F13(a): the field keeps its name until the next envelope major; only its description generalizes):
  ```python
          # runtime: the agent runtime that opened the session (Claudlobby#2145 P1): "claude" or "codex".
          # Absent on logs written before 0.27 and read as "claude". Top-level on purpose — actor is
          # additionalProperties: false, so a key nested there would make a 0.23–0.26 reader skip the event.
          optional={"claude_pid": _OPT_INT, "harvest": _DICT, "runtime": _STR},
          choices={"source": ("startup", "clear", "resume", "fork"), "runtime": ("claude", "codex")},
  ```
- [ ] **Step 3:** `store.py:206-235` — `open_session(..., harvest: dict | None = None, runtime: str | None =
  None)`; docstring: "``runtime`` names the agent runtime (``claude``, ``codex``); the hook adapter records it,
  older logs lack it"; after `:233-234` add `if runtime is not None: data["runtime"] = runtime`.
- [ ] **Step 4:** `project.py:166-175` `SessionFacts` gains `runtime: str = "claude"  # the first
  session.opened's that names one; a log written before 0.27 reads as claude`. `session_facts` (`:290-299`):
  initialise `runtime = None`, in the `session.opened` branch add `runtime = runtime or e["data"].get("runtime")`
  (the `chain_id` first-wins pattern at `:294` — a resume cannot change the runtime, because the two runtimes'
  ids never meet in one session directory), and return `runtime=runtime or "claude"`.
- [ ] **Step 5:** `boundaries.py` after `:49`:
  ```python
  #: The runtime this adapter serves, recorded at open as ``session.opened.runtime`` (Claudlobby#2145 P1).
  #: P4's host split replaces the literal with the selected host's name.
  RUNTIME = "claude"
  ```
  and at `:146` pass `runtime=RUNTIME` next to `claude_pid=claude_pid_of(env)`.
- [ ] **Step 6:** Verify: `python3 -m pytest tests/test_session_store.py tests/test_session_store_hook.py -q`.
  Commit: `feat(session-store): record the agent runtime at open (session.opened.runtime, additive)`.

### Task 2: `claudna.session/2` — `session.json` carries `runtime`; older tags read, warn, upgrade once

**Files:** `lib/claudna/session_store/schemas/session.schema.json`, `lib/claudna/session_store/project.py`,
`tests/fixtures/session-store/basic/expected/session.json`, `tests/fixtures/session-store/basic/older/session-0.26.json`
(new: today's `expected/session.json`, byte for byte), `tests/test_session_store.py`,
`documentation/specs/2026-09-28-session-store-design.md` §6.4.

- [ ] **Step 1 (tests first):** `tests/test_session_store.py::TestSchemas::test_projection_vocabularies_match_the_registry`
  gains the row `("session", "runtime", "session.opened", "runtime", [])`. `TestProjection`:
  - `test_golden_fixture_projects_byte_for_byte` (`:512`) stays as is; the expected file changes (Step 3).
  - `test_a_0_26_session_projection_is_refolded_to_the_current_schema` — mirror of `:522-538`: rebuild the
    fixture, overwrite `session.json` with `older/session-0.26.json`, `readers.show(...)["session"]["schema"]
    == "claudna.session/2"` and `["runtime"] == "claude"`; `check_session` has no problems and one "an older
    projection" warning naming `session.json`.
  - `test_the_sweep_rewrites_a_0_26_session_projection_once` — mirror of `:548-559` on `session.json`
    (`report.upgraded == [FIXTURE_SID]`, then `[]`).
  - `test_a_0_26_reader_and_a_0_27_writer_alternating_converge_on_session_2` — the convergence test the
    epic's sentence needs. The 0.26 reader is simulated at the one seam 0.26 differs on:
    `monkeypatch.setattr(project, "SESSION_SCHEMA", "claudna.session/1")` and
    `monkeypatch.setattr(project, "OLDER_PROJECTIONS", frozenset({"claudna.segment/1"}))` (what 0.26's
    `read_projection`/`session_doc` ask for; `store.py` keeps its own `/2` binding, so the writer stays 0.27).
    Loop three times: 0.27 write (`prompt(h)`, then `h.seal_segment`/`open_segment`) → the file is `/2`; 0.26 read
    (`project.session_doc(h.paths)` under the patch — `monkeypatch.setattr` then `monkeypatch.undo()` per step)
    returns the right `status`/`segments` and the file's bytes are unchanged. Assert the file was `/2` after
    every step and `project.rebuild` was never called by a read (count via a wrapper on `project.rebuild`).
  - `test_a_0_26_writers_file_is_rebuilt_to_session_2_once` — the writer side, honestly: write
    `older/session-0.26.json` over a live session's `session.json` (what a 0.26 `rebuild` leaves); the next 0.27
    activity append rebuilds (`rebuild` called once, the file is `/2`, `runtime` present); the next append takes
    the fast path (`rebuild` not called again). Wrap both bindings (`project.rebuild` and `store.rebuild`).
  - `:307-312` `test_a_tagged_but_malformed_projection_is_healed_not_trusted`: the literal becomes
    `"claudna.session/2"` so the test still means "the **current** tag, malformed" (a `/1` doc is now "older").
  - `:657-661` `test_golden_projections_validate` and `:683-687` `test_schemas_reject_malformed_projections`
    gain nothing; add `("session", lambda d: d.update(runtime="gpt"))` and `("session", lambda d: d.pop("runtime"))`
    to the reject matrix (the field is required and closed).
- [ ] **Step 2:** `schemas/session.schema.json`: `:3` `"$id": "claudna.session/2"`; `:6-10` `required` gains
  `"runtime"`; `:13` `"schema": { "const": "claudna.session/2" }`; after `:19` `"private"` add
  `"runtime": { "enum": ["claude", "codex"] },` (not nullable: the fold always names one).
- [ ] **Step 3:** `project.py:32-38` — before/after:
  ```python
  SESSION_SCHEMA = "claudna.session/1"                                  # → "claudna.session/2"
  OLDER_PROJECTIONS = frozenset({"claudna.segment/1"})                  # → frozenset({"claudna.segment/1", "claudna.session/1"})
  _SCHEMA_FILES = {SESSION_SCHEMA: "session", SEGMENT_SCHEMA: "segment"}  # unchanged: keyed by the constants; no /1 entry (see Evidence)
  ```
  `project_session` (`:346-383`): `:354` becomes `facts = session_facts(lifecycle.events)`; the dict uses
  `"private": facts.private` and gains `"runtime": facts.runtime` (one rule for the value: `session_facts`).
  `stale_projections` (`:416-426`): docstring "Does ``session.json`` or any ``segment.json`` carry a tag an
  earlier release wrote …"; body iterates `(paths.session_json, *(paths.segment(i).segment_json for i in
  paths.segment_indices()))` with the same `read_json`/tag test. The fully-retired skip at `retention.py:184-185`
  stays: a fully retired `/1` `session.json` is folded by readers (a lifecycle read, no segments) until a
  `rebuild`; not worth reordering the sweep for.
  Golden fixture `expected/session.json`: `:31` → `"schema": "claudna.session/2"` and, sorted between
  `projected_from` and `schema`, `  "runtime": "claude",` (the fixture log predates `runtime`: it exercises the
  default). Copy today's file first to `older/session-0.26.json`.
- [ ] **Step 4:** Spec §6.4 (`:243-266`): the example's `"schema"` becomes `"claudna.session/2"` and gains
  `"runtime": "claude",` after `"private": false,`; one sentence after the example: "`runtime` is the first
  `session.opened`'s (`claude` for a log written before 0.27). `claudna.session/1` is an older tag: readers fold
  it from the log, `check` warns, the sweep rewrites a closed session's once (as for `claudna.segment/1`)."
- [ ] **Step 5:** Verify: `python3 -m pytest tests/test_session_store.py tests/test_session_store_readers.py
  tests/test_session_store_export.py tests/test_session_store_unclosed.py -q` (every reader of `session.json`),
  then `grep -rn 'claudna.session/1' lib tests documentation` — only `OLDER_PROJECTIONS`, the `older/` fixture,
  the two new tests and the spec sentence may match. Commit: `feat(session-store): session.json is
  claudna.session/2 and carries runtime; /1 reads, warns and upgrades once`.

### Task 3: `claudna.export/1` items gain `runtime`; spec §8 names the item fields for the first time

**Files:** `lib/claudna/session_store/export.py`, `tests/test_session_store_export.py`, spec §8.

- [ ] **Step 1 (tests first):** `tests/test_session_store_export.py::TestExport::test_items_and_the_next_cursor`
  (`:52-60`) additionally asserts `item["session"]["runtime"] == "claude"`; new
  `test_a_codex_sessions_items_carry_its_runtime` — `session_with(...)` gains a `runtime=None` kwarg forwarded to
  `open_session`; with `runtime="codex"` the item's `session["runtime"] == "codex"`, the envelope's `schema` is
  still `"claudna.export/1"` and `set(item["session"]) == set(export.SESSION_FIELDS)`.
- [ ] **Step 2:** `export.py:46-47` — `SESSION_FIELDS = (..., "origin", "runtime")`; the docstring sentence at
  `:11-12` gains "(``runtime`` since 0.27; absent in an older envelope means ``claude``)".
- [ ] **Step 3:** Spec §8, after `:440`, a new subsection **"Item fields and rules"** (contract text; Claudron
  register rule R3 — consumers conform to this):

  | Field | Type | Source | Rule |
  |---|---|---|---|
  | `sid` | string | the id the runtime gave its hooks | with `session.runtime`, the join key (Claudlobby#2145 §2.2) |
  | `seg` | integer ≥ 1 | the segment index | one item per final, `done` segment past the consumer's cursor |
  | `session.{sid,status,opened_at,closed_at,close_reason,chain_id,parent_sid,actor,origin}` | as §6.4 | the first `session.opened` | unchanged since 0.23 |
  | `session.runtime` | `"claude"` \| `"codex"` | `session.opened.runtime`; `claude` when the log predates 0.27 | **new in 0.27**; a consumer that sees no key (an older clauDNA) treats it as `claude` |
  | `summary` | a `claudna.segment-summary/1` document | `seg-NNN/summary.json`, or the archived copy of a retired segment | always a `done` summary; a skipped segment emits no item (P3 adds `--include-skipped` status items) |

  Envelope `{schema: "claudna.export/1", consumer, items, next}`; `next[sid] = through_seg` only for a session
  whose cursor moved. **Consumer names** match `^[a-z][a-z0-9_-]{0,31}$`; `harvest` is **reserved** (the
  store's own consumer, acked only by its code). `--since-seg N`, `--limit N` (default 100). **Ack:**
  `--ack --sid <sid> --through <seg>` replies `{consumer, sid, through_seg}`; `through` may not exceed the
  session's highest final-or-retired index, and a cursor never moves back. **Additive rule:** new item keys
  may land under `claudna.export/1`; a consumer ignores unknown keys; removing or retyping a key is
  `claudna.export/2`. The second contract surface, `entrypoint.json`, is P3's.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_export.py -q`. Commit: `feat(export):
  items carry session.runtime; spec §8 names the item fields, the consumer grammar and the ack rule`.

### Task 4: F9 — runtime-qualified harvest provenance

**Files:** `lib/claudna/session_store/filing.py`, `lib/claudna/session_store/harvest.py`,
`tests/test_session_store_filing.py`, `tests/test_session_store_harvest.py`, `tests/test_claudron_live.py`.

- [ ] **Step 1 (tests first):** `tests/test_session_store_filing.py`: `test_evidence_refs_qualify_every_runtime_but_claude`
  — `filing.evidence_ref("s1", 1) == "session:s1:1" == filing.evidence_ref("s1", 1, "claude")` and
  `filing.evidence_ref("s1", 1, "codex") == "session:codex/s1:1"`. `TestFileBlock::test_a_codex_sessions_subject_and_fact_carry_the_qualified_ref`
  — the `file` helper (`:86-91`) gains `runtime="claude"` passed to both `finding_of` and `file_block`; with
  `"codex"` the captured subject's `source_url` and body ("First filed from session:codex/s1:1") and the amend's
  `evidence.ref` all equal `"session:codex/s1:1"`. `TestHarvestGate` — `run` (`:213-216`) gains `runtime=None`
  forwarded to `summarized_session`; `test_a_codex_session_files_with_qualified_provenance` asserts the per-claim
  `source_url` and the amend ref. `tests/test_session_store_harvest.py::summarized_session` (`:66-80`) gains
  `runtime=None` → `open_session(..., runtime=runtime)`; `test_blocks_become_draft_findings_scoped_to_the_repo`
  (`:95-105`) keeps its body assertion byte-for-byte (Claude prose is unchanged).
- [ ] **Step 2:** `filing.py:97-99`:
  ```python
  def evidence_ref(sid: str, index: int, runtime: str = "claude") -> str:
      """Where a harvested claim came from, as a ``source_url`` and an evidence ref.

      ``session:<sid>:<seg>`` for a Claude Code session — byte-identical to every ref written before 0.27 —
      and ``session:<runtime>/<sid>:<seg>`` for any other runtime (Claudlobby#2145 F9, mirroring F2): ids
      from two vendors must not collide in one vault. Claudron treats the string as opaque, so no engine change.
      """
      return f"session:{sid}:{index}" if runtime == "claude" else f"session:{runtime}/{sid}:{index}"
  ```
  `file_block(block, finding, *, sid, index, target, record, runtime="claude")` (`:159`) and `:174` `ref =
  evidence_ref(sid, index, runtime)`. `harvest.finding_of(..., homes=False, runtime="claude")` (`:110-111`) and
  `:141` `evidence_ref(sid, index, runtime)`; the body line at `:130` stays as it is (the ref is the contract;
  the prose names the id either way). `_harvest_session`: `:268-270` and `:280-281` pass `runtime=facts.runtime`
  (`facts` from `:204`).
- [ ] **Step 3 (live suite):** `tests/test_claudron_live.py::_session` (`:107-116`) gains `runtime: str | None =
  None` forwarded to `open_session`. In `TestHarvest` (`@current`):
  `test_a_codex_sessions_provenance_is_runtime_qualified` — `_session(store, "s1", [BLOCK], ...)` (the default) and
  `_session(store, "s2", [CODEX_BLOCK], ..., runtime="codex")` where `CODEX_BLOCK` is an `entity` block about a
  distinct subject (e.g. name `rollout store`, so it files its own note, `projects/webapp/unverified-rollout-store.md`
  by Claudron's slug rule as `SUBJECT` shows it); after `_harvest`, `_frontmatter(vault / SUBJECT)["source_url"]
  == "session:s1:1"`, `_frontmatter(vault / CODEX_SUBJECT)["source_url"] == "session:codex/s2:1"`, and the Codex
  note's text contains `"  - evidence: session:codex/s2:1"` (Claudron `amend.py:63` renders the ref first).
  Run it with `make deps-contract test-contract` (engine at `contracts/claudron.ref`); it is what the
  `contract with Claudron` CI job runs.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_filing.py tests/test_session_store_harvest.py -q`
  and the contract leg. Commit: `feat(harvest): provenance is session:<runtime>/<sid>:<seg> for non-Claude
  sessions (F9); Claude refs unchanged`.

### Task 5: spec §1.1, §4, §6.2, §7.2, §10; CHANGELOG

**Files:** `documentation/specs/2026-09-28-session-store-design.md`, `CHANGELOG.md`.

- [ ] **Step 1:** §1.1 rule 1 (`:32`) → "**A fact is recorded once, by whoever observes it first-hand.**
  Per-tool-call telemetry is the runtime's: Claude Code and Codex export it natively and Claudlobby's local
  Collector normalizes it (Claudlobby#2145 P2; until P2 lands the plane's `bot-vitals.sh` still records
  `tool_call`). The store records **no generic tool-call event**, before or after P2 — only what neither has:
  `tool.failed` with its signature, `skill.invoked`, prompt metadata, and segment boundaries. Interactive
  sessions have no plane and may have no Collector, which is why the store stands alone." Rule 2 (`:33`): "joins
  on `sess_<sha256(sid)[:32]>` for `runtime: claude` and `sess_<sha256("<runtime>:" + sid)[:32]>` otherwise
  (Claudlobby#2145 F2)". Rule 4 (`:35`) → "**One summarizer, owned here (decided 2026-10-04, Claudlobby#2145
  F15).** Claudlobby's `transcript-digest.sh` (a dormant SessionEnd `claude -p` digest) overlapped this spec's
  summarizer; the pre-registered siloed comparison this rule required is **waived** and the digest retires in
  #2145 P3. The store's summarizer is the single owner. Rule 4's coverage concern — the skipped rows the plane's
  monitor needs — is kept by F6: `session export --include-skipped` emits status-only items (P3) and
  Claudlobby turns them into its `session_summary` event. Claudlobby#1961's remaining item (the digest child's
  isolation) closes with the digest."
- [ ] **Step 2:** §4.1 `:92` Session identity → "the runtime's `session_id` (Claude Code today; the runtime is
  recorded at open as `session.opened.runtime`, `claude` when absent)"; `:103` Source → "the runtime
  (`session.opened.runtime`)"; after `:95` add: "Vault evidence is `session:<sid>:<seg>` for `claude` and
  `session:<runtime>/<sid>:<seg>` otherwise (F9), so two runtimes' ids never collide in one vault." §4.2 `:106-108`:
  one sentence above the table — "The table is Claude Code's hook vocabulary; another runtime's adapter maps its
  events onto the same store actions (#2145 P4)." §6.2 `:217`: add `runtime?: "claude"|"codex"` to the row and
  "`runtime` is the agent runtime that opened the session (absent before 0.27: `claude`); `claude_pid` is the
  owning agent process (Claude Code's `$CLAUDE_PID`; F13 keeps the name until the next envelope major)". §7.2
  `:414`: "evidence `session:<sid>:<seg>` (`session:<runtime>/<sid>:<seg>` for a non-Claude session, F9)". §10
  `:453` Hosts: append "The adapter records its runtime at open (`runtime: "claude"`); a second host's adapter
  (#2145 P4) records its own, and everything past the adapter reads `SessionFacts.runtime`."
- [ ] **Step 3:** `CHANGELOG.md` `## [Unreleased]` (house format: bold headline, issue in parentheses, prose
  naming files and fields; `### Added` before `### Changed`, above the existing `### Fixed`):
  - Added — **Sessions record which agent runtime opened them** (Claudlobby#2145 P1): `session.opened.runtime`
    (`claude` | `codex`, optional, top-level; older logs read as `claude`), `SessionHandle.open_session(...,
    runtime=)`, the Claude Code hook records `claude`; `session.json` and the export item's `session` subset
    carry it (`claudna.export/1` stays; additive). Spec §8 names the item fields, the consumer grammar and the
    ack rule for the first time.
  - Added — **Harvest provenance names the runtime for non-Claude sessions** (F9): `session:<runtime>/<sid>:<seg>`;
    Claude refs are byte-identical to before.
  - Changed — **Heads-up: `session.json` is `claudna.session/2`.** 0.27 reads a `/1` file by folding its log,
    `check` warns on it, the sweep rewrites a closed session's once. A **0.26 writer** sharing a store (a bot not
    yet restarted onto 0.27, its detached summarizer/harvest/sweep) rebuilds a `/2` file back to `/1` on its own
    writes and 0.27 rebuilds it forward — one full rebuild each, nothing lost (the log holds `runtime`); it ends
    when the last 0.26 process restarts. `check` run from 0.26 fails on a `/2` file: run it from 0.27.
  - Changed — **Spec §1.1 rules 1 and 4 amended** (tool calls are the runtime's telemetry after #2145 P2; the
    summarizer comparison is waived, F15) and §4/§6.2/§7.2/§10 say "runtime" where they said Claude Code.
- [ ] **Step 4:** Commit: `docs(spec): runtime at open, qualified provenance, rules 1 and 4 amended; changelog`.

### Task 6: the gate — `make check`, the 3.9 runtime leg, the contract leg

- [ ] `make check` (unsandboxed, or `make lint && python3 -m pytest tests/` plus `bash scripts/check-changelog.sh`
  per the sandbox note); compare failing names with the before leg — only the known sandbox-only four may fail.
- [ ] `PATH="<venv-39>/bin:$PATH" make test-runtime` on `/usr/bin/python3` (3.9.6): the new code uses nothing past
  3.9 (`from __future__ import annotations` already covers the `str | None` hints).
- [ ] `tests/test_runtime_layout.py` passes untouched: no new module, no new import edge (`filing` ← nothing new,
  `harvest` 6 still imports `filing` 5 and `project` 3).
- [ ] `make deps-contract test-contract` for Task 4's live case; `make test-contract-floor` (floor engines must
  still take their paths; the new test is `@current`).
- [ ] Not a runtime-behavior change in Claudlobby's sense (no bot composition changes); the clauDNA-side live
  check is the contract leg above plus one local session: `claude --plugin-dir <worktree>`, start and exit a
  session, then `python3 lib/claudna/session_store show <sid> --json | jq .session.runtime` prints `"claude"`
  and `… export --consumer canary --json | jq '.items[0].session.runtime'` prints `"claude"` once a segment is
  summarized (or `"next": {}` with no error when none is). Record both in the evidence dir.

### Task 7: release 0.27.0

- [ ] After merge, on a release branch: `./scripts/release.sh minor` (0.26.0 → 0.27.0: a new projection tag and
  a new contract field — minor, not patch; both manifests move together, `make check-manifest` pins it), PR
  titled `release: v0.27.0` (the convention of #401), merge, push the tag. The marketplace tracks the default
  branch (CONTRIBUTING "What a marketplace user receives"), so the merge is what users receive. P3's plans
  name 0.27.0 as their floor.

## Test Plan

Unit (all in `make test`, the session-store ones also in `make test-runtime` on 3.9): `test_session_store.py`
(registry choices, top-level-not-nested, `open_session` additive, `session_facts`, vocabulary gate, golden
fixture, `/1` re-fold + warn, sweep upgrade once, the reader/writer convergence pair), `test_session_store_hook.py`
(the hook records `claude`), `test_session_store_export.py` (`runtime` in items, envelope tag unchanged),
`test_session_store_filing.py` and `test_session_store_harvest.py` (`evidence_ref`, `finding_of`, `file_block`,
`_harvest_session` threading; Claude prose byte-identical). Contract: `test_claudron_live.py` current-engine
case on `source_url` and the rendered evidence line (`make test-contract`), floor leg unchanged. Gates: `make
check` (skills, manifests, changelog, lint, tests), the layering gate, CI's `runtime-floor` and `contract with
Claudron` jobs.

## Verification Checklist

- [ ] `python3 -m pytest tests/ -q` and `make test-runtime` under 3.9: same failing names as the before leg (none new).
- [ ] `grep -rn 'claudna.session/1' lib tests documentation` → `project.py` (`OLDER_PROJECTIONS`), `older/session-0.26.json`,
  the two convergence tests, the §6.4 sentence; nothing else.
- [ ] `grep -n '"runtime"' tests/fixtures/session-store/basic/expected/session.json` → one line, `"claude"`, between
  `projected_from` and `schema`; the golden byte-for-byte test passes.
- [ ] In `test_a_0_26_reader_and_a_0_27_writer_alternating_converge_on_session_2`, temporarily make a read call
  `rebuild` (shown, restored) — the test fails; as committed it passes.
- [ ] `python3 -m pytest tests/test_session_store.py -k 'runtime' -v` shows the "nested in actor" case raising.
- [ ] A local session (Task 6) shows `.session.runtime == "claude"` in `show --json` and in an export item.
- [ ] `make test-contract`: the Codex note's frontmatter has `source_url: session:codex/s2:1` and its fact's
  evidence line starts with that ref; the Claude note keeps `session:s1:1`.
- [ ] `./scripts/release.sh --dry-run minor` prints 0.27.0 and the four CHANGELOG bullets it would move.

## What NOT To Do

- Do not nest `runtime` inside `actor` (epic §11): a 0.23–0.26 reader would skip the whole `session.opened`.
- Do not make `read_projection` accept a `/1` `session.json` or add a `/1` entry to `_SCHEMA_FILES`: a 0.26
  rebuild writes `/1` without `runtime` over a log that has it; folding is the only correct read.
- Do not keep a `session-1.schema.json`; the `segment/1` precedent keeps none either.
- Do not qualify Claude refs (F9: `session:<sid>:<seg>` stays byte-identical) or change the Claude harvest prose.
- Do not rename `claude_pid` (F13: the name holds until the next envelope major; only its description changes).
- Do not add `--include-skipped`, `entrypoint.json` or the item's `segment` object (P3), nor `host_*.py`, `--host`
  or any Codex payload handling (P4); do not add a module (the layering gate would need a rank).
- Do not reorder the sweep's fully-retired skip (`retention.py:184-185`) to upgrade a fully retired `/1` file; readers fold it.
- Do not touch `~/.claude/settings.json`, the plugin cache, or any Claudlobby file (its `derive_session_uid` and
  `CLAUDLOBBY_RUNTIME` are the P1 Claudlobby plan's).
- Do not bump `claudna.export/1`: both item changes are additive, and P3 relies on the tag staying.

## Context

area: session store (events, projection, export, harvest provenance) · effort: M · risk: Low–Med (a projection
tag bump touches every reader; mixed-version writers rebuild — lossless, bounded, documented) · priority: P1
(§10.1 order 3; releases before P3) · related: Claudlobby#2145 (epic; F2, F6, F9, F13, F15), Claudlobby#1961
(spec rule 4), clauDNA#300/#306 (Codex adapter prior art). Epic text to touch when this merges: §6 P1 clauDNA
bullet 2 — "readers converge" is right; add "a 0.26 writer still rebuilds to `/1`; bounded and lossless".
