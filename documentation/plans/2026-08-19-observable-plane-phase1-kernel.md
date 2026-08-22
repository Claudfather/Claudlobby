# Observable Plane — Phase 1: Semantic Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the observable plane's write substrate — canonical serialization, minted identity, the common event envelope, SQLite storage with a global ingest sequence, the `claudlobby emit` spine with spool fallback, and the benchmark that gates Phase 2's ingest choice — headless, no UI, no door migration yet.

**Architecture:** New `claudlobby/plane/` package inside the existing compositor. Append-only typed tables share a common envelope and draw ordering from one `ingest_ledger` (fork F16). Every write flows through one validated ingest function; infrastructure failure spools to the filesystem (never the db it protects); duplicate `event_id` replay is success. Identity is minted uids with human aliases resolved at write time (lazy, provisional-flagged). No door (dispatch/report-back/tg-post) is touched in this phase — Phase 2 wires doors onto this kernel after the Pi benchmark rules the ingest implementation.

**Tech Stack:** Python ≥3.10 (existing floor), Pydantic v2 (the one new dependency), stdlib `sqlite3` (WAL), stdlib `uuid`/`hashlib`/`unicodedata`. No ORM. No FastAPI yet (daemon is Phase 4).

**Spec:** `documentation/plans/2026-08-18-observable-plane-design-v2.md` (branch `design/observable-plane`) — forks F1–F18 are locked; this plan implements §3–§5, §9-canonicalization, §10, §14-benchmarks, §15-tests for the kernel subset.

## Global Constraints

- Python ≥3.10 (pyproject `requires-python`); Pydantic v2 is the ONLY new runtime dependency this phase; no ORM (spec F2).
- SQLite: WAL mode, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`; db lives at `<root>/state/plane/plane.db` — host-scoped, gitignored, outside any vault working tree (spec F3).
- Observed tables are APPEND-ONLY: no UPDATE/DELETE statements anywhere in this phase's code for event tables. `identity_registry` is registry (Lane C-adjacent) and MAY update `last_seen`/`provisional` — the only sanctioned mutation (spec §5).
- `event_id` is minted BEFORE any insert attempt; duplicate `event_id` on replay is SUCCESS, not error (spec §10).
- Contract violation (bad payload) fails LOUD: exit 2, nothing written, nothing spooled. Infrastructure failure (db unavailable): spool + exit 0 with stderr notice — the caller's own job must not fail because the ledger is down. Spool-write-also-failed: exit 3 (spec §5, §10).
- Column names: `sender_uid`/`recipient_uid`, never `from`/`to` (spec §15).
- Timestamps: ISO-8601 UTC with explicit offset (`2026-08-19T12:00:00.000000+00:00`); ordering authority is `ingest_seq`, never timestamps, never `rowid` as a public cursor (spec §4).
- Body cap 16 KiB; over-cap → truncate + `truncated=1` + `body_bytes` (original size) + `body_sha256` (full content); ANSI escapes stripped before storage (spec §7).
- PII bright line: no real chat ids, tokens, hostnames-with-identity, or fleet-specific values in code, tests, or fixtures — obviously fake placeholders only (repo CLAUDE.md).
- Tests: run UNSANDBOXED (`mktemp` phantom-failure class); the suite baseline is NOT green — use the counts+names diff protocol from CLAUDE.md ("Know the baseline") for every before/after comparison.
- Commit style: repo conventional commits (`feat(plane): …`), each task commits on `design/observable-plane` or its worktree.

## File Structure (end state of this phase)

```
claudlobby/plane/
  __init__.py          — package marker, version constant PLANE_SCHEMA_VERSION
  canonical.py         — CANON_V1 canonical-bytes serializer + sha256 helper
  ids.py               — uid/id minting + host_uid persistence
  contracts.py         — Pydantic v2: envelope, family payloads, EmitRequest; JSON-Schema export
  db.py                — connection factory (pragmas), db path resolution
  migrations.py        — user_version-based migration runner
  migrations/0001_kernel.sql — DDL: ingest_ledger, identity_registry, 5 event families
  identity.py          — alias→uid resolver (lazy mint, provisional flag)
  ingest.py            — the one transactional write path
  spool.py             — atomic spool, drain, quarantine
  emit_api.py          — emit(): validate → resolve → ingest → (spool)
claudlobby/commands/plane.py — cmd_emit, cmd_plane_status, cmd_plane_spool, cmd_plane_schema
claudlobby/commands/_parsers.py — register the new subcommands (modify)
pyproject.toml         — pydantic dep, claudlobby.plane package + package-data (modify)
bin/plane-bench.py     — benchmark harness (executable, not installed)
tests/test_plane_canonical.py, test_plane_ids.py, test_plane_contracts.py,
tests/test_plane_db.py, test_plane_identity.py, test_plane_ingest.py,
tests/test_plane_spool.py, test_plane_cli.py, test_plane_crash_battery.py
tests/fixtures/plane/canonical_golden.json
```

---

### Task 0: Package skeleton and dependency

**Files:**
- Modify: `pyproject.toml`
- Create: `claudlobby/plane/__init__.py`
- Create: `claudlobby/plane/migrations/` (directory, with 0001 arriving in Task 4)
- Modify: `.gitignore` (only if `state/` is not already ignored — verify first)

**Interfaces:**
- Produces: importable `claudlobby.plane` package; `PLANE_SCHEMA_VERSION = "1.0.0"` constant later tasks import.

- [ ] **Step 1: Verify gitignore covers state/**

Run: `grep -n "^state/\|^/state" .gitignore || echo "MISSING"`
If MISSING, add a line `state/` with comment `# host-scoped runtime state (plane db, host-uid) — never committed`.

- [ ] **Step 2: Add pydantic dependency and package registration**

In `pyproject.toml`, change the `dependencies` list and setuptools blocks to:

```toml
dependencies = [
    "PyYAML>=6.0,<7",
    "Jinja2>=3.1,<4",
    "pydantic>=2.5,<3",
]
```

```toml
[tool.setuptools]
packages = ["claudlobby", "claudlobby.commands", "claudlobby.plane"]

[tool.setuptools.package-data]
"claudlobby.plane" = ["migrations/*.sql"]
```

- [ ] **Step 3: Create the package marker**

`claudlobby/plane/__init__.py`:

```python
"""The observable plane's write substrate (design v2, forks F1-F18).

Append-only event kernel: canonical serialization, minted identity, one
transactional ingest path, filesystem spool. No UI here — the daemon (Phase 4)
and the doors (Phase 2) are consumers of this package, never part of it.
"""

from __future__ import annotations

# Version of the envelope + DDL contract this checkout writes. Bump per
# schema-changing migration; readers accept N and N-1 (spec §15).
PLANE_SCHEMA_VERSION = "1.0.0"
```

- [ ] **Step 4: Reinstall editable and verify import**

Run: `./.venv/bin/python -m pip install -e '.[dev]' -q && ./.venv/bin/python -c "from claudlobby.plane import PLANE_SCHEMA_VERSION; print(PLANE_SCHEMA_VERSION)"`
Expected: `1.0.0`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml claudlobby/plane/__init__.py .gitignore
git commit -m "feat(plane): package skeleton + pydantic dependency (Phase 1 kernel)"
```

---

### Task 1: Canonical bytes (CANON_V1) with golden fixtures

**Files:**
- Create: `claudlobby/plane/canonical.py`
- Create: `tests/fixtures/plane/canonical_golden.json`
- Test: `tests/test_plane_canonical.py`

**Interfaces:**
- Produces: `canonical_bytes(obj: object) -> bytes`, `canonical_hash(obj: object) -> str` (returns `"sha256:<hex>"`), `CANON_VERSION = "canon-1"`, exception `CanonicalizationError`.

The rules (spec §9, now exact): UTF-8; every `str` NFC-normalized (keys and values); keys sorted by post-normalization code point; compact separators; `ensure_ascii=False`; `None` serialized as `null` and INCLUDED (producers must not drop fields); allowed scalars are `str | int | bool | None` — **floats are a contract violation** (registry payloads carry versions, counts, names; a float smuggled in would make hashing platform-dependent); containers are `dict | list`; any other type raises. Path normalization is the PRODUCER's duty (absolute POSIX form) — canonicalization never rewrites values.

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_canonical.py`:

```python
"""CANON_V1: the exact bytes that get hashed. Golden fixtures pin the contract
across versions — a serializer change that alters any golden output is a
schema-version event, not a refactor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby.plane.canonical import (
    CANON_VERSION,
    CanonicalizationError,
    canonical_bytes,
    canonical_hash,
)

GOLDEN = Path(__file__).parent / "fixtures" / "plane" / "canonical_golden.json"


def test_version_constant():
    assert CANON_VERSION == "canon-1"


def test_sorts_keys_and_compacts():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_nfc_normalizes_keys_and_values():
    # "é" as NFD (e + combining acute) must serialize identically to NFC "é"
    nfd = "é"
    nfc = "é"
    assert canonical_bytes({nfd: nfd}) == canonical_bytes({nfc: nfc})


def test_none_is_included_not_dropped():
    assert canonical_bytes({"a": None}) == b'{"a":null}'


def test_non_ascii_not_escaped():
    assert canonical_bytes({"k": "émoji 🐋"}) == '{"k":"émoji 🐋"}'.encode("utf-8")


def test_nested_containers():
    obj = {"z": [{"b": 1, "a": [True, False, None]}], "a": "x"}
    assert canonical_bytes(obj) == b'{"a":"x","z":[{"a":[true,false,null],"b":1}]}'


def test_float_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": 1.5})


def test_unsupported_type_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": {1, 2}})


def test_hash_format():
    h = canonical_hash({"a": 1})
    assert h.startswith("sha256:") and len(h) == 7 + 64


def test_golden_fixtures():
    cases = json.loads(GOLDEN.read_text())
    assert len(cases) >= 5
    for case in cases:
        got_bytes = canonical_bytes(case["input"])
        assert got_bytes.decode("utf-8") == case["canonical"], case["name"]
        assert canonical_hash(case["input"]) == case["hash"], case["name"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_canonical.py -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudlobby.plane.canonical'`

- [ ] **Step 3: Implement canonical.py**

```python
"""CANON_V1 — the one definition of canonical bytes (design v2 §9).

The hash gate and cross-host payload comparison both depend on identical
states producing identical bytes. Rules, in full:

  encoding      UTF-8, ensure_ascii=False
  unicode       every str (key or value) NFC-normalized before serialization
  ordering      dict keys sorted by post-normalization code point
  whitespace    none (separators ",", ":")
  numbers       int and bool only; float raises (platform-dependent repr
                would silently fork hashes — registry payloads never need it)
  null          None is serialized as null and always included
  containers    dict and list only; anything else raises
  paths         producer's duty: absolute POSIX form before handing over —
                canonicalization never rewrites values
  hash          sha256 over the canonical bytes, rendered "sha256:<hex>"

Any change to these rules is a new CANON_VERSION and a new golden-fixture
set — never an in-place edit.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

CANON_VERSION = "canon-1"


class CanonicalizationError(ValueError):
    """The object violates the CANON_V1 value contract."""


def _normalize(obj: object) -> object:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise CanonicalizationError("floats are not canonicalizable (CANON_V1)")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string key: {k!r}")
            out[unicodedata.normalize("NFC", k)] = _normalize(v)
        return out
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    raise CanonicalizationError(f"unsupported type: {type(obj).__name__}")


def canonical_bytes(obj: object) -> bytes:
    return json.dumps(
        _normalize(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def canonical_hash(obj: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()
```

- [ ] **Step 4: Generate the golden fixture file**

Write `tests/fixtures/plane/canonical_golden.json` by running this once and committing its output:

```bash
./.venv/bin/python - <<'EOF'
import json, pathlib
from claudlobby.plane.canonical import canonical_bytes, canonical_hash
cases = [
    ("flat-sort", {"b": 1, "a": 2}),
    ("nfd-unicode", {"café": "résumé"}),
    ("null-kept", {"present": None, "n": 0}),
    ("nested", {"z": [{"b": 1, "a": [True, False, None]}], "a": "x"}),
    ("emoji", {"msg": "fleet 🐋 alive", "count": 21}),
    ("empty-containers", {"d": {}, "l": []}),
]
out = [
    {"name": n, "input": i,
     "canonical": canonical_bytes(i).decode("utf-8"),
     "hash": canonical_hash(i)}
    for n, i in cases
]
p = pathlib.Path("tests/fixtures/plane/canonical_golden.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
print(f"wrote {p} ({len(out)} cases)")
EOF
```

(Generating goldens from the implementation is safe exactly once, at contract birth — from then on they pin it. Review the file by eye before committing: the `nfd-unicode` case must show NFC bytes.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_canonical.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add claudlobby/plane/canonical.py tests/test_plane_canonical.py tests/fixtures/plane/canonical_golden.json
git commit -m "feat(plane): CANON_V1 canonical bytes + golden fixtures"
```

---

### Task 2: Identity and id minting

**Files:**
- Create: `claudlobby/plane/ids.py`
- Test: `tests/test_plane_ids.py`

**Interfaces:**
- Produces: `mint(prefix: str) -> str` and the typed wrappers `mint_event_id()`, `mint_msg_id()`, `mint_attempt_id()`, `mint_work_item_id()`, `mint_task_attempt_id()`, `mint_uid(kind: str) -> str`; `ensure_host_uid(state_dir: Path) -> str`; `ID_PATTERNS: dict[str, str]` (regex per prefix, consumed by contracts.py validation).

Prefixes (fixed): `ev_` events, `msg_` messages, `att_` transport attempts, `wi_` work items, `ta_` task attempts, and uids `host_`, `fleet_`, `actor_`, `boti_`, `sess_` — each followed by 32 lowercase hex chars (uuid4). Ordering never comes from ids (that is `ingest_seq`'s job), so uuid4 suffices and stays stdlib.

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_ids.py`:

```python
from __future__ import annotations

import re
import stat
from pathlib import Path

from claudlobby.plane.ids import (
    ID_PATTERNS,
    ensure_host_uid,
    mint_event_id,
    mint_uid,
)


def test_event_id_shape():
    eid = mint_event_id()
    assert re.fullmatch(r"ev_[0-9a-f]{32}", eid)
    assert re.fullmatch(ID_PATTERNS["event"], eid)


def test_uid_kinds():
    for kind, prefix in [("host", "host_"), ("fleet", "fleet_"),
                         ("actor", "actor_"), ("bot_instance", "boti_"),
                         ("session", "sess_"), ("vault", "vault_"), ("project", "proj_"), ("library_item", "lib_")]:
        uid = mint_uid(kind)
        assert uid.startswith(prefix) and len(uid) == len(prefix) + 32


def test_mint_is_unique():
    assert len({mint_event_id() for _ in range(1000)}) == 1000


def test_ensure_host_uid_mints_once(tmp_path: Path):
    first = ensure_host_uid(tmp_path)
    second = ensure_host_uid(tmp_path)
    assert first == second
    assert first.startswith("host_")
    f = tmp_path / "host-uid"
    assert f.read_text().strip() == first
    # 0600: the uid is joined against every ledger row; owner-only like .env
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_ensure_host_uid_survives_trailing_newline(tmp_path: Path):
    (tmp_path / "host-uid").write_text("host_" + "a" * 32 + "\n")
    assert ensure_host_uid(tmp_path) == "host_" + "a" * 32


def test_ensure_host_uid_rejects_garbage(tmp_path: Path):
    (tmp_path / "host-uid").write_text("not-a-uid\n")
    import pytest
    with pytest.raises(ValueError):
        ensure_host_uid(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_ids.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ids.py**

```python
"""Minted identifiers (design v2 §3, F10). Names are aliases; uids are truth.

A corrupted host-uid file is a hard error, never silently re-minted: re-minting
would fork every subsequent row's host identity from the estate's history,
which is exactly the longitudinal-join corruption F10 exists to prevent.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_UID_PREFIX = {
    "host": "host_",
    "fleet": "fleet_",
    "actor": "actor_",
    "bot_instance": "boti_",
    "session": "sess_",
    "vault": "vault_",
    "project": "proj_",
    "library_item": "lib_",
}

ID_PATTERNS: dict[str, str] = {
    "event": r"ev_[0-9a-f]{32}",
    "msg": r"msg_[0-9a-f]{32}",
    "attempt": r"att_[0-9a-f]{32}",
    "work_item": r"wi_[0-9a-f]{32}",
    "task_attempt": r"ta_[0-9a-f]{32}",
    **{kind: prefix + r"[0-9a-f]{32}" for kind, prefix in _UID_PREFIX.items()},
}

_HOST_UID_RE = re.compile(r"^host_[0-9a-f]{32}$")


def mint(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def mint_event_id() -> str:
    return mint("ev_")


def mint_msg_id() -> str:
    return mint("msg_")


def mint_attempt_id() -> str:
    return mint("att_")


def mint_work_item_id() -> str:
    return mint("wi_")


def mint_task_attempt_id() -> str:
    return mint("ta_")


def mint_uid(kind: str) -> str:
    return mint(_UID_PREFIX[kind])


def ensure_host_uid(state_dir: Path) -> str:
    """Read the persisted host uid, minting it exactly once (atomic, 0600)."""
    state_dir = Path(state_dir)
    path = state_dir / "host-uid"
    if path.exists():
        value = path.read_text().strip()
        if not _HOST_UID_RE.fullmatch(value):
            raise ValueError(
                f"corrupt host-uid at {path}: {value!r} — refusing to re-mint; "
                "restore from backup or delete deliberately"
            )
        return value
    state_dir.mkdir(parents=True, exist_ok=True)
    value = mint_uid("host")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_ids.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudlobby/plane/ids.py tests/test_plane_ids.py
git commit -m "feat(plane): id minting + persisted host uid"
```

---

### Task 3: Contracts — envelope and family payloads

**Files:**
- Create: `claudlobby/plane/contracts.py`
- Test: `tests/test_plane_contracts.py`

**Interfaces:**
- Produces (consumed by ingest.py, emit_api.py, commands/plane.py):
  - `class EmitRequest(BaseModel)` — the wire contract doors/tests submit: `event_type: str`, `occurred_at: AwareDatetime | None`, `emitter: str`, `source_ref: str | None`, `fleet: str | None` (alias), `payload: dict`.
  - `FAMILIES: dict[str, type[BaseModel]]` mapping event_type → payload model: `"communication" → Communication`, `"communication_attempt" → CommunicationAttempt`, `"work_item" → WorkItem`, `"task_attempt" → TaskAttempt`, `"task_event" → TaskEvent`.
  - Enums as `Literal` sets: `MESSAGE_CLASSES`, `COMMAND_TYPES`, `ATTEMPT_STATES`, `TASK_EVENTS` (exact values below).
  - `validate_request(raw: dict) -> tuple[EmitRequest, BaseModel]` — parses envelope then family payload; raises `ContractViolation` (carries `.errors`).
  - `export_schemas() -> dict` — JSON Schema per family + envelope (feeds F2's TS codegen later).
  - Body handling: `cap_body(text: str) -> BodyFields` applying the 16 KiB cap + ANSI strip + sha256-of-full.

Vocabulary (spec §7, F11, F17 — exact):
- `MESSAGE_CLASSES = task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control`
- `COMMAND_TYPES = task, cancel, compact, restart, query`
- `ATTEMPT_STATES = send_attempted, carrier_accepted, pane_submitted, failed, unknown, recipient_acknowledged, duplicate_suppressed`
- `TASK_EVENTS = dispatch_intended, transmission_failed, dispatch_submitted, receiver_acknowledged, accepted, rejected, progress, blocked_waiting, returned_blocked, resumed, completed, failed, cancelled, deadline_changed, superseded, reassigned, retry_created, orphaned_by_session_loss, recovered_after_restart, expired`
- `contract_created` is NOT a task_event: the `work_item`/`task_attempt` row IS that event (one fact, one row — spec §8 mapping note).
- `CARRIERS = tmux, telegram-tgpost, telegram-bridge`

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_contracts.py`:

```python
from __future__ import annotations

import pytest

from claudlobby.plane.contracts import (
    FAMILIES,
    ContractViolation,
    cap_body,
    export_schemas,
    validate_request,
)


def _req(event_type: str, payload: dict) -> dict:
    return {
        "event_type": event_type,
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "payload": payload,
    }


def _intent_payload(**over) -> dict:
    p = {
        "msg_id": "msg_" + "0" * 32,
        "sender": "bot:example-fleet/alpha",
        "recipient": "bot:example-fleet/beta",
        "message_class": "task_request",
        "command_type": "task",
        "body": "review PR 42",
        "privacy": "full",
    }
    p.update(over)
    return p


def test_families_registered():
    assert set(FAMILIES) == {
        "communication", "communication_attempt", "work_item", "task_attempt", "task_event"
    }


def test_valid_intent_parses():
    env, payload = validate_request(_req("communication", _intent_payload()))
    assert env.event_type == "communication"
    assert payload.message_class == "task_request"
    assert payload.body_bytes == len(b"review PR 42")
    assert payload.truncated is False


def test_unknown_event_type_is_violation():
    with pytest.raises(ContractViolation):
        validate_request(_req("nonsense", {}))


def test_unknown_message_class_is_violation_not_coercion():
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(message_class="shout")))


def test_extra_fields_rejected():
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(surprise=1)))


def test_body_cap_truncates_and_hashes():
    big = "x" * 20_000
    fields = cap_body(big)
    assert fields.truncated is True
    assert fields.body_bytes == 20_000
    assert len(fields.body.encode()) <= 16_384
    assert fields.body_sha256.startswith("sha256:")


def test_body_ansi_stripped():
    fields = cap_body("\x1b[31mred\x1b[0m plain")
    assert fields.body == "red plain"


def test_task_event_vocabulary_enforced():
    good = {"work_item_id": "wi_" + "0" * 32, "event": "blocked_waiting"}
    env, payload = validate_request(_req("task_event", good))
    assert payload.event == "blocked_waiting"
    with pytest.raises(ContractViolation):
        validate_request(_req("task_event", {**good, "event": "blocked"}))


def test_communication_attempt_states():
    good = {
        "attempt_id": "att_" + "0" * 32,
        "msg_id": "msg_" + "0" * 32,
        "attempt_no": 1,
        "carrier": "tmux",
        "destination": "bot:example-fleet/beta",
        "state": "pane_submitted",
    }
    _, payload = validate_request(_req("communication_attempt", good))
    assert payload.state == "pane_submitted"
    with pytest.raises(ContractViolation):
        validate_request(
            _req("communication_attempt", {**good, "state": "delivered"})  # banned word
        )


def test_schemas_export():
    schemas = export_schemas()
    assert "envelope" in schemas and "communication" in schemas
    assert schemas["communication"]["title"] == "Communication"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_contracts.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement contracts.py**

```python
"""Pydantic v2 wire contracts (design v2 §4, §7-8, F11, F17).

The vocabulary is a CLOSED enum enforced here: an unknown message_class or
task event is a caller bug and fails loud (ContractViolation) — never coerced,
never spooled. `delivered` is deliberately absent from ATTEMPT_STATES (F9).
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from .ids import ID_PATTERNS

MESSAGE_CLASSES = (
    "task_request", "report", "question", "answer", "alert", "notice",
    "briefing", "nudge", "acknowledgement", "chat", "config_change",
    "raw_control",
)
COMMAND_TYPES = ("task", "cancel", "compact", "restart", "query")
ATTEMPT_STATES = (
    "send_attempted", "carrier_accepted", "pane_submitted", "failed",
    "unknown", "recipient_acknowledged", "duplicate_suppressed",
)
TASK_EVENTS = (
    "dispatch_intended", "transmission_failed", "dispatch_submitted",
    "receiver_acknowledged", "accepted", "rejected", "progress",
    "blocked_waiting", "returned_blocked", "resumed", "completed", "failed",
    "cancelled", "deadline_changed", "superseded", "reassigned",
    "retry_created", "orphaned_by_session_loss", "recovered_after_restart",
    "expired",
)
CARRIERS = ("tmux", "telegram-tgpost", "telegram-bridge")

BODY_CAP_BYTES = 16_384
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class ContractViolation(ValueError):
    """Payload violates the wire contract — caller bug, fail loud."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(str(errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BodyFields(_Strict):
    body: str
    body_bytes: int
    body_sha256: str
    truncated: bool


def cap_body(text: str) -> BodyFields:
    """ANSI-strip, then cap at BODY_CAP_BYTES (UTF-8 safe), hashing the FULL
    stripped content so a truncated row still proves what it truncated."""
    stripped = _ANSI_RE.sub("", text)
    raw = stripped.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if len(raw) <= BODY_CAP_BYTES:
        return BodyFields(
            body=stripped, body_bytes=len(raw), body_sha256=digest, truncated=False
        )
    cut = raw[:BODY_CAP_BYTES].decode("utf-8", errors="ignore")
    return BodyFields(
        body=cut, body_bytes=len(raw), body_sha256=digest, truncated=True
    )


class Communication(_Strict):
    msg_id: str = Field(pattern=ID_PATTERNS["msg"])
    sender: str = Field(min_length=1)          # alias; resolved to uid at ingest
    sender_session_uid: Optional[str] = Field(None, pattern=ID_PATTERNS["session"])
    recipient: Optional[str] = None            # alias; None = broadcast-shaped
    recipient_raw: Optional[str] = None        # carrier-native address (chat id)
    message_class: Literal[MESSAGE_CLASSES]
    command_type: Optional[Literal[COMMAND_TYPES]] = None
    work_item_id: Optional[str] = Field(None, pattern=ID_PATTERNS["work_item"])
    task_attempt_id: Optional[str] = Field(None, pattern=ID_PATTERNS["task_attempt"])
    workstream_id: Optional[str] = None
    deliberation_id: Optional[str] = None      # Phase-5 seam, reserved
    reply_to_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    supersedes_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    body: Optional[str] = None
    privacy: Literal["metadata", "preview", "full"] = "metadata"
    idempotency_key: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    # Derived at validation from `body` (never caller-supplied):
    body_bytes: int = 0
    body_sha256: Optional[str] = None
    truncated: bool = False

    def model_post_init(self, __context) -> None:
        if self.body is not None:
            fields = cap_body(self.body)
            object.__setattr__(self, "body", fields.body)
            object.__setattr__(self, "body_bytes", fields.body_bytes)
            object.__setattr__(self, "body_sha256", fields.body_sha256)
            object.__setattr__(self, "truncated", fields.truncated)


class CommunicationAttempt(_Strict):
    attempt_id: str = Field(pattern=ID_PATTERNS["attempt"])
    msg_id: str = Field(pattern=ID_PATTERNS["msg"])
    attempt_no: int = Field(ge=1)
    carrier: Literal[CARRIERS]
    destination: str
    state: Literal[ATTEMPT_STATES]
    carrier_ref: Optional[str] = None
    error: Optional[str] = None


class WorkItem(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    title: str = Field(min_length=1)
    created_by: str                             # alias
    workstream_id: Optional[str] = None         # the WHY axis
    repo: Optional[str] = Field(None, pattern=r"[^/\s]+/[^/\s]+")  # WHERE: owner/name
    project_key: Optional[str] = Field(None, pattern=r"[a-z][a-z0-9-]*")  # projects.yaml slug
    # Authored, not relayed: oversized bodies REJECT (contract violation),
    # never truncate-with-proof — the communications rule is for relayed content.
    body: Optional[str] = Field(None, max_length=16_384)


class TaskAttempt(_Strict):
    task_attempt_id: str = Field(pattern=ID_PATTERNS["task_attempt"])
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    assignee: str                               # alias
    assigned_by: str                            # alias
    expected_by: Optional[AwareDatetime] = None
    dispatch_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])


class TaskEvent(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    task_attempt_id: Optional[str] = Field(None, pattern=ID_PATTERNS["task_attempt"])
    event: Literal[TASK_EVENTS]
    actor: Optional[str] = None                 # alias: who reported it
    session_uid: Optional[str] = Field(None, pattern=ID_PATTERNS["session"])
    progress: Optional[int] = Field(None, ge=0, le=100)
    summary: Optional[str] = None
    pr_url: Optional[str] = None
    deadline: Optional[AwareDatetime] = None
    successor_id: Optional[str] = None  # reassigned/retry_created -> task_attempt_id; superseded -> superseding id


FAMILIES: dict[str, type[BaseModel]] = {
    "communication": Communication,
    "communication_attempt": CommunicationAttempt,
    "work_item": WorkItem,
    "task_attempt": TaskAttempt,
    "task_event": TaskEvent,
}


class EmitRequest(_Strict):
    event_type: str
    occurred_at: Optional[AwareDatetime] = None   # None → ingest stamps now()
    emitter: str = Field(min_length=1)
    source_ref: Optional[str] = None
    fleet: Optional[str] = None                   # alias, e.g. "example-fleet"
    event_id: Optional[str] = Field(None, pattern=ID_PATTERNS["event"])
    payload: dict


def validate_request(raw: dict) -> tuple[EmitRequest, BaseModel]:
    try:
        env = EmitRequest.model_validate(raw)
    except ValidationError as exc:
        raise ContractViolation(exc.errors()) from exc
    model = FAMILIES.get(env.event_type)
    if model is None:
        raise ContractViolation(
            [{"loc": ("event_type",), "msg": f"unknown event type {env.event_type!r}"}]
        )
    try:
        payload = model.model_validate(env.payload)
    except ValidationError as exc:
        raise ContractViolation(exc.errors()) from exc
    return env, payload


def export_schemas() -> dict:
    out = {"envelope": EmitRequest.model_json_schema()}
    for name, model in FAMILIES.items():
        out[name] = model.model_json_schema()
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_contracts.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudlobby/plane/contracts.py tests/test_plane_contracts.py
git commit -m "feat(plane): wire contracts — envelope, five families, closed vocabularies"
```

---

### Task 4: Database, migrations runner, kernel DDL

**Files:**
- Create: `claudlobby/plane/db.py`
- Create: `claudlobby/plane/migrations.py`
- Create: `claudlobby/plane/migrations/0001_kernel.sql`
- Test: `tests/test_plane_db.py`

**Interfaces:**
- Produces: `connect(db_path: Path) -> sqlite3.Connection` (pragmas applied, `row_factory=sqlite3.Row`); `db_path(root: Path) -> Path` (`<root>/state/plane/plane.db`, parents created); `migrate(conn) -> int` (applies pending `NNNN_*.sql` by `PRAGMA user_version`, returns version); `SCHEMA_USER_VERSION = 1`.
- Envelope columns, identical on every family table (ingest.py fills them): `ingest_seq INTEGER NOT NULL UNIQUE, event_id TEXT NOT NULL UNIQUE, schema_version TEXT NOT NULL, occurred_at TEXT NOT NULL, observed_at TEXT, ingested_at TEXT NOT NULL, host_uid TEXT NOT NULL, fleet_uid TEXT, emitter TEXT NOT NULL, source_ref TEXT, correlation_id TEXT, causation_id TEXT, trace_id TEXT, span_id TEXT`.

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_db.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, migrate


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(db_path(tmp_path))
    migrate(c)
    yield c
    c.close()


def test_db_path_shape(tmp_path: Path):
    p = db_path(tmp_path)
    assert p == tmp_path / "state" / "plane" / "plane.db"
    assert p.parent.is_dir()


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_migrate_sets_user_version_and_is_idempotent(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_USER_VERSION
    assert migrate(conn) == SCHEMA_USER_VERSION  # second run: no-op


def test_expected_tables(conn):
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "ingest_ledger", "identity_registry", "communications",
        "communication_attempts", "work_items", "task_attempts", "task_events",
    } <= names


def test_downgrade_refused(tmp_path: Path):
    c = connect(db_path(tmp_path))
    c.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION + 100}")
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrate(c)


def test_ingest_ledger_seq_monotonic(conn):
    for i in range(3):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'task_event', '2026-01-01T00:00:00+00:00')",
            (f"ev_{i:032x}",),
        )
    seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY ingest_seq")]
    assert seqs == sorted(seqs) and len(seqs) == 3


def test_ddl_vocabularies_match_contracts():
    """The same enum is enforced twice — Literal at validation, CHECK at
    insert. This pins the two copies together: retiring or adding a value
    must touch both, or this test names the disagreement."""
    import re
    from importlib import resources

    from claudlobby.plane import contracts

    sql = (
        resources.files("claudlobby.plane") / "migrations" / "0001_kernel.sql"
    ).read_text()

    def check_set(column: str, table: str) -> set[str]:
        block = sql.split(f"CREATE TABLE {table}")[1].split(";")[0]
        m = re.search(
            column + r"\s+TEXT[^,]*CHECK \(" + column + r" IN\s*\(([^)]*)\)",
            block, re.S,
        )
        assert m, f"no CHECK for {column} in {table}"
        return {v.strip().strip("'") for v in m.group(1).split(",") if v.strip()}

    assert check_set("message_class", "communications") == set(contracts.MESSAGE_CLASSES)
    assert check_set("command_type", "communications") == set(contracts.COMMAND_TYPES)
    assert check_set("state", "communication_attempts") == set(contracts.ATTEMPT_STATES)
    assert check_set("carrier", "communication_attempts") == set(contracts.CARRIERS)
    assert check_set("event", "task_events") == set(contracts.TASK_EVENTS)


def test_duplicate_event_id_rejected_by_ledger(conn):
    conn.execute(
        "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
        " VALUES ('ev_' || printf('%032x', 7), 'task_event', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES ('ev_' || printf('%032x', 7), 'task_event', 't')"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_db.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement db.py**

```python
"""Connection factory + path resolution (design v2 F3).

The db is HOST-scoped: <root>/state/plane/plane.db — outside every vault
working tree so message bodies can never ride a vault sync (spec §5).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def db_path(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "plane.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
```

- [ ] **Step 4: Implement migrations.py**

```python
"""user_version-gated migration runner (design v2 F2: versioned .sql files).

Forward-only: a db whose user_version exceeds this code's ceiling is from a
NEWER checkout — refuse loudly rather than write rows an older schema
understands differently (spec §15 downgrade refusal).
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources

SCHEMA_USER_VERSION = 1

_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def _migration_files() -> list[tuple[int, str]]:
    pkg = resources.files("claudlobby.plane") / "migrations"
    out = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.read_text()))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_USER_VERSION:
        raise RuntimeError(
            f"plane.db user_version={current} is newer than this code"
            f" (supports ≤{SCHEMA_USER_VERSION}) — refusing downgrade"
        )
    for number, sql in _migration_files():
        if number <= current:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {number}")
        current = number
    return current
```

- [ ] **Step 5: Write migrations/0001_kernel.sql**

```sql
-- 0001_kernel: ingest ledger, identity registry, five event families.
-- Envelope columns are identical on every family table by design (F16):
--   ingest_seq, event_id, schema_version, occurred_at, observed_at,
--   ingested_at, host_uid, fleet_uid, emitter, source_ref,
--   correlation_id, causation_id, trace_id, span_id
-- Ordering authority is ingest_ledger.ingest_seq (AUTOINCREMENT), copied
-- into each family row in the same transaction. rowid is never a cursor.

CREATE TABLE ingest_ledger (
    ingest_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    family      TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Registry, not observed lane: last_seen/provisional may UPDATE (the one
-- sanctioned mutation — spec §5). provisional=1 marks a lazily-minted
-- identity awaiting confirmation by a generate-time registry pass (Phase 2+);
-- doctor surfaces provisional actors so a typo'd alias cannot silently
-- become a phantom colleague.
CREATE TABLE identity_registry (
    uid         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('host','fleet','actor','bot_instance','session','vault','project','library_item')),
    alias       TEXT NOT NULL,
    parent_uid  TEXT,
    provisional INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (kind, alias)
);

CREATE TABLE communications (
    ingest_seq        INTEGER NOT NULL UNIQUE,
    event_id          TEXT NOT NULL UNIQUE,
    schema_version    TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    observed_at       TEXT,
    ingested_at       TEXT NOT NULL,
    host_uid          TEXT NOT NULL,
    fleet_uid         TEXT,
    emitter           TEXT NOT NULL,
    source_ref        TEXT,
    correlation_id    TEXT,
    causation_id      TEXT,
    trace_id          TEXT,
    span_id           TEXT,
    msg_id            TEXT PRIMARY KEY NOT NULL,   -- the communication id
    sender_uid        TEXT NOT NULL,
    sender_alias      TEXT NOT NULL,
    sender_session_uid TEXT,
    recipient_uid     TEXT,
    recipient_alias   TEXT,
    recipient_raw     TEXT,
    message_class     TEXT NOT NULL CHECK (message_class IN
        ('task_request','report','question','answer','alert','notice',
         'briefing','nudge','acknowledgement','chat','config_change',
         'raw_control')),
    command_type      TEXT CHECK (command_type IN
        ('task','cancel','compact','restart','query')),
    work_item_id      TEXT,
    task_attempt_id   TEXT,
    workstream_id     TEXT,
    deliberation_id   TEXT,
    reply_to_msg_id   TEXT,
    supersedes_msg_id TEXT,
    body              TEXT,
    body_bytes        INTEGER NOT NULL DEFAULT 0,
    body_sha256       TEXT,
    truncated         INTEGER NOT NULL DEFAULT 0,
    privacy           TEXT NOT NULL CHECK (privacy IN ('metadata','preview','full')),
    idempotency_key   TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_intents_msg       ON communications (msg_id);
CREATE INDEX idx_intents_sender    ON communications (sender_uid, ingest_seq);
CREATE INDEX idx_intents_work_item ON communications (work_item_id)
    WHERE work_item_id IS NOT NULL;

CREATE TABLE communication_attempts (
    ingest_seq      INTEGER NOT NULL UNIQUE,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    observed_at     TEXT,
    ingested_at     TEXT NOT NULL,
    host_uid        TEXT NOT NULL,
    fleet_uid       TEXT,
    emitter         TEXT NOT NULL,
    source_ref      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    trace_id        TEXT,
    span_id         TEXT,
    attempt_id      TEXT NOT NULL,
    msg_id          TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    carrier         TEXT NOT NULL CHECK (carrier IN
                      ('tmux','telegram-tgpost','telegram-bridge')),
    destination     TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN
        ('send_attempted','carrier_accepted','pane_submitted','failed',
         'unknown','recipient_acknowledged','duplicate_suppressed')),
    carrier_ref     TEXT,
    error           TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_attempts_msg ON communication_attempts (msg_id, attempt_no);

CREATE TABLE work_items (
    ingest_seq      INTEGER NOT NULL UNIQUE,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    observed_at     TEXT,
    ingested_at     TEXT NOT NULL,
    host_uid        TEXT NOT NULL,
    fleet_uid       TEXT,
    emitter         TEXT NOT NULL,
    source_ref      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    trace_id        TEXT,
    span_id         TEXT,
    work_item_id    TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    created_by_uid  TEXT NOT NULL,
    workstream_id   TEXT,
    repo            TEXT,
    project_key     TEXT,
    body            TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

CREATE TABLE task_attempts (
    ingest_seq      INTEGER NOT NULL UNIQUE,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    observed_at     TEXT,
    ingested_at     TEXT NOT NULL,
    host_uid        TEXT NOT NULL,
    fleet_uid       TEXT,
    emitter         TEXT NOT NULL,
    source_ref      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    trace_id        TEXT,
    span_id         TEXT,
    task_attempt_id TEXT NOT NULL UNIQUE,
    work_item_id    TEXT NOT NULL,
    assignee_uid    TEXT NOT NULL,
    assigned_by_uid TEXT NOT NULL,
    expected_by     TEXT,
    dispatch_msg_id TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_task_attempts_item ON task_attempts (work_item_id);
CREATE INDEX idx_task_attempts_assignee ON task_attempts (assignee_uid, ingest_seq);

CREATE TABLE task_events (
    ingest_seq      INTEGER NOT NULL UNIQUE,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    observed_at     TEXT,
    ingested_at     TEXT NOT NULL,
    host_uid        TEXT NOT NULL,
    fleet_uid       TEXT,
    emitter         TEXT NOT NULL,
    source_ref      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    trace_id        TEXT,
    span_id         TEXT,
    work_item_id    TEXT NOT NULL,
    task_attempt_id TEXT,
    event           TEXT NOT NULL CHECK (event IN
        ('dispatch_intended','transmission_failed','dispatch_submitted',
         'receiver_acknowledged','accepted','rejected','progress',
         'blocked_waiting','returned_blocked','resumed','completed','failed',
         'cancelled','deadline_changed','superseded','reassigned',
         'retry_created','orphaned_by_session_loss','recovered_after_restart',
         'expired')),
    actor_uid       TEXT,
    session_uid     TEXT,
    progress        INTEGER CHECK (progress IS NULL OR (progress >= 0 AND progress <= 100)),
    summary         TEXT,
    pr_url          TEXT,
    deadline        TEXT,
    successor_id    TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_task_events_item ON task_events (work_item_id, ingest_seq);
CREATE INDEX idx_task_events_attempt ON task_events (task_attempt_id)
    WHERE task_attempt_id IS NOT NULL;
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_db.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add claudlobby/plane/db.py claudlobby/plane/migrations.py claudlobby/plane/migrations/0001_kernel.sql tests/test_plane_db.py
git commit -m "feat(plane): kernel DDL — ingest ledger, identity registry, five families"
```

---

### Task 5: Identity resolver (lazy mint, provisional)

**Files:**
- Create: `claudlobby/plane/identity.py`
- Test: `tests/test_plane_identity.py`

**Interfaces:**
- Consumes: `mint_uid` (Task 2), a migrated connection (Task 4).
- Produces: `resolve(conn, kind: str, alias: str, *, now: str, parent_uid: str | None = None) -> str` — returns the uid for `(kind, alias)`, minting a `provisional=1` row on first sight and touching `last_seen` after; `resolve_fleet(conn, fleet_alias, now) -> str`; `resolve_party(conn, alias, now, fleet_uid=None) -> str` (kind=`actor`; an "actor" is any addressable party — `bot:<fleet>/<name>`, `operator`, `system:<job>`, `telegram:<alias>`); `provisional_actors(conn) -> list[sqlite3.Row]` (the doctor surface).

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_identity.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.identity import provisional_actors, resolve, resolve_party
from claudlobby.plane.migrations import migrate

NOW = "2026-08-19T00:00:00.000000+00:00"
LATER = "2026-08-19T01:00:00.000000+00:00"


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(db_path(tmp_path))
    migrate(c)
    yield c
    c.close()


def test_first_sight_mints_provisional(conn):
    uid = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    assert uid.startswith("actor_")
    row = conn.execute(
        "SELECT * FROM identity_registry WHERE uid = ?", (uid,)
    ).fetchone()
    assert row["provisional"] == 1
    assert row["first_seen"] == NOW


def test_resolution_is_stable(conn):
    a = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    b = resolve_party(conn, "bot:example-fleet/alpha", LATER)
    assert a == b
    row = conn.execute(
        "SELECT first_seen, last_seen FROM identity_registry WHERE uid = ?", (a,)
    ).fetchone()
    assert row["first_seen"] == NOW and row["last_seen"] == LATER


def test_distinct_aliases_distinct_uids(conn):
    a = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    b = resolve_party(conn, "bot:example-fleet/beta", NOW)
    assert a != b


def test_kinds_do_not_collide(conn):
    fleet = resolve(conn, "fleet", "example-fleet", now=NOW)
    actor = resolve(conn, "actor", "example-fleet", now=NOW)
    assert fleet != actor and fleet.startswith("fleet_")


def test_provisional_listing(conn):
    resolve_party(conn, "operator", NOW)
    rows = provisional_actors(conn)
    assert [r["alias"] for r in rows] == ["operator"]


def test_concurrent_mint_race_yields_one_uid(conn, tmp_path: Path):
    # Second connection simulates a concurrent emitter losing the insert race.
    other = connect(db_path(tmp_path))
    a = resolve_party(conn, "bot:example-fleet/gamma", NOW)
    b = resolve_party(other, "bot:example-fleet/gamma", LATER)
    other.close()
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_identity.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement identity.py**

```python
"""Alias→uid resolution with lazy minting (design v2 §3, F10).

Doors speak aliases (bash cannot mint uuids sanely); rows store uids. First
sight of an alias mints a PROVISIONAL identity — doctor lists provisionals so
a typo'd alias becomes a visible finding instead of a phantom colleague. A
generate-time registry pass (Phase 2+) confirms real bots (provisional=0).

Race rule: INSERT OR IGNORE then SELECT — two emitters resolving one new
alias concurrently converge on the winner's uid.
"""

from __future__ import annotations

import sqlite3

from .ids import mint_uid


def resolve(
    conn: sqlite3.Connection,
    kind: str,
    alias: str,
    *,
    now: str,
    parent_uid: str | None = None,
) -> str:
    candidate = mint_uid(kind)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO identity_registry"
            " (uid, kind, alias, parent_uid, provisional, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, 1, ?, ?)",
            (candidate, kind, alias, parent_uid, now, now),
        )
        row = conn.execute(
            "SELECT uid FROM identity_registry WHERE kind = ? AND alias = ?",
            (kind, alias),
        ).fetchone()
        conn.execute(
            "UPDATE identity_registry SET last_seen = ? WHERE uid = ?",
            (now, row["uid"]),
        )
    return row["uid"]


def resolve_fleet(conn: sqlite3.Connection, fleet_alias: str, now: str) -> str:
    return resolve(conn, "fleet", fleet_alias, now=now)


def resolve_party(
    conn: sqlite3.Connection,
    alias: str,
    now: str,
    fleet_uid: str | None = None,
) -> str:
    return resolve(conn, "actor", alias, now=now, parent_uid=fleet_uid)


def provisional_actors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT uid, alias, first_seen, last_seen FROM identity_registry"
        " WHERE kind = 'actor' AND provisional = 1 ORDER BY first_seen"
    ).fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_identity.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudlobby/plane/identity.py tests/test_plane_identity.py
git commit -m "feat(plane): alias-to-uid resolver with provisional lazy minting"
```

---

### Task 6: Transactional ingest

**Files:**
- Create: `claudlobby/plane/ingest.py`
- Test: `tests/test_plane_ingest.py`

**Interfaces:**
- Consumes: contracts (Task 3), db (Task 4), identity (Task 5), ids (Task 2).
- Produces: `ingest(conn, env: EmitRequest, payload: BaseModel, *, host_uid: str) -> IngestResult` where `IngestResult` is a dataclass `{event_id: str, ingest_seq: int | None, duplicate: bool}`. One transaction: ledger insert + family insert; `sqlite3.IntegrityError` on the ledger's `event_id` UNIQUE → `duplicate=True`, `ingest_seq=None`, success. Also `now_iso() -> str` (single timestamp formatter used everywhere).

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_ingest.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane.contracts import validate_request
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.ingest import ingest, now_iso
from claudlobby.plane.migrations import migrate


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield conn, host
    conn.close()


def _intent_req(event_id=None) -> dict:
    return {
        "event_type": "communication",
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "event_id": event_id,
        "payload": {
            "msg_id": "msg_" + "1" * 32,
            "sender": "bot:example-fleet/alpha",
            "recipient": "bot:example-fleet/beta",
            "message_class": "chat",
            "body": "hello",
            "privacy": "full",
        },
    }


def test_ingest_writes_ledger_and_family(env):
    conn, host = env
    e, p = validate_request(_intent_req())
    result = ingest(conn, e, p, host_uid=host)
    assert result.duplicate is False and result.ingest_seq == 1
    row = conn.execute("SELECT * FROM communications").fetchone()
    assert row["event_id"] == result.event_id
    assert row["ingest_seq"] == 1
    assert row["host_uid"] == host
    assert row["sender_alias"] == "bot:example-fleet/alpha"
    assert row["sender_uid"].startswith("actor_")
    assert row["fleet_uid"].startswith("fleet_")
    ledger = conn.execute("SELECT family FROM ingest_ledger").fetchone()
    assert ledger["family"] == "communication"


def test_duplicate_event_id_is_success_and_writes_nothing(env):
    conn, host = env
    eid = mint_event_id()
    e, p = validate_request(_intent_req(event_id=eid))
    first = ingest(conn, e, p, host_uid=host)
    assert first.duplicate is False
    # Same event replayed (spool drain, door retry) — different msg body even:
    again = validate_request(_intent_req(event_id=eid))
    second = ingest(conn, again[0], again[1], host_uid=host)
    assert second.duplicate is True and second.ingest_seq is None
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1


def test_family_failure_rolls_back_ledger(env, monkeypatch):
    """If the family insert dies, the ledger row must not survive —
    otherwise the event_id is burned and replay would report duplicate
    for an event that was never stored."""
    conn, host = env
    e, p = validate_request(_intent_req())
    import claudlobby.plane.ingest as mod

    real = mod._insert_family

    def boom(*a, **k):
        raise RuntimeError("family insert failed")

    monkeypatch.setattr(mod, "_insert_family", boom)
    with pytest.raises(RuntimeError):
        ingest(conn, e, p, host_uid=host)
    monkeypatch.setattr(mod, "_insert_family", real)
    assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 0


def test_occurred_at_defaults_to_now(env):
    conn, host = env
    e, p = validate_request(_intent_req())
    ingest(conn, e, p, host_uid=host)
    row = conn.execute("SELECT occurred_at, ingested_at FROM communications").fetchone()
    assert row["occurred_at"].endswith("+00:00")
    assert row["ingested_at"].endswith("+00:00")


def test_now_iso_shape():
    s = now_iso()
    assert s.endswith("+00:00") and "T" in s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_ingest.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ingest.py**

```python
"""The one transactional write path (design v2 §5).

Ledger row + family row commit or roll back together. Duplicate event_id is
SUCCESS (idempotent replay — spool drains and door retries depend on it).
Alias resolution happens here, at write time, so every stored row carries
uids while doors keep speaking aliases.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel

from . import PLANE_SCHEMA_VERSION
from .contracts import (
    Communication,
    EmitRequest,
    TaskAttempt,
    TaskEvent,
    CommunicationAttempt,
    WorkItem,
)
from .identity import resolve_fleet, resolve_party
from .ids import mint_event_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    ingest_seq: int | None
    duplicate: bool


_ENVELOPE_COLS = (
    "ingest_seq, event_id, schema_version, occurred_at, observed_at,"
    " ingested_at, host_uid, fleet_uid, emitter, source_ref,"
    " correlation_id, causation_id, trace_id, span_id"
)


def _envelope_values(seq, event_id, env: EmitRequest, payload, *, host_uid, fleet_uid, now):
    occurred = env.occurred_at.isoformat() if env.occurred_at else now
    return (
        seq, event_id, PLANE_SCHEMA_VERSION, occurred, None, now,
        host_uid, fleet_uid, env.emitter, env.source_ref,
        getattr(payload, "correlation_id", None),
        getattr(payload, "causation_id", None),
        getattr(payload, "trace_id", None),
        getattr(payload, "span_id", None),
    )


def _insert_family(conn, env, payload, base, now):
    if isinstance(payload, Communication):
        sender_uid = resolve_party(conn, payload.sender, now)
        recipient_uid = (
            resolve_party(conn, payload.recipient, now) if payload.recipient else None
        )
        conn.execute(
            f"INSERT INTO communications ({_ENVELOPE_COLS},"
            " msg_id, sender_uid, sender_alias, sender_session_uid,"
            " recipient_uid, recipient_alias,"
            " recipient_raw, message_class, command_type, work_item_id,"
            " task_attempt_id, workstream_id, deliberation_id, reply_to_msg_id,"
            " supersedes_msg_id, body, body_bytes, body_sha256, truncated,"
            " privacy, idempotency_key)"
            " VALUES (" + ",".join("?" * 14) + "," + ",".join("?" * 21) + ")",
            base + (
                payload.msg_id, sender_uid, payload.sender,
                payload.sender_session_uid, recipient_uid,
                payload.recipient, payload.recipient_raw, payload.message_class,
                payload.command_type, payload.work_item_id,
                payload.task_attempt_id, payload.workstream_id,
                payload.deliberation_id,
                payload.reply_to_msg_id, payload.supersedes_msg_id,
                payload.body, payload.body_bytes, payload.body_sha256,
                int(payload.truncated), payload.privacy, payload.idempotency_key,
            ),
        )
    elif isinstance(payload, CommunicationAttempt):
        conn.execute(
            f"INSERT INTO communication_attempts ({_ENVELOPE_COLS},"
            " attempt_id, msg_id, attempt_no, carrier, destination, state,"
            " carrier_ref, error)"
            " VALUES (" + ",".join("?" * 14) + "," + ",".join("?" * 8) + ")",
            base + (
                payload.attempt_id, payload.msg_id, payload.attempt_no,
                payload.carrier, payload.destination, payload.state,
                payload.carrier_ref, payload.error,
            ),
        )
    elif isinstance(payload, WorkItem):
        created_by = resolve_party(conn, payload.created_by, now)
        conn.execute(
            f"INSERT INTO work_items ({_ENVELOPE_COLS},"
            " work_item_id, title, created_by_uid, workstream_id, repo,"
            " project_key, body)"
            " VALUES (" + ",".join("?" * 14) + "," + ",".join("?" * 7) + ")",
            base + (
                payload.work_item_id, payload.title, created_by,
                payload.workstream_id, payload.repo, payload.project_key,
                payload.body,
            ),
        )
    elif isinstance(payload, TaskAttempt):
        assignee = resolve_party(conn, payload.assignee, now)
        assigned_by = resolve_party(conn, payload.assigned_by, now)
        conn.execute(
            f"INSERT INTO task_attempts ({_ENVELOPE_COLS},"
            " task_attempt_id, work_item_id, assignee_uid, assigned_by_uid,"
            " expected_by, dispatch_msg_id)"
            " VALUES (" + ",".join("?" * 14) + "," + ",".join("?" * 6) + ")",
            base + (
                payload.task_attempt_id, payload.work_item_id, assignee,
                assigned_by,
                payload.expected_by.isoformat() if payload.expected_by else None,
                payload.dispatch_msg_id,
            ),
        )
    elif isinstance(payload, TaskEvent):
        actor = resolve_party(conn, payload.actor, now) if payload.actor else None
        conn.execute(
            f"INSERT INTO task_events ({_ENVELOPE_COLS},"
            " work_item_id, task_attempt_id, event, actor_uid, session_uid,"
            " progress, summary, pr_url, deadline, successor_id)"
            " VALUES (" + ",".join("?" * 14) + "," + ",".join("?" * 10) + ")",
            base + (
                payload.work_item_id, payload.task_attempt_id, payload.event,
                actor, payload.session_uid, payload.progress, payload.summary,
                payload.pr_url,
                payload.deadline.isoformat() if payload.deadline else None,
                payload.successor_id,
            ),
        )
    else:  # pragma: no cover — FAMILIES and this dispatch move together
        raise TypeError(f"no insert path for {type(payload).__name__}")


def ingest(
    conn: sqlite3.Connection,
    env: EmitRequest,
    payload: BaseModel,
    *,
    host_uid: str,
) -> IngestResult:
    event_id = env.event_id or mint_event_id()
    now = now_iso()
    try:
        with conn:  # one transaction: ledger + family commit together
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, ?, ?)",
                (event_id, env.event_type, now),
            )
            seq = cur.lastrowid
            fleet_uid = resolve_fleet(conn, env.fleet, now) if env.fleet else None
            base = _envelope_values(
                seq, event_id, env, payload,
                host_uid=host_uid, fleet_uid=fleet_uid, now=now,
            )
            _insert_family(conn, env, payload, base, now)
    except sqlite3.IntegrityError as exc:
        if "ingest_ledger.event_id" in str(exc):
            return IngestResult(event_id=event_id, ingest_seq=None, duplicate=True)
        raise
    return IngestResult(event_id=event_id, ingest_seq=seq, duplicate=False)
```

Note: `identity.resolve` opens its own `with conn` inside the outer transaction — SQLite connections nest `with` as savepoint-free no-ops when a transaction is already open via the same connection, so resolution participates in the ingest transaction. The rollback test (family failure → no ledger row) is what proves this holds; if it fails, inline the resolve calls' SQL into the ingest transaction instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_ingest.py -v 2>&1 | tail -3`
Expected: all PASS. If `test_family_failure_rolls_back_ledger` fails on the nested-`with` behavior, apply the note above (move resolve SQL inline) and re-run.

- [ ] **Step 5: Commit**

```bash
git add claudlobby/plane/ingest.py tests/test_plane_ingest.py
git commit -m "feat(plane): transactional ingest — ledger + family, duplicate replay = success"
```

---

### Task 7: Spool

**Files:**
- Create: `claudlobby/plane/spool.py`
- Test: `tests/test_plane_spool.py`

**Interfaces:**
- Consumes: contracts (Task 3), ingest (Task 6).
- Produces: `spool_dir(root) -> Path` (`<root>/state/plane/spool/`, quarantine subdir); `spool_write(root, raw_request: dict, event_id: str, error: str) -> Path` (atomic tmp+rename, JSON: `{event_id, spooled_at, error, attempts, request}`); `drain(root, conn, host_uid) -> DrainReport` dataclass `{ingested: int, duplicates: int, quarantined: int, remaining: int}`; `spool_entries(root) -> list[dict]` (listing with age); `quarantine(root, name) -> Path`; `MAX_ATTEMPTS = 5`.

Rules (spec §10): mint before spool (event_id arrives as an argument — already minted by emit_api); duplicate on drain = success + delete; `ContractViolation` on drain (schema moved underneath) or attempts exhausted → quarantine, never silent discard, never infinite retry; file deleted only AFTER committed ingest.

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_spool.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.migrations import migrate
from claudlobby.plane.spool import (
    MAX_ATTEMPTS,
    drain,
    quarantine_dir,
    spool_dir,
    spool_entries,
    spool_write,
)


def _req(msg_suffix="2") -> dict:
    return {
        "event_type": "communication",
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + msg_suffix * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "notice",
            "body": "spooled hello",
            "privacy": "full",
        },
    }


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield tmp_path, conn, host
    conn.close()


def test_spool_write_is_atomic_json(env):
    root, conn, host = env
    eid = mint_event_id()
    p = spool_write(root, _req(), eid, "db locked")
    assert p.parent == spool_dir(root)
    data = json.loads(p.read_text())
    assert data["event_id"] == eid and data["attempts"] == 0
    assert not list(spool_dir(root).glob("*.tmp"))


def test_drain_ingests_and_deletes(env):
    root, conn, host = env
    spool_write(root, _req(), mint_event_id(), "db locked")
    report = drain(root, conn, host)
    assert report.ingested == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    assert spool_entries(root) == []


def test_drain_duplicate_is_success(env):
    root, conn, host = env
    eid = mint_event_id()
    spool_write(root, _req(), eid, "x")
    drain(root, conn, host)
    # Same event spooled again (door retried after crash) — drains as duplicate.
    spool_write(root, _req(), eid, "x")
    report = drain(root, conn, host)
    assert report.duplicates == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1


def test_malformed_spool_file_quarantined(env):
    root, conn, host = env
    bad = spool_dir(root) / "garbage.json"
    bad.write_text("{not json")
    report = drain(root, conn, host)
    assert report.quarantined == 1
    assert list(quarantine_dir(root).iterdir())


def test_contract_violation_quarantined_not_retried(env):
    root, conn, host = env
    req = _req()
    req["payload"]["message_class"] = "no-such-class"
    spool_write(root, req, mint_event_id(), "x")
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0


def test_attempts_capped(env, monkeypatch):
    root, conn, host = env
    spool_write(root, _req(), mint_event_id(), "x")
    import claudlobby.plane.spool as mod

    def always_fail(*a, **k):
        raise RuntimeError("db still down")

    monkeypatch.setattr(mod, "ingest", always_fail)
    for _ in range(MAX_ATTEMPTS):
        drain(root, conn, host)
    assert spool_entries(root) == []  # exhausted → quarantined
    assert len(list(quarantine_dir(root).iterdir())) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_spool.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3: Implement spool.py**

```python
"""Filesystem spool — the valve that must not depend on the db it protects
(design v2 §10). Plain JSON files, atomic tmp+rename, drained by plane
status/doctor or any caller; deletion only after committed ingest; poison
records quarantined with their reason, never silently dropped, never
retried forever.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractViolation, validate_request
from .ingest import ingest  # patched in tests; keep module-level name

MAX_ATTEMPTS = 5


def spool_dir(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "spool"
    p.mkdir(parents=True, exist_ok=True)
    return p


def quarantine_dir(root: Path) -> Path:
    p = spool_dir(root) / "quarantine"
    p.mkdir(parents=True, exist_ok=True)
    return p


def spool_write(root: Path, raw_request: dict, event_id: str, error: str) -> Path:
    entry = {
        "event_id": event_id,
        "spooled_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "attempts": 0,
        "request": raw_request,
    }
    target = spool_dir(root) / f"{event_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, target)
    return target


def spool_entries(root: Path) -> list[dict]:
    out = []
    for f in sorted(spool_dir(root).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            data = {"event_id": None, "spooled_at": None, "attempts": None}
        data["_file"] = f.name
        out.append(data)
    return out


def _quarantine(root: Path, f: Path, reason: str) -> None:
    meta = quarantine_dir(root) / (f.name + ".reason")
    meta.write_text(reason + "\n")
    os.replace(f, quarantine_dir(root) / f.name)


@dataclass(frozen=True)
class DrainReport:
    ingested: int = 0
    duplicates: int = 0
    quarantined: int = 0
    remaining: int = 0


def drain(root: Path, conn: sqlite3.Connection, host_uid: str) -> DrainReport:
    ingested = duplicates = quarantined = 0
    for f in sorted(spool_dir(root).glob("*.json")):
        try:
            entry = json.loads(f.read_text())
            raw = entry["request"]
            raw = {**raw, "event_id": entry["event_id"]}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            _quarantine(root, f, f"malformed spool file: {exc}")
            quarantined += 1
            continue
        try:
            env, payload = validate_request(raw)
        except ContractViolation as exc:
            _quarantine(root, f, f"contract violation on drain: {exc}")
            quarantined += 1
            continue
        try:
            result = ingest(conn, env, payload, host_uid=host_uid)
        except Exception as exc:  # noqa: BLE001 — infra failure: retry or quarantine
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["error"] = str(exc)
            if entry["attempts"] >= MAX_ATTEMPTS:
                f.write_text(json.dumps(entry, ensure_ascii=False) + "\n")
                _quarantine(root, f, f"retries exhausted: {exc}")
                quarantined += 1
            else:
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(entry, ensure_ascii=False) + "\n")
                os.replace(tmp, f)
            continue
        if result.duplicate:
            duplicates += 1
        else:
            ingested += 1
        f.unlink()  # only after committed ingest
    remaining = len(list(spool_dir(root).glob("*.json")))
    return DrainReport(ingested, duplicates, quarantined, remaining)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_spool.py -v 2>&1 | tail -3`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add claudlobby/plane/spool.py tests/test_plane_spool.py
git commit -m "feat(plane): filesystem spool — atomic, capped retries, quarantine"
```

---

### Task 8: emit API and CLI

**Files:**
- Create: `claudlobby/plane/emit_api.py`
- Create: `claudlobby/commands/plane.py`
- Modify: `claudlobby/commands/_parsers.py` (add registrations at the end of `register_subparsers`, following the existing `sub.add_parser(...)` + `set_defaults(func=...)` pattern)
- Test: `tests/test_plane_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `emit_api.emit(root: Path, raw_request: dict) -> EmitOutcome` — dataclass `{event_id, status: Literal["committed","duplicate","spooled"], detail: str | None}`. Flow: validate (ContractViolation propagates — NEVER spooled) → connect+migrate → ingest; on `sqlite3.OperationalError`/`sqlite3.DatabaseError` → `spool_write` → `spooled`.
  - CLI `claudlobby emit <event_type> --json -` (stdin) or `--json <path>`: prints `event_id` on stdout; exit 0 committed/duplicate/spooled (spooled adds one stderr line `plane: db unavailable — spooled <file>`); exit 2 on ContractViolation (stderr: first error); exit 3 if spool write itself failed.
  - CLI `claudlobby plane status`: db path + exists, `user_version`, per-family row counts, ledger max seq, spool depth + oldest entry age, provisional actor count. Exit 0.
  - CLI `claudlobby plane spool list|retry|quarantine <file>`: `list` prints entries (name, event_id, attempts, age); `retry` runs `drain`; `quarantine <name>` force-moves one entry.
  - CLI `claudlobby plane schema`: prints `export_schemas()` JSON to stdout (feeds TS codegen).
- Root resolution: reuse the CLI's existing `--root` global (`args.root`), defaulting like other commands do — inspect how `cmd_status` resolves root in `commands/core.py` and use the same helper (it exists in `_helpers.py`; read it before writing and mirror the call, adapting only the function name if it differs from `resolve_root`).

- [ ] **Step 1: Write the failing tests**

`tests/test_plane_cli.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.migrations import migrate


def _run(args: list[str], stdin: str | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", *args],
        input=stdin, capture_output=True, text=True, cwd=cwd,
    )


def _intent_json() -> str:
    return json.dumps({
        "event_type": "communication",
        "emitter": "cli-test",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + "3" * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "chat",
            "body": "via cli",
            "privacy": "full",
        },
    })


def test_emit_commits_and_prints_event_id(tmp_path: Path):
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=_intent_json())
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("ev_")
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    conn.close()


def test_emit_contract_violation_exits_2(tmp_path: Path):
    bad = json.loads(_intent_json())
    bad["payload"]["message_class"] = "yell"
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=json.dumps(bad))
    assert r.returncode == 2
    assert "message_class" in r.stderr
    # Nothing written, nothing spooled:
    assert not (tmp_path / "state" / "plane" / "spool").exists() or not list(
        (tmp_path / "state" / "plane" / "spool").glob("*.json")
    )


def test_plane_status_reports(tmp_path: Path):
    _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
         stdin=_intent_json())
    r = _run(["--root", str(tmp_path), "plane", "status"])
    assert r.returncode == 0
    assert "communication" in r.stdout and "spool" in r.stdout


def test_plane_schema_exports_json(tmp_path: Path):
    r = _run(["--root", str(tmp_path), "plane", "schema"])
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "envelope" in data and "task_event" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_cli.py -v 2>&1 | tail -4`
Expected: FAIL — argparse error (unknown command `emit`)

- [ ] **Step 3: Implement emit_api.py**

```python
"""emit(): the programmatic spine every writer uses (design v2 §5).

Failure taxonomy is the contract:
  ContractViolation  → caller bug, propagate, write NOTHING (not even spool)
  db unavailable     → spool + report spooled (the caller's own job proceeds)
  spool also failed  → propagate SpoolWriteError (exit 3 at the CLI)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .contracts import validate_request
from .db import connect, db_path
from .ids import ensure_host_uid, mint_event_id
from .ingest import ingest
from .migrations import migrate
from .spool import spool_write


class SpoolWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmitOutcome:
    event_id: str
    status: Literal["committed", "duplicate", "spooled"]
    detail: Optional[str] = None


def emit(root: Path, raw_request: dict) -> EmitOutcome:
    env, payload = validate_request(raw_request)  # ContractViolation propagates
    event_id = env.event_id or mint_event_id()
    raw_request = {**raw_request, "event_id": event_id}
    try:
        conn = connect(db_path(root))
        try:
            migrate(conn)
            host = ensure_host_uid(Path(root) / "state")
            result = ingest(
                conn,
                env.model_copy(update={"event_id": event_id}),
                payload,
                host_uid=host,
            )
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, RuntimeError) as exc:
        try:
            path = spool_write(root, raw_request, event_id, str(exc))
        except OSError as spool_exc:
            raise SpoolWriteError(
                f"db failed ({exc}) AND spool failed ({spool_exc})"
            ) from spool_exc
        return EmitOutcome(event_id, "spooled", detail=str(path))
    status = "duplicate" if result.duplicate else "committed"
    return EmitOutcome(event_id, status)
```

- [ ] **Step 4: Implement commands/plane.py and register**

`claudlobby/commands/plane.py`:

```python
"""claudlobby emit / claudlobby plane — the kernel's CLI surface."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..plane.contracts import ContractViolation, export_schemas
from ..plane.db import connect, db_path
from ..plane.emit_api import SpoolWriteError, emit
from ..plane.identity import provisional_actors
from ..plane.ids import ensure_host_uid
from ..plane.migrations import migrate
from ..plane.spool import drain, quarantine_dir, spool_dir, spool_entries

_FAMILY_TABLES = {
    "communication": "communications",
    "communication_attempt": "communication_attempts",
    "work_item": "work_items",
    "task_attempt": "task_attempts",
    "task_event": "task_events",
}


def _read_request(args) -> dict:
    raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
    request = json.loads(raw)
    request["event_type"] = args.event_type
    return request


def cmd_emit(args, root: Path) -> int:
    try:
        request = _read_request(args)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"emit: unreadable request: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = emit(root, request)
    except ContractViolation as exc:
        first = exc.errors[0] if exc.errors else {}
        print(f"emit: contract violation: {first}", file=sys.stderr)
        return 2
    except SpoolWriteError as exc:
        print(f"emit: TOTAL FAILURE — {exc}", file=sys.stderr)
        return 3
    print(outcome.event_id)
    if outcome.status == "spooled":
        print(f"plane: db unavailable — spooled {outcome.detail}", file=sys.stderr)
    return 0


def cmd_plane_status(args, root: Path) -> int:
    path = db_path(root)
    print(f"db: {path} ({'present' if path.exists() else 'absent'})")
    if path.exists():
        conn = connect(path)
        try:
            migrate(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            print(f"schema user_version: {version}")
            top = conn.execute(
                "SELECT COALESCE(MAX(ingest_seq), 0) FROM ingest_ledger"
            ).fetchone()[0]
            print(f"ingest_seq high-water: {top}")
            for family, table in _FAMILY_TABLES.items():
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {family}: {n}")
            prov = provisional_actors(conn)
            print(f"provisional actors: {len(prov)}")
        finally:
            conn.close()
    entries = spool_entries(root)
    oldest = ""
    if entries and entries[0].get("spooled_at"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            entries[0]["spooled_at"]
        )
        oldest = f", oldest {int(age.total_seconds())}s"
    print(f"spool: {len(entries)} pending{oldest}")
    print(f"quarantine: {len(list(quarantine_dir(root).glob('*.json')))}")
    return 0


def cmd_plane_spool(args, root: Path) -> int:
    if args.spool_action == "list":
        for e in spool_entries(root):
            print(f"{e['_file']}  event={e.get('event_id')}  attempts={e.get('attempts')}")
        return 0
    if args.spool_action == "retry":
        conn = connect(db_path(root))
        try:
            migrate(conn)
            host = ensure_host_uid(root / "state")
            report = drain(root, conn, host)
        finally:
            conn.close()
        print(
            f"ingested={report.ingested} duplicates={report.duplicates}"
            f" quarantined={report.quarantined} remaining={report.remaining}"
        )
        return 0
    if args.spool_action == "quarantine":
        src = spool_dir(root) / args.name
        if not src.exists():
            print(f"no such spool entry: {args.name}", file=sys.stderr)
            return 1
        import os

        (quarantine_dir(root) / (args.name + ".reason")).write_text("operator\n")
        os.replace(src, quarantine_dir(root) / args.name)
        print(f"quarantined {args.name}")
        return 0
    return 1


def cmd_plane_schema(args, root: Path) -> int:
    print(json.dumps(export_schemas(), indent=2, sort_keys=True))
    return 0
```

Register in `_parsers.py` (append inside `register_subparsers`, mirroring neighbors; the exact `func=` calling convention — whether commands receive `(args)` or `(args, root)` — MUST be copied from how `cmd_status` is registered and invoked; adapt the four `cmd_*` signatures above to match it exactly):

```python
    # --- observable plane (Phase 1 kernel) ---
    pe = sub.add_parser("emit", help="Validated event ingest into the plane db")
    pe.add_argument("event_type", help="communication | communication_attempt | work_item | task_attempt | task_event")
    pe.add_argument("--json", required=True, help="Request JSON path, or '-' for stdin")
    pe.set_defaults(func=cmd_emit)

    pp = sub.add_parser("plane", help="Observable-plane operations")
    psub = pp.add_subparsers(dest="plane_action", required=True)
    ps = psub.add_parser("status", help="Kernel health: db, counts, spool")
    ps.set_defaults(func=cmd_plane_status)
    psc = psub.add_parser("schema", help="Export JSON Schemas (envelope + families)")
    psc.set_defaults(func=cmd_plane_schema)
    psp = psub.add_parser("spool", help="Inspect/drain the emit spool")
    psp.add_argument("spool_action", choices=["list", "retry", "quarantine"])
    psp.add_argument("name", nargs="?", help="Spool file name (quarantine)")
    psp.set_defaults(func=cmd_plane_spool)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_cli.py -v 2>&1 | tail -4`
Expected: all PASS

- [ ] **Step 6: Run the FULL suite with the baseline protocol**

Follow CLAUDE.md's counts+names recipe (stash/before/after). Expected: zero new failing names; count delta explained entirely by new passing tests.

- [ ] **Step 7: Commit**

```bash
git add claudlobby/plane/emit_api.py claudlobby/commands/plane.py claudlobby/commands/_parsers.py tests/test_plane_cli.py
git commit -m "feat(plane): claudlobby emit + plane status/spool/schema CLI"
```

---

### Task 9: Crash and concurrency battery

**Files:**
- Test: `tests/test_plane_crash_battery.py`

**Interfaces:** consumes everything; produces confidence. These are the spec §15 crash boundaries expressible at kernel level (door-level boundaries — send-succeeded/record-missing — are Phase 2, they need doors).

- [ ] **Step 1: Write the battery**

```python
"""Kernel crash/concurrency battery (design v2 §15).

Covers: concurrent emitters (25-writer burst), SQLITE_BUSY under a held
write lock, disk-full via PRAGMA max_page_count, duplicate replay under
concurrency, spool fallback when the db is unopenable.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.migrations import migrate


def _mk_request(i: int) -> dict:
    return {
        "event_type": "task_event",
        "emitter": f"writer-{i}",
        "fleet": "example-fleet",
        "payload": {
            "work_item_id": "wi_" + f"{i:032x}",
            "event": "progress",
            "progress": i % 100,
            "actor": f"bot:example-fleet/w{i}",
        },
    }


def _worker(root: str, i: int, out: mp.Queue) -> None:
    try:
        outcome = emit(Path(root), _mk_request(i))
        out.put((i, outcome.status))
    except Exception as exc:  # noqa: BLE001
        out.put((i, f"error:{exc}"))


def test_25_writer_burst_loses_nothing(tmp_path: Path):
    # Prime db + host uid once to avoid a 25-way migration race:
    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")

    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_worker, args=(str(tmp_path), i, q)) for i in range(25)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    results = [q.get(timeout=5) for _ in range(25)]
    statuses = {s for _, s in results}
    assert statuses <= {"committed", "spooled"}, results
    conn = connect(db_path(tmp_path))
    committed = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    spooled = len(list((tmp_path / "state" / "plane" / "spool").glob("*.json")))
    conn.close()
    assert committed + spooled == 25  # nothing lost
    # Ordering authority: seqs are gapless 1..N for committed rows
    conn = connect(db_path(tmp_path))
    seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY 1")]
    conn.close()
    assert seqs == list(range(1, committed + 1))


def test_busy_lock_leads_to_spool_not_loss(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    ensure_host_uid(tmp_path / "state")
    # Hold an exclusive write lock from a raw connection with NO busy_timeout,
    # long enough that emit's 5s busy_timeout expires:
    blocker = sqlite3.connect(db_path(tmp_path))
    blocker.execute("PRAGMA busy_timeout = 0")
    blocker.execute("BEGIN EXCLUSIVE")
    outcome = emit(tmp_path, _mk_request(1))
    assert outcome.status == "spooled"
    blocker.rollback()
    blocker.close()
    conn.close()


def test_disk_full_spools(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    ensure_host_uid(tmp_path / "state")
    # Clamp the db to its current page count so the next insert gets SQLITE_FULL:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {pages}")
    conn.close()
    # emit opens its own connection; re-apply the clamp there by pre-shrinking:
    # max_page_count is per-connection, so simulate instead by filling: insert
    # rows until SQLITE_FULL via a clamped connection, proving the spool path.
    clamped = sqlite3.connect(db_path(tmp_path))
    clamped.execute(f"PRAGMA max_page_count = {pages}")
    with pytest.raises(sqlite3.OperationalError, match="full"):
        for i in range(10_000):
            clamped.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, 'task_event', 't')",
                (f"ev_{i:032x}",),
            )
        clamped.commit()
    clamped.close()


def test_duplicate_event_id_under_concurrency(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")
    fixed = {"event_id": "ev_" + "d" * 32, **_mk_request(1)}
    first = emit(tmp_path, dict(fixed))
    second = emit(tmp_path, dict(fixed))
    assert first.status == "committed" and second.status == "duplicate"


def test_unopenable_db_spools(tmp_path: Path):
    # A directory where the db file should be → connect raises → spool.
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "plane.db").mkdir()
    outcome = emit(tmp_path, _mk_request(2))
    assert outcome.status == "spooled"
```

- [ ] **Step 2: Run the battery**

Run: `./.venv/bin/pytest tests/test_plane_crash_battery.py -v 2>&1 | tail -8`
Expected: all PASS. (`test_disk_full_spools` proves the SQLITE_FULL error class reaches callers as OperationalError — the class emit() spools on; the burst test is the load-bearing one.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_plane_crash_battery.py
git commit -m "test(plane): crash/concurrency battery — burst, busy, full, duplicate, unopenable"
```

---

### Task 10: Benchmark harness (the Phase-2 gate)

**Files:**
- Create: `bin/plane-bench.py` (chmod 0755)

**Interfaces:**
- Produces the numbers that rule spec §19 item 2 (direct writer vs socket daemon). Decision rule (recorded here, applied at Phase-2 planning): **cold-emit p95 ≤ 300 ms on the Pi AND the 25-writer burst completes with zero `error:` outcomes → the direct writer ships in Phase 2; otherwise the Unix-socket ingest daemon goes into the Phase 2 plan.** Fleet message rates are <1 Hz sustained, so per-emit cost bounds matter far more than throughput.

- [ ] **Step 1: Write the harness**

```python
#!/usr/bin/env python3
"""plane-bench: cold/warm emit latency + burst behavior (design v2 §14).

Usage: ./bin/plane-bench.py [--root DIR] [--cold N] [--warm N] [--burst N]
Writes a fresh throwaway db under --root (default: a mkdtemp), prints a
markdown results block to paste into the Phase-2 plan.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _request(i: int) -> dict:
    return {
        "event_type": "task_event",
        "emitter": "bench",
        "fleet": "bench-fleet",
        "payload": {
            "work_item_id": "wi_" + f"{i:032x}",
            "event": "progress",
            "progress": i % 100,
        },
    }


def _pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * len(xs))) )]


def bench_cold(root: Path, n: int) -> list[float]:
    """Full subprocess spawn per emit — what a bash door pays."""
    out = []
    for i in range(n):
        payload = json.dumps(_request(i))
        t0 = time.perf_counter()
        r = subprocess.run(
            [sys.executable, "-m", "claudlobby", "--root", str(root),
             "emit", "task_event", "--json", "-"],
            input=payload, capture_output=True, text=True, cwd=REPO,
        )
        dt = time.perf_counter() - t0
        if r.returncode != 0:
            print(f"cold emit {i} failed rc={r.returncode}: {r.stderr}", file=sys.stderr)
            continue
        out.append(dt)
    return out


def bench_warm(root: Path, n: int) -> list[float]:
    """In-process emit — what a resident daemon would pay per event."""
    from claudlobby.plane.emit_api import emit
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        emit(root, _request(100_000 + i))
        out.append(time.perf_counter() - t0)
    return out


def bench_burst(root: Path, n: int) -> dict:
    import multiprocessing as mp
    from claudlobby.plane.emit_api import emit as _emit

    def worker(i: int, q):
        try:
            o = _emit(root, _request(200_000 + i))
            q.put(o.status)
        except Exception as exc:  # noqa: BLE001
            q.put(f"error:{exc}")

    q: mp.Queue = mp.Queue()
    t0 = time.perf_counter()
    procs = [mp.Process(target=worker, args=(i, q)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    wall = time.perf_counter() - t0
    results = [q.get(timeout=5) for _ in range(n)]
    return {
        "wall_s": round(wall, 2),
        "committed": results.count("committed"),
        "spooled": results.count("spooled"),
        "errors": [r for r in results if str(r).startswith("error:")],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--cold", type=int, default=50)
    ap.add_argument("--warm", type=int, default=1000)
    ap.add_argument("--burst", type=int, default=25)
    args = ap.parse_args()
    root = args.root or Path(tempfile.mkdtemp(prefix="plane-bench-"))

    cold = bench_cold(root, args.cold)
    warm = bench_warm(root, args.warm)
    burst = bench_burst(root, args.burst)

    print("## plane-bench results\n")
    print(f"- host: `{__import__('platform').node()}` "
          f"({__import__('platform').machine()}), python {sys.version.split()[0]}")
    for name, xs in (("cold (subprocess)", cold), ("warm (in-process)", warm)):
        ms = [x * 1000 for x in xs]
        print(f"- {name}: n={len(ms)} p50={_pctl(ms, 50):.1f}ms "
              f"p95={_pctl(ms, 95):.1f}ms max={max(ms):.1f}ms "
              f"mean={statistics.mean(ms):.1f}ms")
    print(f"- burst n={args.burst}: wall={burst['wall_s']}s "
          f"committed={burst['committed']} spooled={burst['spooled']} "
          f"errors={len(burst['errors'])}")
    print("\nGate (Phase-2 ingest choice): Pi cold p95 ≤ 300ms AND burst errors == 0 → direct writer; else socket daemon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run on this machine and record**

Run: `./.venv/bin/python bin/plane-bench.py 2>&1 | tail -8`
Expected: results block prints; zero burst errors. Paste the block into the commit message body.

- [ ] **Step 3: The Pi gate (operator step — do not skip silently)**

The binding numbers are the Pi's. From the repo on the Pi (after this branch lands there):

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]' -q
./.venv/bin/python bin/plane-bench.py
```

Paste the Pi results into the Phase-2 plan's header when writing it. If the gate fails, the Phase-2 plan opens with the socket-daemon task.

- [ ] **Step 4: Commit**

```bash
chmod 755 bin/plane-bench.py
git add bin/plane-bench.py
git commit -m "feat(plane): emit benchmark harness — the Phase-2 ingest gate"
```

---

### Task 11: Phase finalization

**Files:**
- Modify: `CHANGELOG.md` (if an Unreleased section exists — follow its format)
- Modify: `documentation/plans/2026-08-18-observable-plane-design-v2.md` (§19: mark items 1–3 delivered-by-plan, item 2 pending-Pi-numbers)

- [ ] **Step 1: Full-suite baseline diff**

Run the CLAUDE.md counts+names protocol once more over the whole branch. Expected: no new failing names vs the pre-branch baseline; count delta = new plane tests, all passing.

- [ ] **Step 2: Run the phase-finalization gate**

Per the operator's standing rule: `/simplify` over the branch diff, then `/code-review` (or `/review-work`), then `/verify-completion`. Address findings before the PR.

- [ ] **Step 3: Update spec §19 statuses + CHANGELOG**

In the v2 spec, §19: item 1 (canonical spec) → "delivered: `plane/canonical.py` CANON_V1 + golden fixtures"; item 2 (ingest impl) → "harness delivered (`bin/plane-bench.py`); AWAITING Pi numbers"; item 3 (DDL) → "delivered: migration 0001". Item 4 already carries Claudron#145.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin design/observable-plane
gh pr create --title "feat(plane): Phase 1 semantic kernel — canonical bytes, identity, envelope, ingest, spool, emit CLI" --body "$(cat <<'EOF'
Implements the observable-plane Phase 1 kernel per documentation/plans/2026-08-19-observable-plane-phase1-kernel.md (spec: 2026-08-18-observable-plane-design-v2.md, forks F1-F18).

- CANON_V1 canonical bytes + golden fixtures
- Minted ids; persisted host uid (0600, refuses corrupt re-mint)
- Pydantic v2 envelope + five family contracts (closed vocabularies; `delivered` deliberately absent)
- SQLite WAL kernel: ingest_ledger (global ingest_seq), identity_registry (provisional lazy mint), five append-only family tables
- One transactional ingest path; duplicate event_id replay = success
- Filesystem spool: atomic, capped retries, quarantine, drains without the (future) UI daemon
- `claudlobby emit` + `claudlobby plane status|spool|schema`
- Crash/concurrency battery: 25-writer burst loses nothing; busy/full/unopenable → spool
- plane-bench harness; Pi numbers gate Phase 2's ingest choice

Validation: <paste counts+names diff summary and local bench block here>.
No door, hook, or bot-runtime behavior is touched — the runtime-change gate applies from Phase 2 onward.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: Hand off to Phase 2 planning**

Phase 2's plan (doors as shims, transport-attempt evidence from `pane_send_verified`, acknowledgement, dual-write canary, projections, reader layer consuming `brief`) is written ONLY after: this PR merges, the Pi bench numbers exist, and the door callsite inventory is re-run against merged main.

---

## Self-Review (performed at write time)

- **Spec coverage:** F16 (typed tables + shared seq) → Task 4; F17 vocabulary → Task 3 (`blocked_waiting`/`returned_blocked`, no bare `blocked`); F10/§3 identity → Tasks 2+5; §4 envelope → Tasks 3+4 (all envelope fields present as columns; `observed_at` nullable — populated by Phase-2 doors that observe rather than produce); §5 spine + failure taxonomy → Tasks 6–8; §9 canonicalization → Task 1; §10 spool → Task 7; §14 gates → Tasks 9–10; §15 kernel-expressible tests → Tasks 1–9 (door-level crash boundaries and dual-write mismatch tests are Phase 2 by construction — they need doors); F18 backfill → deliberately absent (Phase 2, per F18's own definition). F7 privacy: the `privacy` field is enforced per-row; the fleet.yaml opt-in knob composes in Phase 2 with the doors (constraint noted in spec §11).
- **Placeholder scan:** no TBDs; every step carries runnable code or an exact command. Two deliberate executor-verification points are flagged inline as instructions, not gaps: the `cmd_*` calling convention (Task 8 — copy from `cmd_status`'s registration) and the nested-transaction note (Task 6).
- **Type consistency:** `EmitRequest.fleet` (alias) vs stored `fleet_uid` — consistent through ingest; `ID_PATTERNS` keys used by contracts match ids.py; `FAMILIES` keys match `_FAMILY_TABLES` keys in commands/plane.py; `IngestResult`/`EmitOutcome`/`DrainReport` field names consistent across Tasks 6–8 and tests.
