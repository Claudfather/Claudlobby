---
title: "P3 — the export contract Claudlobby consumes: skipped items, the segment object, `entrypoint.json`, the frozen activity layer (clauDNA)"
type: plan
status: draft
owner: chrisrogers37
created: 2026-10-04
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
> Depends on: the P1 clauDNA release (`session.opened.data.runtime`, `claudna.session/2`, `runtime` in
> `SESSION_FIELDS`). Waits on canaries: **C10 for Task 5 only**, plus the hook-env reading from C11's
> subagent leg (see *Canary answers this PR waits on*). Everything else ships regardless.

## Summary

This PR makes clauDNA's export door carry what Claudlobby's P3 `session-export` job needs and nothing it has to guess: an opt-in `--include-skipped` that turns the three segment kinds the cursor silently passes into *status items* (F6), an additive `segment: {sealed_at, sealed_by, counts}` object on every item (the `session_summary` event's volume fields), and `<CLAUDNA_STATE_DIR>/entrypoint.json`, written by the store's own opening SessionStart so the job finds the store by contract instead of parsing Claude Code's plugin registry (F16). It freezes the activity layer with a test and a spec note (F8), reads Claude Code's own child marker first in the nested-child guard (conditional on C10), fixes the two SETUP_GUIDE sentences F13 changes, writes the new surfaces into spec §8 as contract text (Claudron register rule R3: Claudlobby conforms to it), and releases. It deliberately leaves the host split (`host_claude.py`/`host_codex.py`, `--host`), every Codex field, and the per-bot state-dir cutover (F14, a Claudlobby composition change) to P4 and the P3 Claudlobby plan.

## Evidence (clauDNA at `71f983d`)

- `lib/claudna/session_store/export.py:10-13` the item shape: `{sid, seg, session: <a session.json subset>, summary: <the segment summary>}`; `:46-47` `SESSION_FIELDS` (P1 appends `"runtime"`); `:81-82` `export(store, consumer, *, since_seg=None, limit=100, now=None)`; `:92` `start = max(handle.cursor(consumer), since_seg or 0)`; `:98-99` private sessions `continue`; `:106-116` `take()` — a retired segment with no archive passes (`:108-109`), `state.summary in ("done", "skipped")` returns `None` for skipped (`:113-114`), a settled give-up passes (`:115-116`); `:124-128` the item is built only `if doc is not None`; `:129` `through = index` advances regardless; `:135-149` `ack` (never back, never past the last final segment).
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
- Claudlobby (`cd292cb`; checkout `2dd0aad5`, identical here): `_runtime_scripts/start-bot.sh:224-227` sources `bot.conf` under `set -a`, `:268` `exec $CLAUDE …`; no `unset CLAUDE_CODE_*` anywhere in the script — the C10 leak path is open today.

## Implementation Plan

### Dependencies
The **P1 clauDNA release** on `main`: `SESSION_FIELDS` carries `runtime`, `session.json` is `claudna.session/2`, and `boundaries.py` has the constant P1 passes as `runtime=` to `open_session` (called `RUNTIME` below; if P1 named it otherwise, use P1's name — never a second literal). Nothing here needs Claudlobby or Claudron to have moved.

### Blocks
The P3 Claudlobby plan (`2026-10-04-runtime-neutral-observability-p3-claudlobby-summaries.md`): its `session-export` job runs `python3 -S <entrypoint> export --consumer claudlobby --include-skipped --json` and fills `session_summary` from the `segment` object. It cannot open until this PR is **released** (plan index §10.1: plan 5 releases before plan 6 consumes it).

### Steps
Tasks 1–2 (`export.py`, `cli.py`, `tests/test_session_store_export.py`) are disjoint from Task 3 (`boundaries.py`, a new schema, `tests/test_session_store_hook.py`) and Task 4 (`tests/test_session_store_activity.py`), so they can be developed in parallel and land in one PR. Task 5 edits the same two files as Task 3 and follows it. Tasks 6–8 are docs; Task 9 gates and releases.

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

**Files:** `lib/claudna/session_store/export.py`, `lib/claudna/session_store/cli.py`, `tests/test_session_store_export.py` (all modified).

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

The loop (`:118-131`): `go_on, doc, reason = take(index)`; the item condition becomes `if doc is not None or (include_skipped and reason not in (None, "private")):`; the item dict is built as in Task 1 and, `if doc is None`, gains `item["skipped"] = {"reason": reason}` — regular items never carry a `skipped` key. `private` passes with no item because a summary withheld by the person's choice is not a coverage fact for a fleet monitor (the spec's reason set excludes it). Docstring (`:19-20`) gains one sentence naming the flag and the three reasons.
- [ ] **Step 3:** `cli.py`: after `:368` add `exp.add_argument("--include-skipped", action="store_true", help="also emit a status item {summary: null, skipped: {reason}} for each segment the cursor passes unsummarized")`; `:274` passes `include_skipped=args.include_skipped`. The `hook` hot path (`:316`) is untouched.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_export.py -q`. Commit: `feat(export): --include-skipped emits status items for the segments the cursor passes (F6)`.

### Task 3: the opening SessionStart writes `<CLAUDNA_STATE_DIR>/entrypoint.json` (F16)

**Files:** `lib/claudna/session_store/boundaries.py` (modified), `lib/claudna/session_store/schemas/entrypoint.schema.json` (new), `tests/test_session_store_hook.py` (modified).

- [ ] **Step 1 (tests first):** a new class `TestEntrypoint` in `tests/test_session_store_hook.py` (imports: `schema` from `claudna.session_store`, `subprocess`, `sys` already there):
  - `test_an_opening_session_start_writes_the_record`: `fire(store, "SessionStart", transcript, source="startup")`; `doc = json.loads((store.root / "entrypoint.json").read_text())`; `schema.validate(doc, schema.load("entrypoint")) == []`; `doc["entrypoint"] == str(Path(boundaries.__file__).resolve().parent)`; `doc["plugin_root"] == str(REPO_ROOT)`; `doc["plugin_version"] == json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]`; `doc["host"] == "claude"`; `doc["python"] == sys.executable`; `(store.root / "entrypoint.json").stat().st_mode & 0o077 == 0`.
  - `test_the_record_is_rewritten_on_every_opening_start_and_never_on_compact`: after the startup, overwrite the file with `{"stale": true}`; `PreCompact` then `SessionStart(compact)` leave it stale; `SessionEnd` then `SessionStart(resume)` rewrite a valid record.
  - `test_the_recorded_entrypoint_runs_the_export_door`: `subprocess.run([sys.executable, "-S", doc["entrypoint"], "export", "--consumer", "test", "--json", "--root", str(store.root)], capture_output=True, text=True)` → `returncode == 0` and `json.loads(stdout)["schema"] == "claudna.export/1"` — the contract in one assertion: the recorded path is the directory form the hooks use.
  - `test_a_missing_or_malformed_manifest_leaves_the_version_null`: `monkeypatch.setattr(boundaries, "ENTRYPOINT", tmp_path / "lib" / "claudna" / "session_store")` (mkdir parents); `boundaries.write_entrypoint(root, host="claude")` → `plugin_version is None`, `plugin_root == str(tmp_path)`; write `{"version": 3}` to `tmp_path/.claude-plugin/plugin.json` → still `None`.
  - `test_a_failed_write_is_logged_and_the_session_is_still_open`: `monkeypatch.setattr(boundaries, "atomic_write_json", raising OSError)`; through `run_hook(...)` (the `:248-256` recipe) the result starts with `"error:"`, `hooks/errors.log` has the line, and `sessions/<SID>/seg-001` exists.
  - In `TestWrapper.test_it_records_prints_nothing_and_exits_0` (`:304-309`): assert `(state / "entrypoint.json").is_file()` and its `entrypoint == str(REPO_ROOT / "lib" / "claudna" / "session_store")` — the real wrapper, the real package.
- [ ] **Step 2:** `schemas/entrypoint.schema.json` (new; only `_SUPPORTED` keywords, and the envelope's timestamp pattern, or `tests/test_session_store.py:648-655` fail):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "claudna.entrypoint/1",
  "title": "<state>/entrypoint.json — where this plugin's store entrypoint is (spec §8, F16)",
  "type": "object",
  "required": ["schema", "entrypoint", "plugin_root", "plugin_version", "python", "host", "written_at"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "claudna.entrypoint/1"},
    "entrypoint": {"type": "string", "minLength": 1},
    "plugin_root": {"type": "string", "minLength": 1},
    "plugin_version": {"type": ["string", "null"], "minLength": 1},
    "python": {"type": "string", "minLength": 1},
    "host": {"enum": ["claude", "codex"]},
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
def write_entrypoint(root: Path, *, host: str) -> Path:
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
        "host": host, "written_at": ev.now_ts(),
    })
    return path
```

In `_session_start`, insert before `return f"session opened ({source})"` (`:162`): `write_entrypoint(root, host=RUNTIME)  # F16: after the store writes, so a failed write can't lose the session`. It sits after the `_SOURCES` check, so `compact` (`:130-135`) never writes. An `OSError` propagates like any store failure: `run_hook` logs it to `hooks/errors.log` (`cli.py:177-193`) and the hook still exits 0.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_hook.py tests/test_session_store.py -q` (the second file's schema walk now covers the new file). Commit: `feat(session): the opening SessionStart writes <state>/entrypoint.json (F16)`.

### Task 4: freeze the activity layer (F8) — the test

**Files:** `tests/test_session_store_activity.py` (modified; the spec note is Task 7).

- [ ] **Step 1:** add `from claudna.session_store import events as ev` and `from claudna.session_store.project import _COUNTED`, then:

```python
class TestFrozenLayer:
    """F8 (Claudlobby#2145): the activity layer is frozen — these four kinds, these three hooks, no more.
    Intra-session detail is the runtime's own OpenTelemetry export; the hooks stay wired (TestWiring)."""

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

### Task 5: the nested-child guard reads `CLAUDE_CODE_CHILD_SESSION=1` first — **conditional on C10**

**Files:** `lib/claudna/session_store/boundaries.py`, `tests/test_session_store_hook.py`, `SETUP_GUIDE.md` (all modified). Hold this task's commit until the canary section below is answered; the rest of the PR does not wait.

- [ ] **Step 1 (tests first):** in `TestNestedChildren` (`:436`): `test_claude_codes_own_child_marker_is_enough` — parent `self.env("cli", 100)` opens; a child with `{**self.env("cli", 100), "CLAUDE_CODE_CHILD_SESSION": "1"}` (same pid, same entrypoint — today's three checks would let it through) gets `ignored: nested` on `SessionStart(startup)`, `PreCompact` and `SessionEnd`; the session stays open with one segment; the parent still closes. `test_a_marked_child_records_nothing_of_its_own` — a child with the marker and a **fresh** id (`boundaries.handle` with `session_id="child-1"`) gets `ignored: nested` on its `SessionStart(startup)` and nothing exists under `sessions/child-1` (spec §4.4 `:145`: "a real child marker would still be the stronger signal where it exists" — a marked child records nothing, like `CLAUDNA_SESSION_CHILD`). `test_a_marked_resume_is_ignored_too` — the marker wins over the resume exemption.
- [ ] **Step 2:** `boundaries.py`: constant after `INTERACTIVE_ENTRYPOINTS`: `CHILD_SESSION_ENV = "CLAUDE_CODE_CHILD_SESSION"  #: Claude Code's own marker for a nested session's processes (documented; C10 measured the leak)`. `inherited` (`:307`) gains, as its **first** statement, before the unknown/resume preamble: `if env.get(CHILD_SESSION_ENV) == "1": return True`. Docstring: first line becomes "Is this hook a nested ``claude``'s — Claude Code's own marker says so, or it reuses an existing session's id? (spec §11.5)"; a first bullet names the marker; the three existing bullets become "the fallbacks for a Claude Code that doesn't set it". `handle` (`:337`) returns `"ignored: nested child (a Claude Code child session, or an inherited session id)"` — every existing assertion is `startswith("ignored: nested")`. Not in the wrapper: one guard, in Python, so `scripts/session_canary.py:44` (which imports `inherited`) keeps asking the real one.
- [ ] **Step 3:** `SETUP_GUIDE.md` §3.7 bullets (after `:320`): "**Nested sessions:** a `claude` started from inside a session carries Claude Code's `CLAUDE_CODE_CHILD_SESSION=1` and records nothing; a child that inherited its parent's session id is ignored on the same grounds." The env table (`:309-317`) gains no row: this is Claude Code's variable, not a clauDNA setting.
- [ ] **Step 4:** Verify: `python3 -m pytest tests/test_session_store_hook.py tests/test_session_canary.py -q`. Commit: `feat(session): the nested-child guard reads CLAUDE_CODE_CHILD_SESSION first (C10)`.

### Task 6: SETUP_GUIDE — the owning agent process (F13, unconditional)

**Files:** `SETUP_GUIDE.md` (modified).

- [ ] `:316` "How long a session whose `claude` process is gone may sit open…" → "How long a session whose owning agent process (the `claude` that opened it, recorded as `claude_pid`) is gone may sit open…"; `:320` "…and whose `claude` process is gone, and marks them `abandoned`" → "…and whose owning agent process is gone, and marks them `abandoned`". No other row changes semantics in this PR (`:313`'s entrypoint list is P4's). Add one bullet after `:320`: "**For fleet tooling:** each opening SessionStart rewrites `~/.claudna/entrypoint.json`, naming the store's entrypoint and plugin version, so Claudlobby's export job runs the store by contract instead of guessing a plugin-cache path (spec §8)." Commit with Task 8.

### Task 7: spec §8 contract text, §5 layout, §4.2 freeze note

**Files:** `documentation/specs/2026-09-28-session-store-design.md` (modified). Register rule R3: this text is what Claudlobby conforms to, so it is exact. `0.28` below means the version Task 9 releases (`0.28.0` if P1 shipped as `0.27.0`); write the real number.

- [ ] **§8 (`:433-440`)** replaces the contract line, the fenced block and the envelope sentence with:

> **Export contract** (the surface Claudron and Claudlobby pin; drift-gated on their side per Claudron register rule R3):
>
> ```
> session export --consumer <name> [--since-seg N] [--limit N] [--include-skipped] --json
> session export --consumer <name> --ack --sid <sid> --through <seg>
> ```
>
> Returns `{ schema: "claudna.export/1", consumer, items: [...], next: <cursor> }`. Each item is `{ sid, seg, session, summary, segment }`, plus `skipped` on a status item:
> - `session` — the `session.json` subset `SESSION_FIELDS`: `sid`, `status`, `opened_at`, `closed_at`, `close_reason`, `chain_id`, `parent_sid`, `actor`, `origin`, `runtime` (0.27; absent means `claude`).
> - `summary` — the segment summary (§6.6), or `null` on a status item.
> - `segment` (0.28, additive) — `{ sealed_at, sealed_by, counts }`: the seal as `segment.json` records it (§6.5) and `counts` = `{ prompts, skills, failures, interrupts }`; `counts` is `null` for a retired segment, whose activity log is gone.
> - **Status items** (0.28) appear only with `--include-skipped`: `{ …, summary: null, skipped: { reason } }` for each final segment the cursor passes unsummarized. `reason` is the `summary.skipped` reason (`disabled`, `trivial`, `headless`, `no_transcript`), `gave_up` (a summary that will never come: failed for good, attempts spent, or a retry nothing will run), or `retired` (retired before any summary was archived). A segment skipped as `private`, like a private session, never exports. Status items count against `--limit`; `next` is the same with or without the flag; without it the envelope is byte-identical to the flagged one minus the status items.
> - `next` — per session, the segment the consumer may ack. An ack never moves a cursor back and never passes the session's last final segment.
>
> Consumers read through this command and never parse the store's files; item keys are additive, and a consumer ignores what it doesn't know.
>
> **The entrypoint record** (0.28, F16). The store's own opening SessionStart (`startup`, `clear`, `resume`, `fork`; never `compact`) writes `<CLAUDNA_STATE_DIR>/entrypoint.json`, mode 0600, by atomic replace:
>
> ```json
> {"schema": "claudna.entrypoint/1", "entrypoint": "/abs/…/lib/claudna/session_store", "plugin_root": "/abs/…",
>  "plugin_version": "0.28.0", "python": "/usr/bin/python3", "host": "claude", "written_at": "2026-10-04T12:00:00.000Z"}
> ```
>
> `entrypoint` is the package directory the hooks themselves run (`python3 -S <entrypoint> <verb>`); `plugin_root` its plugin; `plugin_version` from `<plugin_root>/.claude-plugin/plugin.json`, `null` when unreadable; `host` the adapter that wrote it (`claude`; `codex` once that host ships); `written_at` in the store's timestamp format. A consumer runs `python3 -S <entrypoint> export --consumer <name> [--include-skipped] --json` and `… --ack --sid <sid> --through <seg>`. A record whose `entrypoint` no longer exists is **stale** (a plugin replaced under a running session): the consumer skips that store until the next SessionStart rewrites it and never guesses another path. Schema: `schemas/entrypoint.schema.json`. It is written by the store hook (no matcher, gated by `CLAUDNA_SESSION_STORE`), not the briefing hook, which bots turn off.

- [ ] **§5 (`:149-173`)**: under the root, before `links/`, add `  entrypoint.json            # where this plugin's store entrypoint is (§8, F16); rewritten at each opening SessionStart`.
- [ ] **§4.2**, after the table (`:116`): "**Frozen (2026-10-04, Claudlobby#2145 F8).** The activity layer — `prompt.submitted`, `skill.invoked`, `tool.failed`, `tool.interrupted` from the three hooks above — takes no new kinds and no new hooks, and the hooks stay wired. Intra-session detail is the runtime's own OpenTelemetry export, normalized by Claudlobby's Collector. Unwiring waits until interactive users have OTel too. Pinned by `tests/test_session_store_activity.py::TestFrozenLayer` and `tests/test_session_store_hook.py::TestWiring`."
- [ ] Commit: `docs(spec): §8 names the export items — status items, the segment object, entrypoint.json; §5 layout; §4.2 freeze (F6, F8, F16)`.

### Task 8: CHANGELOG

**Files:** `CHANGELOG.md` (modified; `scripts/check-changelog.sh` requires new `[Unreleased]` content).

- [ ] `### Added`: "**The export door carries what Claudlobby's `session_summary` needs** (Claudlobby#2145 P3). Every `claudna.export/1` item gains `segment: {sealed_at, sealed_by, counts}` (`counts` is `null` for a retired segment). `session export --include-skipped` adds a status item `{summary: null, skipped: {reason}}` for each final segment the cursor passes unsummarized — `reason` is the summary-gate reason, `gave_up` or `retired`; `private` never exports — so a fleet monitor sees coverage, not just summaries; without the flag the envelope is unchanged. Each opening SessionStart now writes `~/.claudna/entrypoint.json` (`claudna.entrypoint/1`: the store's entrypoint, plugin root and version), so Claudlobby runs the store by contract instead of guessing the plugin-cache path (spec §8)."
- [ ] `### Changed`: "**The activity layer is frozen** (F8): its four kinds and three hooks are pinned; new intra-session detail comes from the runtime's own telemetry." · "**SETUP_GUIDE says "owning agent process"** where it said "`claude` process" (F13)." · (Task 5, when it lands) "**Heads-up: a `claude` started from inside a session records nothing.** The nested-child guard reads Claude Code's own `CLAUDE_CODE_CHILD_SESSION=1` first; the pid and entrypoint checks remain the fallbacks. Fleet hosts: `start-bot.sh` must not pass that variable to a bot (Claudlobby#2145 C10)."
- [ ] Commit (with Task 6): `docs: owning-agent wording (F13), the entrypoint record and the export additions in SETUP_GUIDE and CHANGELOG`.

### Task 9: gate, the live check, release

- [ ] `make check` in the worktree (CI runs the same target, `Makefile:22`); then the 3.9 leg: `PATH="<venv-39>/bin:$PATH" make test-runtime` (the export, hook and activity suites are in `RUNTIME_TESTS`, `:54-61`); `tests/test_runtime_layout.py` (layering, stdlib-only, one shim) runs inside both. Compare failing names against Task 0's before-leg files; only pre-existing names may remain.
- [ ] Live check on this machine: `claude --plugin-dir ~/Projects/claudna-p3-export`, type one prompt, `/exit`; then `jq . ~/.claudna/entrypoint.json` shows `entrypoint` under the worktree and `plugin_version` the bumped number (the marketplace 0.26.0 copy writes nothing, so nothing overwrites it); `python3 -S "$(jq -r .entrypoint ~/.claudna/entrypoint.json)" export --consumer test --json | python3 -m json.tool | head -40` prints a `claudna.export/1` envelope whose items carry `segment`; add `--include-skipped` and confirm a `skipped` item appears for a session whose segment was skipped (`headless`/`disabled` — this session, if `CLAUDNA_HARVEST` is unset). Paste both outputs, paths redacted to `~`, into the PR body.
- [ ] PR → review → merge. Release on `main` per `CONTRIBUTING.md:128-138`: `./scripts/release.sh minor` (both manifests, the CHANGELOG section, the commit and tag; it does not push), push the commit and the tag; `release-tag.yml` also tags on the version change. Then the P3 Claudlobby plan may bump its `claudna_version` and open.

## Test Plan

New: `TestExport::test_every_item_carries_the_segment_object`, the archived-item assertions in `TestReviewRound387`, `TestIncludeSkipped` (seven tests) and the extended `test_the_cli` in `tests/test_session_store_export.py`; `TestEntrypoint` (five tests) and the wrapper assertion in `tests/test_session_store_hook.py`; `TestFrozenLayer` (three) in `tests/test_session_store_activity.py`; `TestNestedChildren` gains three (Task 5). Existing: `tests/test_session_store.py::TestSchemas` now also walks `entrypoint.schema.json`. All of these run in both legs (`RUNTIME_TESTS`). Manual: Task 9's live check. Coverage: every branch of the new `take()` (done / skipped / retired-with-archive / retired-without / settled / unsettled) has a test naming it.

## Verification Checklist

- [ ] `python3 -m pytest tests/ -q` and `make test-runtime` under `/usr/bin/python3` (3.9): no failing name that is not in Task 0's before-leg files.
- [ ] `make check` green (lint at `line-length = 120`, manifests, changelog gate).
- [ ] `python3 -m pytest tests/test_session_store_export.py -q -k "unchanged"`: the unflagged envelope equals the flagged one minus status items; no default item has a `skipped` key.
- [ ] `~/.claudna/entrypoint.json` exists after a real SessionStart, is `0600`, validates against `schemas/entrypoint.schema.json`, and `python3 -S <its entrypoint> export --consumer test --json` exits 0 with a `claudna.export/1` envelope.
- [ ] Mutation probes, shown then restored: drop `"counts"` from `segment_record` → Task 1's test fails; return `"trivial"` for every reason → the `retired`/`gave_up` tests fail; add an `ACTIVITY` kind to `REGISTRY` → `TestFrozenLayer` fails; change the schema's `written_at` pattern → `test_every_timestamp_pattern_is_the_envelopes` fails.
- [ ] Spec §8 names every item key the code emits (`sid, seg, session, summary, segment, skipped`), and `SESSION_FIELDS` in the text equals `export.SESSION_FIELDS`.
- [ ] (Task 5) `scripts/session_canary.py report` on a run with a marked child shows every child event ignored by the guard.

## What NOT To Do

- Don't write `entrypoint.json` from `plugin-hooks/session-start.sh`: bots set `CLAUDNA_SESSION_BRIEFING=0` and it fires only for `startup|clear` (epic §11; F16 forge note).
- Don't compute the entrypoint from `${CLAUDE_PLUGIN_ROOT}` or by reading Claude Code's `installed_plugins.json`; `Path(__file__)` is the one source (`ENTRYPOINT`), shared with `spawn_worker`.
- Don't add `"skipped": null` to regular items, don't bump `claudna.export/1`, and don't change `next` or `ack` — the only default-output change is the additive `segment` key.
- Don't emit a status item for a `private` skip or a private session.
- Don't add `runtime` anywhere (P1 did), `--host` or `host_*.py` (P4), or a new `session_store` module (none is needed; the layering gate would also demand a rank).
- Don't put the `CLAUDE_CODE_CHILD_SESSION` check in `session-store.sh`: one guard, in Python, that the canary script can ask.
- Don't install 0.28 on a tmux-hosted bot before C10 is answered; if the variable leaks, `start-bot.sh` must `unset CLAUDE_CODE_CHILD_SESSION` first (a Claudlobby change) or every bot hook is ignored and no session ever closes.
- Don't use 3.10+ syntax in `lib/` (`match`, `X | Y` at runtime, `zip(strict=)`): the floor is 3.9; annotations are fine under `from __future__ import annotations`.
- Don't let `write_entrypoint` swallow its `OSError`: `run_hook` logs it; a silent failure would leave a stale record nobody notices.

## Context

area: session store / export door · effort: M · risk: Medium (two runtime-behaviour changes in the SessionStart hook — a new file write, and the guard under C10; the export changes are additive) · priority: P3 (plan 5 of §10.1) · related: Claudlobby#2145 (epic), Claudlobby#1961 (the digest's coverage concern F6 keeps), clauDNA#373 (nested children, M3), clauDNA#387 (export review S4).

## Canary answers this PR waits on

| Canary | Task | If it answers the other way |
|---|---|---|
| **C10** — does `CLAUDE_CODE_CHILD_SESSION` reach a bot whose tmux server was started from inside another Claude session? | 5 | **Leaks:** Claudlobby's `start-bot.sh` scrubs it before `exec $CLAUDE` (`:224-227,268`; it unsets nothing today) and that change is deployed to every bot **before** 0.28 is installed; Task 5 is held (or the release waits) until then. **Doesn't leak:** Task 5 ships as written. |
| **C11, hook-side reading** — do `PostToolUse`/`PostToolUseFailure` hook processes fired by an Agent subagent's tool calls carry `CLAUDE_CODE_CHILD_SESSION=1` with the parent's `session_id`? (C11 already runs hooks from a subagent; a 3-line scratch hook appending `env | grep ^CLAUDE_CODE_` to a file records it — no repo change.) | 5 | **They do:** the marker check in `inherited` is restricted to `event in ("SessionStart", "PreCompact", "SessionEnd")` so the parent keeps its subagents' `skill.invoked`/`tool.failed`; activity events keep the three fallback checks (which already catch a reused id by pid). **They don't:** as written. |
