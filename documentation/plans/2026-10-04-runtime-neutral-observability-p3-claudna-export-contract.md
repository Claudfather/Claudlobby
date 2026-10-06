---
title: "P3 — the export contract Claudlobby consumes: skipped items, the segment object, `entrypoint.json`, the frozen activity layer (clauDNA)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
updated: 2026-10-05
epic: documentation/plans/2026-10-04-runtime-neutral-observability-plan.md
spec: Claudfather/clauDNA documentation/specs/2026-09-28-session-store-design.md
issue: "#2145"
repos: Claudfather/clauDNA
---

# P3 — the export contract Claudlobby consumes: skipped items, the segment object, `entrypoint.json`, the frozen activity layer (clauDNA)

> **Status:** draft, authored by `/claudna:forge` on 2026-10-04 from epic plan §6 P3 (clauDNA bullets) and its
> `#### Spec: the clauDNA export contract additions` subsection, plus the `segment` object the
> `#### Spec: the session_summary plane event` subsection asks of this PR. Code references are to clauDNA
> `71f983d` (v0.26.0; `main` `d1f70d4` differs only in `skills/session/resume.md` and `CHANGELOG.md`).
> Depends on: the P1 clauDNA release (`session.opened.data.agent_cli`, `claudna.session/2`, `agent_cli` in
> `SESSION_FIELDS` — the export item's `session.agent_cli`). Waits on canaries: none any more — *(P0 fold, 2026-10-05)* C10 and C11
> answered (Claudlobby run log): Claude Code sets `CLAUDE_CODE_CHILD_SESSION=1` on every hook and tool process, a
> top-level session's included, so Task 5 is re-written as the opposite pin (the guard must *not* read the marker)
> and holds nothing. Everything ships together. Mission: this PR adds no
> runtime vocabulary; it rests on the clauDNA mission amendment the P1 clauDNA release (0.27) carries — D2, ratified
> 2026-10-05 ([lock comment](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137)) — a local
> memory with an export door and no phone-home.

## Summary

This PR makes clauDNA's export door carry what Claudlobby's P3 `session-export` job needs and nothing it has to guess: an opt-in `--include-skipped` that turns the three segment kinds the cursor silently passes into *status items* (F6), an additive `segment: {sealed_at, sealed_by, counts}` object on every item (the `session_summary` event's volume fields), and `<CLAUDNA_STATE_DIR>/entrypoint.json`, written by the store's own opening SessionStart so the job finds the store by contract instead of parsing Claude Code's plugin registry (F16). It freezes the activity layer with a test and a spec note (F8), reads Claude Code's own child marker first in the nested-child guard (conditional on C10), fixes the two SETUP_GUIDE sentences F13 changes, writes the new surfaces into spec §8 as contract text (Claudron register rule R3: Claudlobby conforms to it), and releases. It deliberately leaves the host split (`host_claude.py`/`host_codex.py`, `--host`), every Codex field, and the per-bot state-dir cutover (F14, a Claudlobby composition change) to P4 and the P3 Claudlobby plan.

## Evidence (clauDNA at `71f983d`)

- `lib/claudna/session_store/export.py:10-13` the item shape: `{sid, seg, session: <a session.json subset>, summary: <the segment summary>}`; `:46-47` `SESSION_FIELDS` (P1 appends `"agent_cli"`); `:81-82` `export(store, consumer, *, since_seg=None, limit=100, now=None)`; `:92` `start = max(handle.cursor(consumer), since_seg or 0)`; `:98-99` private sessions `continue`; `:106-116` `take()` — a retired segment with no archive passes (`:108-109`), `state.summary in ("done", "skipped")` returns `None` for skipped (`:113-114`), a settled give-up passes (`:115-116`); `:124-128` the item is built only `if doc is not None`; `:129` `through = index` advances regardless; `:135-149` `ack` (never back, never past the last final segment).
- `lib/claudna/session_store/cli.py:260-278` `_export`: `export.export(store, args.consumer, since_seg=args.since_seg, limit=args.limit)` at `:274`; `:364-372` the `export` subparser; `:472-473` dispatch; `:314-318` the `hook` hot path (untouched here).
- `lib/claudna/session_store/project.py:122-152` `Boundary`/`fold_boundary` — "the single interpretation of boundary events"; `project_segment` writes `"sealed_at": b.last_seal["ts"]` (`:334`), `"sealed_by": b.last_seal["data"]["sealed_by"]` (`:335`), `"counts": counts` (`:340`) tallied over `_COUNTED` (`:46-51`: `prompt.submitted→prompts`, `skill.invoked→skills`, `tool.failed→failures`, `tool.interrupted→interrupts`); `:564-581` `segment_docs(paths, lifecycle: Log, *, trusted=None)` returns every live segment's `segment.json` or its in-memory fold; `:505-518` `SegmentState(index, final, summary, doc, sealed_at)`. `schemas/segment.schema.json:49-61,102-129` pin `sealed_at`, the `sealed_by` enum and the four-key `counts` object.
- `lib/claudna/session_store/boundaries.py:125-162` `_session_start`: `compact` returns early (`:130-135`), `source not in _SOURCES` is ignored (`:136-137`), the opening path ends `return f"session opened ({source})"` (`:162`); `:165-180` `spawn_worker` computes `package = Path(__file__).resolve().parent` (`:174`) and execs `[sys.executable, "-S", str(package), *args, "--root", str(root)]` (`:177`) — that `str(package)` is the entrypoint Claudlobby will run; `:287-314` `inherited` (preamble `:307-308`, pid `:309-311`, fresh start `:312-313`, entrypoint `:314`); `:325-326` the `CLAUDNA_SESSION_CHILD` fast exit; `:336-337` the guard's message; `:343` `find_pid` default.
- `lib/claudna/session_store/__main__.py:15-16` inserts `Path(__file__).resolve().parents[2]` — from `__main__.py` that is `lib/` (the import root); from the package directory, `parents[2]` is the **plugin root** (`session_store → claudna → lib → <root>`).
- `lib/claudna/session_store/fsio.py:84-110` `atomic_write_text` (mkstemp, `chmod 0600` `:99`, `os.replace` `:100`; `path.parent` must exist); `:113-115` `atomic_write_json`; `:118-123` `read_json` (`None` when missing or unparseable); `:62-81` `ensure_dir`. `paths.py:51-59` `state_root`; `:24-27` `STATE_DIR_ENV`, `CHILD_ENV`.
- `lib/claudna/session_store/events.py:34-35` `LIFECYCLE`/`ACTIVITY`; `:169-175` `summary.skipped`, reasons `("private", "disabled", "trivial", "headless", "no_transcript")` (`:174`); `:177-213` the four `ACTIVITY` kinds; `:218` `now_ts()` (`YYYY-MM-DDTHH:MM:SS.mmmZ`). `activity.py:138-143` `_HANDLERS` → `EVENTS == ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure")`.
- `lib/claudna/session_store/schema.py:25-29` `_SUPPORTED` keywords; `tests/test_session_store.py:648-655,716` walk **every** `schemas/*.schema.json` (keywords ⊆ `_SUPPORTED`; every `\d{4}-` pattern equals the envelope's `ts` pattern) — a new schema file is gated automatically.
- `plugin-hooks/hooks.json:79-96` two SessionStart entries: the briefing (`startup|clear`) and the store (`session-store.sh SessionStart`, no matcher); `plugin-hooks/session-store.sh:22-23` gates (`CLAUDNA_SESSION_CHILD`, `CLAUDNA_SESSION_STORE`), `:41-44` `python3 -S "$PKG" hook "${1:-}"`.
- Tests: `tests/test_session_store_export.py:31-48` `session_with(store, sid, statuses)`, `:88-96` the CLI test, `:205-216` `TestExportReviewFixes.stranded`, `:323-329` the retired-no-archive case; `tests/test_session_store_hook.py:36` `CLI_ENV`, `:59-62` `fire`, `:172-177` the inherited-id test, `:248-256` a store failure through `run_hook`, `:304-309` the wrapper test, `:377-411` `TestWiring` (pins the six wired events), `:436-507` `TestNestedChildren` (`env(entrypoint, pid)` `:437-438`); `tests/test_session_store_activity.py:1-50`; `tests/test_runtime_layout.py:33-58,150-164` (no new module here, so the ranks are untouched); `tests/conftest.py:63-88` `segment_summary`/`complete_segment`.
- Spec `documentation/specs/2026-09-28-session-store-design.md:106-122` §4.2 (activity rows `:113-115`); `:145` "A real child marker would still be the stronger signal where it exists"; `:147-173` §5 layout; `:420-440` §8 (`:433` the contract line, `:440` the envelope sentence — §8 names no item fields today); `:453` Hosts; `:464` §11.5.
- `SETUP_GUIDE.md:309-317` the env table; `:316` and `:320` both say "whose `claude` process is gone". `CHANGELOG.md:8-10` the `[Unreleased]` format. `Makefile:22` `check`, `:54-61` `RUNTIME_TESTS` (export, hook and activity suites are in the 3.9 leg); `.github/workflows/ci.yml:31-46` `runtime-floor`; `CONTRIBUTING.md:128-138` the release procedure.
- Zero hits for `include-skipped`, `include_skipped`, `entrypoint.json` and `CLAUDE_CODE_CHILD_SESSION` across `lib/`, `plugin-hooks/`, `scripts/`, `tests/` and the docs (`grep -rn`, 2026-10-04). `CLAUDE_CODE_SESSION_ID` appears only in the guard's docstring (`boundaries.py:290`) and the summarizer strip (`summarize.py:84`).
- Claudlobby (`cd292cb`; checkout `2dd0aad5`, identical here): `_runtime_scripts/start-bot.sh:223-229` sources `bot.conf` under `set -a`, `:268` `exec $CLAUDE …`; no `unset CLAUDE_CODE_*` anywhere in the script — the C10 leak path is open today.

## Implementation Plan

### Dependencies
The **P1 clauDNA release** (0.27) on `main`, carrying clauDNA's mission amendment (D2, ratified 2026-10-05 — [lock comment](https://github.com/Claudfather/Claudlobby/pull/2144#issuecomment-6000052137)): `SESSION_FIELDS` carries `agent_cli` (the item's `session.agent_cli`), `session.json` is `claudna.session/2`, and `boundaries.py` has the constant P1 passes as `agent_cli=` to `open_session` (called `AGENT_CLI` below; if P1 named it otherwise, use P1's name — never a second literal). **Task 5 only:** if C10 shows a leak, P1 Claudlobby Task 7b (Half A; `unset CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE`, tested in `tests/test_boot_policy_conformance.py`) has landed on every bot. Nothing else here needs Claudlobby or Claudron to have moved.

### Blocks
The P3 Claudlobby plan (`2026-10-04-runtime-neutral-observability-p3-claudlobby-summaries.md`): its `session-export` job runs `python3 -S <entrypoint> export --consumer claudlobby --include-skipped --json` and fills `session_summary` from the `segment` object. It cannot open until this PR is **released** (plan index §10.1: plan 5 releases before plan 6 consumes it).

### Steps
Tasks follow as H3 siblings. Line numbers are pre-dependency: the P1 clauDNA release (plan 3) edits `boundaries.py`, `events.py`, `project.py`, `export.py` and the spec before this PR opens — edit bottom-up or re-grep each anchor at PR-open. Tasks 1–2 (`export.py`, `cli.py`, `tests/test_session_store_export.py`) are disjoint from Task 3 (`boundaries.py`, a new schema, `tests/test_session_store_hook.py`) and Task 4 (`tests/test_session_store_activity.py`), so they can be developed in parallel and land in one PR. Task 5 edits the same two files as Task 3 and follows it. Tasks 6–8 are docs; Task 9 gates and releases.

### Task 0: worktree, before-leg, evidence

- [ ] `git -C ~/Projects/claudna fetch origin && git worktree add ../claudna-p3-export -b p3/export-contract origin/main` **after** the P1 release commit is on `main` (`git log --oneline -1 -- lib/claudna/session_store/schemas/session.schema.json` must show P1's `claudna.session/2` change). Evidence dir: `~/Projects/claudna-p3-export-out/`.
- [ ] Before leg, in the worktree, both interpreters (`lib/CLAUDE.md`: the floor is 3.9): `python3 -m pytest tests/ -q 2>&1 | tail -3 > ~/Projects/claudna-p3-export-out/before-dev.txt` and, with a venv built from `/usr/bin/python3` and `requirements-runtime-test.txt`, `make test-runtime 2>&1 | tail -3 > …/before-39.txt`. Record failing names, not just counts (the sandbox-only failures listed in the maintainer's memory note are not this PR's).

### Task 1: every item carries `segment: {sealed_at, sealed_by, counts}`

**Files:** `lib/claudna/session_store/export.py` (modified), `tests/test_session_store_export.py` (modified).

- [ ] **Step 1 (tests first):** in `TestExport`: `test_every_item_carries_the_segment_object` — build a session by hand (not `session_with`, which seals at once): `open_session`, `open_segment("session_open", 0)`, `append("prompt.submitted", {"prompt_id": None, "chars": 3})` twice and `append("tool.failed", {"tool": "Bash", "signature": "Bash: x", "exit_code": 1})` once, `seal_segment(100, "precompact")`, `complete_segment(...)`, `close_session("other")`; assert `item["segment"] == {"sealed_at": seg["sealed_at"], "sealed_by": "precompact", "counts": {"prompts": 2, "skills": 0, "failures": 1, "interrupts": 0}}` where `seg = json.loads(h.paths.segment(1).segment_json.read_text())`, and `set(item) == {"sid", "seg", "session", "summary", "segment"}` (the default item key set, pinned). In `TestReviewRound387.test_a_consumer_behind_retention_still_gets_the_archived_summaries` add: each archived item's `segment["counts"] is None`, `segment["sealed_by"] == "precompact"`, and `segment["sealed_at"]` equals the `segment.sealed` event's `ts` in `h.paths.lifecycle` (the directory is gone; the log still says when it was sealed).
- [ ] **Step 2:** `export.py`. Extend the `.project` import (`:34-35`) with `fold_boundary, segment_docs`. Add, after `_settled` (`:78`):

```python
def segment_record(index: int, bucket: list[dict], live: dict[int, dict]) -> dict:
    """The item's ``segment`` object: the seal from the lifecycle log, the counts from the live projection.

    ``sealed_at``/``sealed_by`` come from the fold ``project_segment`` itself uses (``fold_boundary``), so a
    retired segment — its directory and ``segment.json`` gone — still carries them; its ``counts`` are
    ``None``, because the activity log went with the directory. Claudlobby's ``session_summary`` reads all three.
    """
    seal = fold_boundary(bucket).last_seal
    doc = live.get(index)
    return {"sealed_at": seal["ts"] if seal else None,
            "sealed_by": seal["data"]["sealed_by"] if seal else None,
            "counts": dict(doc["counts"]) if doc else None}
```

In `export()`, the item block (`:124-128`) becomes — `live` is folded once per session, beside `subset`, so a session with no items reads nothing extra:

```python
            if doc is not None:
                if subset is None:
                    session = session_doc(handle.paths, lifecycle)
                    subset = {k: session.get(k) for k in SESSION_FIELDS}
                    live = {d["index"]: d for d in segment_docs(handle.paths, lifecycle)}
                items.append({"sid": sid, "seg": index, "session": subset, "summary": doc,
                              "segment": segment_record(index, buckets.get(index, []), live)})
```

(`through, subset = start, None` at `:100` gains `live: dict[int, dict] = {}`.) Update the docstring item line (`:11-12`) to name `segment`. Why `segment_docs` and not `states[index]`: `SegmentState` has no `sealed_by` or `counts`, and retired indices are not in `states`; the fold is the one source both paths share.
- [ ] **Step 3:** Verify: `python3 -m pytest tests/test_session_store_export.py -q`. Commit: `feat(export): every item carries segment {sealed_at, sealed_by, counts}`.

### Task 2: `--include-skipped` — status items for the segments the cursor passes (F6)

**Files:** `lib/claudna/session_store/export.py`, `lib/claudna/session_store/cli.py`, `tests/test_session_store_export.py` (all modified), `lib/claudna/session_store/schemas/export.schema.json` (new).

- [ ] **Step 1 (tests first):** a new class `TestIncludeSkipped` in `tests/test_session_store_export.py`:
  - `test_a_skipped_summary_is_a_status_item_with_the_flag`: `session_with(store, "s1", ["done", "skipped", "done"])` → with `include_skipped=True` items are `[(1, "done"), (2, None), (3, "done")]` by `(seg, summary is not None)`; item 2 is `{"summary": None, "skipped": {"reason": "trivial"}, "segment": {... "sealed_by": "precompact" ...}}` and carries the same `session` subset; `next == {"s1": 3}` with and without the flag; without the flag the items are `[1, 3]` as today.
  - `test_a_retired_segment_with_no_archive_is_retired`: the `:323-329` recipe (`["skipped", "done"]` then `retention.retire(h, [(1, "age")])`) → with the flag, seg 1 is a status item with `reason == "retired"` and `segment["counts"] is None`.
  - `test_a_settled_give_up_is_gave_up`: `TestExportReviewFixes.stranded(store, "s1")` (a `summary.failed` for good) with `now=LATER` → seg 1 `reason == "gave_up"`; the same with `retryable=True, harvest_on=False` (a retry nothing will run).
  - `test_a_private_skip_never_exports_even_with_the_flag`: a non-private session with `h.append("summary.skipped", {"reason": "private"}, seg=1)` → no item for seg 1 under the flag, `next` still passes it.
  - `test_without_the_flag_the_envelope_is_unchanged`: `export(store, "c") == export(store, "c", include_skipped=False)`; no item has a `"skipped"` key; the flagged envelope with its status items removed `==` the unflagged one (byte-identical minus status items).
  - `test_status_items_count_against_the_limit`: `["skipped", "done"]`, `limit=1, include_skipped=True` → one item (the status item), `next == {"s1": 1}`.
  - In `test_the_cli` (`:88-96`): `main(["export", "--consumer", "claudron", "--include-skipped", "--json", *root])` on `["skipped", "done"]` prints two items, the first with `skipped`; without the flag, one item and no `skipped` key.
- [ ] **Step 2:** `export.py`. Signature (`:81-82`) gains `include_skipped: bool = False`. Add after `_settled`:

```python
def skip_reason(bucket: list[dict]) -> str:
    """The reason the segment's last ``summary.skipped`` gives (the closed set in ``events.py``)."""
    return next(e["data"]["reason"] for e in reversed(bucket) if e["kind"] == "summary.skipped")
```

`take()` (`:106-116`) returns a third value, the pass-over reason — the control flow is unchanged:

```python
        def take(index: int) -> tuple[bool, dict | None, str | None]:
            """``(go on, item summary or None, why it passes with no summary or None)`` for one segment past the cursor."""
            if index in retired and index not in states:  # its archived summary, final and done (owner, #387 S4)
                doc = read_archived(handle.paths, index)
                return True, doc, None if doc is not None else "retired"  # none archived (it had no summary)
            state = states[index]
            if not state.final:
                return False, None, None
            if state.summary == "done":
                return True, state.doc, None
            if state.summary == "skipped":
                return True, None, skip_reason(buckets.get(index, []))
            settled = _settled(state.summary, buckets.get(index, []), retried=retried, abandoned=abandoned, now=now)
            return settled, None, "gave_up" if settled else None  # not settled: in flight, stale or unreadable — hold
```

The loop (`:118-131`): `go_on, doc, reason = take(index)`; the item condition becomes `if doc is not None or (include_skipped and reason not in (None, "private")):`; the item dict is built as in Task 1 and, `if doc is None`, gains `item["skipped"] = {"reason": reason}` — regular items never carry a `skipped` key. `private` passes with no item because a summary withheld by the person's choice is not a coverage fact for a fleet monitor (the spec's reason set excludes it). The no-item default is #387's (`documentation/plans/2026-09-30-session-store-phases-5-7.md:25,66`; `export.py:19-20`: "a skipped segment passes") and stays — the flag adds items, never changes what passes. The reason set is `events.py:174`'s today; a consumer treats an unknown reason as a skip (spec §8, Task 7). Docstring (`:19-20`) gains one sentence naming the flag and the three reasons.
- [ ] **Step 3:** `cli.py`: after `:368` add `exp.add_argument("--include-skipped", action="store_true", help="also emit a status item {summary: null, skipped: {reason}} for each segment the cursor passes unsummarized")`; `:274` passes `include_skipped=args.include_skipped`. The `hook` hot path (`:316`) is untouched.
- [ ] **Step 4 (the machine-readable half of §8 — X17):** `lib/claudna/session_store/schemas/export.schema.json` (new; `$id` `claudna.export/1`; only `_SUPPORTED` keywords, `schema.py:25-29` — `items` and `description` are among them): the envelope `{schema, consumer, items, next}`; each item requires `sid, seg, session, summary, segment`, `skipped` optional; `session` requires every `SESSION_FIELDS` key (`session.agent_cli` included — P1 shipped it); `summary` is `["object", "null"]`; `segment` requires Task 1's `segment_record` keys (`sealed_at`, `sealed_by`, `counts`, each nullable); `skipped` requires `reason`, a non-empty string whose known values (Step 2's `summary.skipped` reasons, `gave_up`, `retired`) are listed in its `description`, not an `enum`. **The schema is open:** no `additionalProperties: false` on the item, `session` or `segment` — clauDNA's validator accepts extra keys unless a schema forbids them (`schema.py:129`) — so the machine half says what P1's additive rule says (new keys land under `claudna.export/1`; a consumer ignores what it doesn't know). Test, in `TestIncludeSkipped`: `test_the_envelope_validates_against_the_published_schema` — `schema.validate(export(store, "c", include_skipped=True), schema.load("export")) == []` on `["done", "skipped", "done"]`; the same envelope with `"skipped": null` forced onto a regular item fails (`skipped` is an object when present); one with an extra key on an item, on its `session` and on its `segment` still validates (the open rule, pinned). `tests/test_session_store.py:648-655` gates the new file's keywords and timestamp patterns automatically. Claudlobby vendors this file the way clauDNA vendors `contracts/claudron.json`, and validates its checked-in canned envelopes against the vendored copy (P3 Claudlobby Task 8's drift gate); until that leg runs, this schema is the contract's only machine gate.
- [ ] **Step 5:** Verify: `python3 -m pytest tests/test_session_store_export.py -q`. Commit: `feat(export): --include-skipped emits status items for the segments the cursor passes; schemas/export.schema.json pins the envelope (F6)`.

### Task 3: the opening SessionStart writes `<CLAUDNA_STATE_DIR>/entrypoint.json` (F16)

**Files:** `lib/claudna/session_store/boundaries.py` (modified), `lib/claudna/session_store/schemas/entrypoint.schema.json` (new), `tests/test_session_store_hook.py` (modified).

- [ ] **Step 1 (tests first):** a new class `TestEntrypoint` in `tests/test_session_store_hook.py` (imports: `schema` from `claudna.session_store`, `subprocess`, `sys` already there):
  - `test_an_opening_session_start_writes_the_record`: `fire(store, "SessionStart", transcript, source="startup")`; `doc = json.loads((store.root / "entrypoint.json").read_text())`; `schema.validate(doc, schema.load("entrypoint")) == []`; `doc["entrypoint"] == str(Path(boundaries.__file__).resolve().parent)`; `doc["plugin_root"] == str(REPO_ROOT)`; `doc["plugin_version"] == json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]`; `doc["agent_cli"] == "claude"`; `doc["python"] == sys.executable`; `(store.root / "entrypoint.json").stat().st_mode & 0o077 == 0`.
  - `test_the_record_is_rewritten_on_every_opening_start_and_never_on_compact`: after the startup, overwrite the file with `{"stale": true}`; `PreCompact` then `SessionStart(compact)` leave it stale; `SessionEnd` then `SessionStart(resume)` rewrite a valid record.
  - `test_the_recorded_entrypoint_runs_the_export_door`: `subprocess.run([sys.executable, "-S", doc["entrypoint"], "export", "--consumer", "test", "--json", "--root", str(store.root)], capture_output=True, text=True)` → `returncode == 0` and `json.loads(stdout)["schema"] == "claudna.export/1"` — the contract in one assertion: the recorded path is the directory form the hooks use.
  - `test_a_missing_or_malformed_manifest_leaves_the_version_null`: `monkeypatch.setattr(boundaries, "ENTRYPOINT", tmp_path / "lib" / "claudna" / "session_store")` (mkdir parents); `boundaries.write_entrypoint(root, agent_cli="claude")` → `plugin_version is None`, `plugin_root == str(tmp_path)`; write `{"version": 3}` to `tmp_path/.claude-plugin/plugin.json` → still `None`.
  - `test_a_failed_write_is_logged_and_the_session_is_still_open`: `monkeypatch.setattr(boundaries, "atomic_write_json", raising OSError)`; through `run_hook(...)` (the `:248-256` recipe) the result starts with `"error:"`, `hooks/errors.log` has the line, and `sessions/<SID>/seg-001` exists.
  - In `TestWrapper.test_it_records_prints_nothing_and_exits_0` (`:304-309`): assert `(state / "entrypoint.json").is_file()` and its `entrypoint == str(REPO_ROOT / "lib" / "claudna" / "session_store")` — the real wrapper, the real package.
- [ ] **Step 2:** `schemas/entrypoint.schema.json` (new; only `_SUPPORTED` keywords, and the envelope's timestamp pattern, or `tests/test_session_store.py:648-655` fail):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "claudna.entrypoint/1",
  "title": "<state>/entrypoint.json — where this plugin's store entrypoint is (spec §8, F16)",
  "type": "object",
  "required": ["schema", "entrypoint", "plugin_root", "plugin_version", "python", "agent_cli", "written_at"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "claudna.entrypoint/1"},
    "entrypoint": {"type": "string", "minLength": 1},
    "plugin_root": {"type": "string", "minLength": 1},
    "plugin_version": {"type": ["string", "null"], "minLength": 1},
    "python": {"type": "string", "minLength": 1},
    "agent_cli": {"enum": ["claude", "codex"]},
    "written_at": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$"}
  }
}
```

- [ ] **Step 3:** `boundaries.py`. Imports (`:41`) gain `atomic_write_json, read_json` from `.fsio`. Module constants, after `INTERACTIVE_ENTRYPOINTS` (`:49`):

```python
#: The store's entrypoint: this package directory, which ``python3 -S <entrypoint> <verb>`` runs (``__main__.py``).
#: Workers exec it (:func:`spawn_worker`); ``entrypoint.json`` publishes it (spec §8, F16). One value, two readers.
ENTRYPOINT = Path(__file__).resolve().parent
ENTRYPOINT_SCHEMA = "claudna.entrypoint/1"
```

`spawn_worker` (`:174,177`) drops its local `package` and uses `str(ENTRYPOINT)` — behaviour identical. New function after `harvest_choice` (`:122`):

```python
def write_entrypoint(root: Path, *, agent_cli: str) -> Path:
    """Publish where this plugin's store entrypoint is (spec §8, F16): ``<root>/entrypoint.json``.

    Claudlobby's export job runs ``python3 -S <entrypoint> export …`` from this record instead of guessing a
    plugin-cache path. Rewritten on every opening SessionStart, so a plugin update shows at the next start;
    0600 and atomic (``atomic_write_json``). A missing or unreadable manifest leaves ``plugin_version`` null.
    """
    plugin_root = ENTRYPOINT.parents[2]  # session_store → claudna → lib → the plugin root
    manifest = read_json(plugin_root / ".claude-plugin" / "plugin.json")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    path = ensure_dir(root) / "entrypoint.json"
    atomic_write_json(path, {
        "schema": ENTRYPOINT_SCHEMA, "entrypoint": str(ENTRYPOINT), "plugin_root": str(plugin_root),
        "plugin_version": version if isinstance(version, str) else None, "python": sys.executable,
        "agent_cli": agent_cli, "written_at": ev.now_ts(),
    })
    return path
```

In `_session_start`, insert before `return f"session opened ({source})"` (`:162`): `write_entrypoint(root, agent_cli=AGENT_CLI)  # F16: after the store writes, so a failed write can't lose the session`. It sits after the `_SOURCES` check, so `compact` (`:130-135`) never writes. An `OSError` propagates like any store failure: `run_hook` logs it to `hooks/errors.log` (`cli.py:177-193`) and the hook still exits 0. Writer-side statement of what the consumer relies on (spec §8, Task 7) — the consumer's six reasons for not exporting a store: no record → `no_entrypoint`; `schema != "claudna.entrypoint/1"` → `unknown_schema`; `plugin_version` missing or `< 0.28.0` → `old_entrypoint`; an `entrypoint` directory missing on disk → `stale_entrypoint`; the door not answering in time → `export_timeout`; a non-zero exit or non-JSON stdout from it → `export_failed`. None is a reason to guess another path (X18). The key is `agent_cli`, not `host`: it is the join key's name (`session.opened.data.agent_cli`, P1); `host` stays the adapter-module vocabulary (`host_claude.py`, P4).
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_hook.py tests/test_session_store.py -q` (the second file's schema walk now covers the new file). Commit: `feat(session): the opening SessionStart writes <state>/entrypoint.json (F16)`.

### Task 4: freeze the activity layer (F8) — the test

**Files:** `tests/test_session_store_activity.py` (modified; the spec note is Task 7).

- [ ] **Step 1:** add `from claudna.session_store import events as ev` and `from claudna.session_store.project import _COUNTED`, then:

```python
class TestFrozenLayer:
    """F8 (Claudlobby#2145): the activity layer is frozen — these four kinds, these three hooks, no more.
    Intra-session detail is the agent CLI's own OpenTelemetry export; the hooks stay wired (TestWiring).
    Not covered: plugin-hooks/telemetry-emit.sh, Claudosseum's skill_invocation writer, which keeps its own
    hook entry by owner decision (phase-4 plan :97-99; spec §10) — a Claudosseum contract, not this layer."""

    KINDS = ("prompt.submitted", "skill.invoked", "tool.failed", "tool.interrupted")

    def test_the_activity_kinds_are_exactly_todays(self):
        assert tuple(k for k, spec in ev.REGISTRY.items() if spec.log == ev.ACTIVITY) == self.KINDS

    def test_the_activity_hooks_are_exactly_todays(self):
        assert activity.EVENTS == ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure")

    def test_every_frozen_kind_is_counted_by_the_projection(self):
        assert tuple(_COUNTED) == self.KINDS
```

"Hooks stay wired" is already pinned by `tests/test_session_store_hook.py::TestWiring` (`:377-389`, the `wired` tuple); no new wiring test.
- [ ] **Step 2:** Verify: `python3 -m pytest tests/test_session_store_activity.py -q`. Commit: `test(activity): pin the frozen activity layer (F8)`.

### Task 1b: tag the summarizer's telemetry (epic §14 Q15, answered 2026-10-05)

**Files:** `lib/claudna/session_store/summarize.py`, `tests/test_session_store_summarize.py` (both modified). C6a measured that
the summarizer's `claude -p` children export full sessions under the bot's telemetry env; the operator chose (c), tag and fold.

- [ ] **Step 1 (tests first):** `test_the_child_tags_its_telemetry` — `run_claude`'s child env (captured with a stub `subprocess.run`)
  carries `OTEL_RESOURCE_ATTRIBUTES` ending in `claudna.role=summarizer`, appended after a comma to an inherited value and alone
  when none is inherited; nothing else in the env changes.
- [ ] **Step 2:** `summarize.py:83`, after `child_env = {**env, CHILD_ENV: "1"}`: append `claudna.role=summarizer` to
  `OTEL_RESOURCE_ATTRIBUTES` (comma-joined, no duplicate). Harmless with telemetry off: the variable is only read by an exporter.
- [ ] **Step 3:** CHANGELOG `### Changed` bullet under this PR's entry. Commit: `feat(summarize): tag the summarizer's telemetry (Claudlobby#2145 Q15)`.

### Task 5: the nested-child guard does not read `CLAUDE_CODE_CHILD_SESSION` — a pin (P0 fold, 2026-10-05)

> **Re-written from C10/C11 (Claudlobby run log, 2026-10-05).** The forge draft below had the guard return "nested"
> whenever `CLAUDE_CODE_CHILD_SESSION=1`. The canaries measured that marker on **every** hook process of a top-level
> `claude -p` session (SessionStart through SessionEnd, the main thread's and a subagent's alike) and in every Bash
> tool's env: Claude Code sets it on whatever it spawns. Shipped, the draft would have ignored every event of every
> session. The marker cannot tell a nested `claude` from its parent, and neither the pid nor the entrypoint
> checks need it. So this task is now a regression pin, with no behaviour change and no hold:
>
> - **Step 1 (tests):** in `TestNestedChildren` (`:436`), `test_claude_codes_child_marker_is_not_a_nested_signal`:
>   a top-level session whose every hook env carries `CLAUDE_CODE_CHILD_SESSION=1` (`{**self.env("cli", 100),
>   "CLAUDE_CODE_CHILD_SESSION": "1"}`) opens, records a `PreCompact` seal and closes exactly as without it. The
>   docstring names the run-log entry. Green on today's code; it fails the day someone adds the draft's line.
> - **Step 2:** `inherited`'s docstring (`:288-306`) gains one sentence: "Claude Code's `CLAUDE_CODE_CHILD_SESSION=1`
>   is on every hook and tool process, a top-level session's included (Claudlobby#2145 C11), so it is not read."
> - **Step 3:** `SETUP_GUIDE.md` §3.7 gains no bullet. Commit: `test(session): the guard ignores Claude Code's child marker — it is on every hook (#2145 C11)`.
>
> The draft's Steps 1–4 below are struck, kept for the record.

~~
**Files:** `lib/claudna/session_store/boundaries.py`, `tests/test_session_store_hook.py`, `SETUP_GUIDE.md` (all modified). Hold this task's commit until C10 is answered and, if it leaks, until P1 Claudlobby Task 7b (Half A; `unset CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE`, tested in `tests/test_boot_policy_conformance.py`) has landed on every bot; the rest of the PR — and the 0.28 release — does not wait (X3). **The hold's vehicle:** if 0.28 is cut before C10 is answered (or before Task 7b is on every bot), Task 5 lands as its own PR, released as 0.28.x, with its own before-leg (Task 0's two commands, its own evidence files) and gate (Task 9's `make check`, the 3.9 leg and the comparison against that before-leg); Task 8's Task 5 CHANGELOG line moves with it into that release's entry. In the planned order C10 runs first (epic §10 order 1) and Task 7b ships in Half A, before this PR, so the hold should rarely bite.~~

- [ ] ~~**Step 1 (tests first):** in `TestNestedChildren` (`:436`): `test_claude_codes_own_child_marker_is_enough` — parent `self.env("cli", 100)` opens; a child with `{**self.env("cli", 100), "CLAUDE_CODE_CHILD_SESSION": "1"}` (same pid, same entrypoint — today's three checks would let it through) gets `ignored: nested` on `SessionStart(startup)`, `PreCompact` and `SessionEnd`; the session stays open with one segment; the parent still closes. `test_a_marked_child_records_nothing_of_its_own` — a child with the marker and a **fresh** id (`boundaries.handle` with `session_id="child-1"`) gets `ignored: nested` on its `SessionStart(startup)` and nothing exists under `sessions/child-1` (spec §4.4 `:145`: "a real child marker would still be the stronger signal where it exists" — a marked child records nothing, like `CLAUDNA_SESSION_CHILD`). `test_a_marked_resume_is_ignored_too` — the marker wins over the resume exemption.~~
- [ ] ~~**Step 2:** `boundaries.py`: constant after `INTERACTIVE_ENTRYPOINTS`: `CHILD_SESSION_ENV = "CLAUDE_CODE_CHILD_SESSION"  #: Claude Code's own marker for a nested session's processes (documented; C10 measured the leak)`. `inherited` (`:307`) gains, as its **first** statement, before the unknown/resume preamble: `if env.get(CHILD_SESSION_ENV) == "1": return True`. Docstring: first line becomes "Is this hook a nested ``claude``'s — Claude Code's own marker says so, or it reuses an existing session's id? (spec §11.5)"; a first bullet names the marker; the three existing bullets become "the fallbacks for a Claude Code that doesn't set it". `handle` (`:337`) returns `"ignored: nested child (a Claude Code child session, or an inherited session id)"` — every existing assertion is `startswith("ignored: nested")`. Not in the wrapper: one guard, in Python, so `scripts/session_canary.py:44` (which imports `inherited`) keeps asking the real one.~~
- [ ] ~~**Step 3:** `SETUP_GUIDE.md` §3.7 bullets (after `:320`): "**Nested sessions:** a `claude` started from inside a session carries Claude Code's `CLAUDE_CODE_CHILD_SESSION=1` and records nothing; a child that inherited its parent's session id is ignored on the same grounds." The env table (`:309-317`) gains no row: this is Claude Code's variable, not a clauDNA setting.~~
- [ ] ~~**Step 4:**~~ Verify (the pin): `python3 -m pytest tests/test_session_store_hook.py tests/test_session_canary.py -q`.

### Task 6: SETUP_GUIDE — the owning agent process (F13, unconditional), and one false claim in three places

**Files:** `SETUP_GUIDE.md`, `lib/claudna/session_store/telemetry.py`, `documentation/plans/2026-09-30-session-store-phase-4.md` (all modified).

- [ ] `:316` "How long a session whose `claude` process is gone may sit open…" → "How long a session whose owning agent process (the `claude` that opened it, recorded as `claude_pid`) is gone may sit open…"; `:320` "…and whose `claude` process is gone, and marks them `abandoned`" → "…and whose owning agent process is gone, and marks them `abandoned`". No other row changes semantics in this PR (`:313`'s entrypoint list is P4's). Add one bullet after `:320`: "**For fleet tooling:** each opening SessionStart rewrites `<state root>/entrypoint.json` (`$CLAUDNA_STATE_DIR`, default `~/.claudna`), naming the store's entrypoint and plugin version, so Claudlobby's export job runs the store by contract instead of guessing a plugin-cache path (spec §8)." Commit with Task 8.
- [ ] **Three false claims, one fact (D6):** `SETUP_GUIDE.md:704` "Fleet bots | **On** (Claudlobby sets `CLAUDNA_TELEMETRY=1`)" → "Fleet bots | **Off** — Claudlobby composes no `CLAUDNA_TELEMETRY` (`composer.py:1319-1330`); a fleet opts a bot in with `CLAUDNA_TELEMETRY=1` in its `env:`" (not "composes only `CLAUDNA_VERSION`": by 0.28 every `bot.conf` also carries `CLAUDNA_STATE_DIR` and `CLAUDNA_SESSION_SUMMARY`, P3 Claudlobby Task 1); `telemetry.py:3` "(Claudlobby sets it for fleet bots)" → "(a fleet sets it per bot through `env:`; Claudlobby composes no default)"; phase-4 plan `:68` "which Claudlobby sets for fleet bots" → the same, dated `(corrected 2026-10-05)`. Claudlobby never has set it; the P3 Claudlobby plan keeps it an operator `env:` knob outside its `claudna:` mapping.

### Task 7: spec §8 contract text, §5 layout, §4.2 freeze note

**Files:** `documentation/specs/2026-09-28-session-store-design.md` (modified). Register rule R3: this text is what Claudlobby conforms to, so it is exact. `0.28` below means the version Task 9 releases (`0.28.0` if P1 shipped as `0.27.0`); write the real number.

- [ ] **§8 — extend P1's table, never rewrite §8 (X4).** P1 clauDNA Task 3 Step 3 inserted an **Item fields and rules** subsection after `:440`: the grammar block, `--since-seg`/`--limit`, the ack rule and the additive rule (new in 0.27, citing `2026-10-01-session-store-hardening.md:44`) live there and stay P1's text; `session`'s row (with `agent_cli`) and `next`'s are P1's too. This task edits that subsection in place — the grammar's first line gains `[--include-skipped]`, `seg`'s and `summary`'s rules are rewritten for status items (P1's "always a `done` summary" and its future-tense "P3 adds `--include-skipped` status items" are deleted: P3 has shipped), the table gains the `segment` and `skipped` rows, and the **Status items** and **entrypoint record** paragraphs follow the table. The text below is what lands, written as rows and paragraphs of P1's subsection (re-grep the anchors at PR-open):

> *The contract line (`:433`, or where P1 moved it):* **Export contract** (the surface Claudron and Claudlobby pin — Claudron register rule R3: consumers conform to this text; the machine gate is `schemas/export.schema.json` here and, once P3 Claudlobby Task 8's conformance leg runs the door, Claudlobby's vendored copy of it). No "drift-gated on their side" claim until that leg exists (X17).
>
> ```
> session export --consumer <name> [--since-seg N] [--limit N] [--include-skipped] --json
> session export --consumer <name> --ack --sid <sid> --through <seg>
> ```
>
> *P1's table, edited in place* (each item is `{ sid, seg, session, summary, segment }`, plus `skipped` on a status item):
> - `seg` — P1's row; its rule becomes "one item per final segment past the cursor: `done` segments, plus status items under `--include-skipped`".
> - `session` — P1's row, untouched (the `SESSION_FIELDS` subset; `session.agent_cli` absent means `claude`).
> - `summary` — P1's row; its rule becomes "the `done` summary, or `null` on a status item" (no "always", no "P3 adds").
> - `segment` (0.28, additive) — `{ sealed_at, sealed_by, counts }`: the seal as `segment.json` records it (§6.5) and `counts` = `{ prompts, skills, failures, interrupts }`; `counts` is `null` for a retired segment, whose activity log is gone.
> - `skipped` (0.28) — `{ reason }`, present only on a **status item**. Status items appear only with `--include-skipped`: `{ …, summary: null, skipped: { reason } }` for each final segment the cursor passes unsummarized. `reason` is the `summary.skipped` reason (`disabled`, `trivial`, `headless`, `no_transcript`), `gave_up` (a summary that will never come: failed for good, attempts spent, or a retry nothing will run), or `retired` (retired before any summary was archived). A segment skipped as `private`, like a private session, never exports. Status items count against `--limit`; `next` is the same with or without the flag; without it the envelope is byte-identical to the flagged one minus the status items. Which reason a bot yields follows the gate (`project.py:276-287`): a Claudlobby bot with summaries unarmed reads `disabled` (its `bot.conf` composes `CLAUDNA_SESSION_SUMMARY=0`, P3 Claudlobby Task 1); `headless` is a bot or `claude -p` with the variable unset. A consumer treats an unknown reason as a skip.
> - `next` and the ack rule — P1's rows, untouched.
>
> *`:440`'s envelope sentence and §1.1 rule 2 (`:33`), amended (D5):* the plane never parses the store's *session* files; the one file a consumer reads is the published `entrypoint.json`, the second contract surface (below). Item keys are additive (P1's rule), and a consumer ignores what it doesn't know.
>
> **The entrypoint record** (0.28, F16). The store's own opening SessionStart (`startup`, `clear`, `resume`, `fork`; never `compact`) writes `<CLAUDNA_STATE_DIR>/entrypoint.json`, mode 0600, by atomic replace:
>
> ```json
> {"schema": "claudna.entrypoint/1", "entrypoint": "/abs/…/lib/claudna/session_store", "plugin_root": "/abs/…",
>  "plugin_version": "0.28.0", "python": "/usr/bin/python3", "agent_cli": "claude", "written_at": "2026-10-04T12:00:00.000Z"}
> ```
>
> `entrypoint` is the package directory the hooks themselves run (`python3 -S <entrypoint> <verb>`); `plugin_root` its plugin; `plugin_version` from `<plugin_root>/.claude-plugin/plugin.json`, `null` when unreadable; `agent_cli` the runtime that wrote it (`claude`; `codex` once that host ships) — the join key's name, not `host` (X18); `written_at` in the store's timestamp format. A consumer runs `python3 -S <entrypoint> export --consumer <name> [--include-skipped] --json` and `… --ack --sid <sid> --through <seg>`. A consumer that does not export a store names why, in one of six reasons: `no_entrypoint` (no record), `unknown_schema` (`schema` ≠ `claudna.entrypoint/1`), `old_entrypoint` (`plugin_version` missing or `< 0.28.0`), `stale_entrypoint` (the recorded `entrypoint` no longer exists — a plugin replaced under a running session; skipped until the next SessionStart rewrites the record), `export_timeout` (the door did not answer in time) and `export_failed` (a non-zero exit or non-JSON stdout). None is a reason to guess another path. Schema: `schemas/entrypoint.schema.json`. The record answers outside a session what `<claudna-root>` (`SKILL_CONTRACT.md` §1.1 `:26-43`) answers inside one — which copy of the plugin is loaded — and formalizes, with a schema, the plugin-writes/fleet-reads shape of `<BOT_DIR>/.claude/session.md` (Claudlobby#2094). It is written by the store hook (no matcher, gated by `CLAUDNA_SESSION_STORE`), not the briefing hook, which bots turn off.

- [ ] **§5 (`:149-173`)**: under the root, before `links/`, add `  entrypoint.json            # where this plugin's store entrypoint is (§8, F16); rewritten at each opening SessionStart`.
- [ ] **§4.2**, after the table (`:116`): "**Frozen (2026-10-04, Claudlobby#2145 F8).** The activity layer — `prompt.submitted`, `skill.invoked`, `tool.failed`, `tool.interrupted` from the three hooks above — takes no new kinds and no new hooks, and the hooks stay wired. Intra-session detail is the runtime's own OpenTelemetry export, normalized by Claudlobby's normalization layer (Claudlobby#2145 P2-a). Unwiring waits until interactive users have OTel too. Pinned by `tests/test_session_store_activity.py::TestFrozenLayer` and `tests/test_session_store_hook.py::TestWiring`."
- [ ] **§10 Hosts (`:453`)**: one sentence — "A host adapter's name is the `agent_cli` value it records (`session.opened.data.agent_cli`, `entrypoint.json.agent_cli`); `host` is the adapter-module vocabulary (`host_claude.py`, P4), never a second key" (X18).
- [ ] **§4.4 (`:145`) and §11.5 (`:464`)** — close the Claudlobby#1961 thread (D6): `CLAUDLOBBY_HOOK_CHILD` was never built; and Claude Code's own `CLAUDE_CODE_CHILD_SESSION=1` is no child marker (it is on every hook, C11). `:145` "(and Claudlobby's child marker once one ships — `CLAUDLOBBY_HOOK_CHILD` is proposed in Claudlobby#1961; none exists today)" → "(no runtime marker exists: Claude Code's `CLAUDE_CODE_CHILD_SESSION=1` is set on every hook and tool process, a top-level session's included — Claudlobby#2145 C11 — so the pid and entrypoint checks are the guard)"; `:464` "a hook-suppression marker (Claudlobby#1961)" → "the `CLAUDNA_SESSION_CHILD=1` marker; Claudlobby#1961's `CLAUDLOBBY_HOOK_CHILD` was never built". *(P0 fold, 2026-10-05: the forge draft's "which the guard reads first" is falsified.)*
- [ ] Commit: `docs(spec): §8 extends the item table — status items, the segment object, entrypoint.json (agent_cli); §1.1 rule 2; §5 layout; §4.2 freeze; §10 hosts; #1961 closed (F6, F8, F16)`.

### Task 8: CHANGELOG

**Files:** `CHANGELOG.md` (modified; `scripts/check-changelog.sh` requires new `[Unreleased]` content).

- [ ] `### Added`: "**The export door carries what Claudlobby's `session_summary` needs** (Claudlobby#2145 P3). Every `claudna.export/1` item gains `segment: {sealed_at, sealed_by, counts}` (`counts` is `null` for a retired segment). `session export --include-skipped` adds a status item `{summary: null, skipped: {reason}}` for each final segment the cursor passes unsummarized — `reason` is the summary-gate reason, `gave_up` or `retired`; `private` never exports — so a fleet monitor sees coverage, not just summaries; without the flag the envelope is unchanged. Each opening SessionStart now writes `<state root>/entrypoint.json` (`$CLAUDNA_STATE_DIR`, default `~/.claudna`; `claudna.entrypoint/1`: the store's entrypoint, plugin root, version and `agent_cli`), so Claudlobby runs the store by contract instead of guessing the plugin-cache path (spec §8); `schemas/export.schema.json` pins the envelope."
- [ ] `### Changed`: "**The activity layer is frozen** (F8): its four kinds and three hooks are pinned; new intra-session detail comes from the runtime's own telemetry." · "**SETUP_GUIDE says "owning agent process"** where it said "`claude` process" (F13)." · *(Task 5's heads-up bullet is struck in the P0 fold, 2026-10-05: Task 5 is now a test-only pin, no user-facing change.)*
- [ ] Commit (with Task 6): `docs: owning-agent wording (F13), the entrypoint record and the export additions in SETUP_GUIDE and CHANGELOG`.

### Task 9: gate, the live check, release

- [ ] `make check` in the worktree (CI runs the same target, `Makefile:22`); then the 3.9 leg: `PATH="<venv-39>/bin:$PATH" make test-runtime` (the export, hook and activity suites are in `RUNTIME_TESTS`, `:54-61`); `tests/test_runtime_layout.py` (layering, stdlib-only, one shim) runs inside both. Compare failing names against Task 0's before-leg files; only pre-existing names may remain.
- [ ] Live check on this machine: `claude --plugin-dir ~/Projects/claudna-p3-export`, type one prompt, `/exit`; then `jq . ~/.claudna/entrypoint.json` shows `entrypoint` under the worktree and `plugin_version` the bumped number (the marketplace 0.26.0 copy writes nothing, so nothing overwrites it); `python3 -S "$(jq -r .entrypoint ~/.claudna/entrypoint.json)" export --consumer test --json | python3 -m json.tool | head -40` prints a `claudna.export/1` envelope whose items carry `segment`; add `--include-skipped` and confirm a `skipped` item appears for a session whose segment was skipped (`headless`/`disabled` — this session, if `CLAUDNA_HARVEST` is unset). Paste both outputs, paths redacted to `~`, into the PR body. Also record, for X18: `ls ~/.claude/plugins/cache/Claudfather/claudna/` before and after `claude plugin update claudna` — does the prior `<version>` directory survive? It decides whether a stale `entrypoint` can point at a still-present older copy, and so what the consumer's staleness check really sees; the answer goes into the PR body and the P3 Claudlobby canary.
- [ ] PR → review → merge. **Before merging,** §10 order 3 has landed and every bot has restarted onto its per-bot root (`CLAUDNA_STATE_DIR`; the P3 Claudlobby plan's Task 1 and Dependencies): marketplace auto-update (`start-bot.sh:315-321` → `lib-common.sh:4693-4710`, `claude plugin update` on every bot start unless `BOOT_PLUGIN_UPDATE_ONCE=1`) delivers 0.28 to every tmux-hosted bot at its next restart, C10 answered or not, and nothing pins the installed plugin (`claudna_version` only exports `CLAUDNA_VERSION`) — so an early 0.28 must land where it harms nothing: the bot's own root, where it writes `entrypoint.json` and, on a bot with summaries unarmed, its summary gate answers `headless` (no spend). That is also why Task 5, not the release, is what C10 gates (D9/X21). Release on `main` per `CONTRIBUTING.md:128-138`: `./scripts/release.sh minor` (both manifests, the CHANGELOG section, the commit and tag; it does not push), push the commit and the tag; `release-tag.yml` also tags on the version change. Then the P3 Claudlobby plan's 6b may open: its floor is 0.28.0 (`MIN_PLUGIN_VERSION`, read from `entrypoint.json`), and there is no `claudna_version` to bump — nothing reads it as a pin.
- [ ] Open the Claudron PR that flips boundary-spec §10.4 register row 11 (the clauDNA export contract: `--include-skipped`, `segment`, `entrypoint.json`) from *planned* to shipped, citing the 0.28 tag — the authoritative text stays here (R2); the register points at it (X14).

## Test Plan

New: `TestExport::test_every_item_carries_the_segment_object`, the archived-item assertions in `TestReviewRound387`, `TestIncludeSkipped` (eight tests, one against `schemas/export.schema.json`) and the extended `test_the_cli` in `tests/test_session_store_export.py`; `TestEntrypoint` (five tests) and the wrapper assertion in `tests/test_session_store_hook.py`; `TestFrozenLayer` (three) in `tests/test_session_store_activity.py`; `TestNestedChildren` gains three (Task 5). Existing: `tests/test_session_store.py::TestSchemas` now also walks `entrypoint.schema.json` and `export.schema.json`. All of these run in both legs (`RUNTIME_TESTS`). Manual: Task 9's live check. Coverage: every branch of the new `take()` (done / skipped / retired-with-archive / retired-without / settled / unsettled) has a test naming it.

## Verification Checklist

- [ ] `python3 -m pytest tests/ -q` and `make test-runtime` under `/usr/bin/python3` (3.9): no failing name that is not in Task 0's before-leg files.
- [ ] `make check` green (lint at `line-length = 120`, manifests, changelog gate).
- [ ] `python3 -m pytest tests/test_session_store_export.py -q -k "unchanged"`: the unflagged envelope equals the flagged one minus status items; no default item has a `skipped` key.
- [ ] `~/.claudna/entrypoint.json` exists after a real SessionStart, is `0600`, validates against `schemas/entrypoint.schema.json`, carries `agent_cli: "claude"`, and `python3 -S <its entrypoint> export --consumer test --json` exits 0 with a `claudna.export/1` envelope.
- [ ] Mutation probes, shown then restored: drop `"counts"` from `segment_record` → Task 1's test fails; return `"trivial"` for every reason → the `retired`/`gave_up` tests fail; add an `ACTIVITY` kind to `REGISTRY` → `TestFrozenLayer` fails; change the schema's `written_at` pattern → `test_every_timestamp_pattern_is_the_envelopes` fails.
- [ ] Spec §8 defines every item key the code emits (`sid, seg, session, summary, segment, skipped`) **exactly once** (`grep -c` per key over the Item fields table — P1's rows plus this PR's; X4), no row still says "P3 adds" or "always a `done` summary" (`grep -c` prints 0 for each over §8), `SESSION_FIELDS` in the text equals `export.SESSION_FIELDS`, and the flagged envelope validates against `schemas/export.schema.json`; `grep -c Collector` over the spec prints 0.
- [ ] (Task 5) `test_claude_codes_child_marker_is_not_a_nested_signal` is green, and `grep -c CLAUDE_CODE_CHILD_SESSION lib/claudna/session_store/boundaries.py` counts only the docstring sentence.

## What NOT To Do

- Don't write `entrypoint.json` from `plugin-hooks/session-start.sh`: bots set `CLAUDNA_SESSION_BRIEFING=0` and it fires only for `startup|clear` (epic §11; F16 forge note).
- Don't compute the entrypoint from `${CLAUDE_PLUGIN_ROOT}` or by reading Claude Code's `installed_plugins.json`; `Path(__file__)` is the one source (`ENTRYPOINT`), shared with `spawn_worker`.
- Don't add `"skipped": null` to regular items, don't bump `claudna.export/1`, and don't change `next` or `ack` — the only default-output change is the additive `segment` key.
- Don't emit a status item for a `private` skip or a private session.
- Don't add `agent_cli` anywhere (P1 did), `--host` or `host_*.py` (P4), or a new `session_store` module (none is needed; the layering gate would also demand a rank).
- Don't read `CLAUDE_CODE_CHILD_SESSION` in the guard, in the wrapper or anywhere else: it is on every hook of every session (C11, 2026-10-05), so reading it ignores every event and no session ever closes — the failure the forge draft guarded against only for a leak is the normal case.
- Don't rewrite §8 from the pre-P1 text: extend P1's **Item fields and rules** table in place (X4).
- Don't write "Collector" into the spec: the normalization layer is Claudlobby's to name (the P1 clauDNA plan's rule) — A-F4 replaced the Collector with the plane's `plane-otel` intake on 2026-10-05, and a spec that names no layer needed no re-amending.
- Don't name the record's adapter `host`: the key is `agent_cli`, the join key's name (X18).
- Don't use 3.10+ syntax in `lib/` (`match`, `X | Y` at runtime, `zip(strict=)`): the floor is 3.9; annotations are fine under `from __future__ import annotations`.
- Don't let `write_entrypoint` swallow its `OSError`: `run_hook` logs it; a silent failure would leave a stale record nobody notices.

## Context

area: session store / export door · effort: M, nearer S–M (three additive contract changes and one file write; D7) · risk: Medium (two runtime-behaviour changes in the SessionStart hook — a new file write; the guard is unchanged — Task 5 is a pin, P0 fold; the export changes are additive) · priority: P3 (plan 5 of §10.1) · related: Claudlobby#2145 (epic), Claudlobby#1961 (the digest's coverage concern F6 keeps), clauDNA#373 (nested children, M3), clauDNA#387 (export review S4).

## Canary answers this PR waits on

| Canary | Task | If it answers the other way |
|---|---|---|
| **C10** — does `CLAUDE_CODE_CHILD_SESSION` reach a bot whose tmux server was started from inside another Claude session? | 5 | **Leaks:** P1 Claudlobby Task 7b (Half A; `unset CLAUDE_CODE_CHILD_SESSION` before `exec $CLAUDE`, `:223-229,268`; it unsets nothing today; tested in `tests/test_boot_policy_conformance.py`) lands and is deployed to every bot first; Task 5 is held until then — **the 0.28 release does not wait** (X3, D9). **Doesn't leak:** Task 5 ships as written. **Answered 2026-10-05 (run log): it leaks, and it does not matter here — the marker is on every hook of a top-level session too (C11). Task 5 is the opposite pin and holds nothing.** |
| **C11, hook-side reading** — do `PostToolUse`/`PostToolUseFailure` hook processes fired by an Agent subagent's tool calls carry `CLAUDE_CODE_CHILD_SESSION=1` with the parent's `session_id`? (C11 already runs hooks from a subagent; a 3-line scratch hook appending `env | grep ^CLAUDE_CODE_` to a file records it — no repo change.) | 5 | **They do:** the marker check in `inherited` is restricted to `event in ("SessionStart", "PreCompact", "SessionEnd")` so the parent keeps its subagents' `skill.invoked`/`tool.failed`; activity events keep the three fallback checks (which already catch a reused id by pid). **They don't:** as written. **Answered 2026-10-05 (headless): they do, with the parent's `session_id` — and so do the parent's own hooks. The payload's `agent_id`/`agent_type` is what tells a subagent's hook apart; the guard reads neither, and the activity events keep their pid/entrypoint checks.** |

When the Claudlobby run log records C10 and C11, the same rows are added to `documentation/plans/2026-09-30-session-store-phase-3.md`'s canary table (`:23-25`, the #395 form), naming `scripts/session_canary.py` where it was the harness (X16).
