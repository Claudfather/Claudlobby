# Observable Plane — Phase 1: Semantic Kernel Implementation Plan

> **REVISION v2.1 (2026-08-24):** round-2 external review reconciled — ten implementation-blocking findings. Every variable-shape family INSERT derives its placeholders from column dicts (hand-counted arithmetic banned; fixed-shape INSERTs stay fixed); ingest is the sole transaction owner; migrations own their transactions in-script; the events CHECK is NULL-safe require-AND-forbid, tested by an executed INSERT matrix from `KIND_MANIFEST`; EmitRequest carries the full envelope (+origin/import_batch/confidence); `emit-batch` provides the atomic dispatch unit; the ack is single (task `receiver_acknowledged` deleted); spool fsyncs and classifies failures by SQLite error class; capture policy is config-resolved with metadata-mode body drop; files 0600/dirs 0700; id patterns anchored; NFC collisions rejected; quarantine names validated; bench is spawn-safe and gains read queries + EXPLAIN.

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
  registries.py        — FIELD_POLICY classification registry (enforcement SSOT); Phase-2b seeds join here
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


def test_nfc_key_collision_rejected():
    nfd, nfc = "e\u0301", "\u00e9"   # both normalize to é
    with pytest.raises(CanonicalizationError):
        canonical_bytes({nfd: 1, nfc: 2})


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
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                # Two distinct input keys normalizing to one NFC key would
                # silently drop a value — round-2 F9 input hardening.
                raise CanonicalizationError(f"NFC key collision: {k!r}")
            out[nk] = _normalize(v)
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
- Produces: `mint(prefix: str) -> str` and the typed wrappers `mint_event_id()`, `mint_msg_id()`, `mint_work_item_id()`, `mint_assignment_id()`, `derive_session_uid(platform_session_id)`, `mint_uid(kind: str) -> str`; `ensure_host_uid(state_dir: Path) -> str`; `ID_PATTERNS: dict[str, str]` (regex per prefix, consumed by contracts.py validation).

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


def test_session_uid_is_derived_and_stable():
    from claudlobby.plane.ids import derive_session_uid

    import pytest
    with pytest.raises(ValueError):
        derive_session_uid("")
    a = derive_session_uid("8ad2aa7e-bade-4c55-b3c3-000000000000")
    b = derive_session_uid("8ad2aa7e-bade-4c55-b3c3-000000000000")
    c = derive_session_uid("different-session")
    assert a == b and a != c
    assert re.fullmatch(r"sess_[0-9a-f]{32}", a)


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


def test_fast_path_fsyncs_directory_before_return(tmp_path: Path, monkeypatch):
    """Round-3 F2: the read path must make the dirent durable itself — the
    winner's own fsync may not have happened yet when a loser returns."""
    import os as _os

    (tmp_path / "host-uid").write_text("host_" + "a" * 32 + "\n")
    synced_dirs = []
    real_fsync = _os.fsync

    def spy(fd):
        try:
            import stat as _stat
            if _stat.S_ISDIR(_os.fstat(fd).st_mode):
                synced_dirs.append(fd)
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", spy)
    assert ensure_host_uid(tmp_path) == "host_" + "a" * 32
    assert synced_dirs, "fast path returned without fsyncing the directory"


def test_publish_is_create_if_absent(tmp_path: Path):
    """Round-3 F2: a pre-existing final file always wins; minting never
    overwrites it (the link-publish loser path). Round-6 note: the fast path
    also REPAIRS a lax pre-existing mode to 0600."""
    import os as _os, stat as _stat

    f = tmp_path / "host-uid"
    f.write_text("host_" + "b" * 32 + "\n")     # write_text => typically 0644
    _os.chmod(f, 0o644)                          # deterministic, not umask-luck
    assert ensure_host_uid(tmp_path) == "host_" + "b" * 32
    assert _stat.S_IMODE(_os.stat(f).st_mode) == 0o600


def test_crash_litter_does_not_break_minting(tmp_path: Path):
    """A crash between tmp-write and publish leaves only tmp litter — the
    next call mints normally and the final file appears complete."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / ".host-uid.999.deadbeef.tmp").write_text("host_" + "c" * 32 + "\n")
    value = ensure_host_uid(tmp_path)
    assert value.startswith("host_")
    assert (tmp_path / "host-uid").read_text().strip() == value


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

# Anchored (round-2 F9): pydantic's pattern is a SEARCH — unanchored patterns
# accept embedded garbage. ^...$ anchors work in both Rust-regex and re.
ID_PATTERNS: dict[str, str] = {
    "event": r"^ev_[0-9a-f]{32}$",
    "msg": r"^msg_[0-9a-f]{32}$",
    "work_item": r"^wi_[0-9a-f]{32}$",
    "assignment": r"^asg_[0-9a-f]{32}$",
    **{kind: "^" + prefix + r"[0-9a-f]{32}$" for kind, prefix in _UID_PREFIX.items()},
}

_HOST_UID_RE = re.compile(r"^host_[0-9a-f]{32}$")


def mint(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def mint_event_id() -> str:
    return mint("ev_")


def mint_msg_id() -> str:
    return mint("msg_")


def mint_work_item_id() -> str:
    return mint("wi_")


def mint_assignment_id() -> str:
    return mint("asg_")


def derive_session_uid(platform_session_id: str) -> str:
    """sess_ uid DERIVED from the platform session id (sha256, first 32 hex).

    Deliberately deterministic, not random (§9d): any emitter — bash included,
    via shasum — computes the same uid for the same session with no registry
    lookup, and the transcript/OTel join needs exactly that stability."""
    import hashlib

    if not platform_session_id or not platform_session_id.strip():
        raise ValueError("empty platform session id — refusing to derive")
    digest = hashlib.sha256(platform_session_id.encode("utf-8")).hexdigest()
    return "sess_" + digest[:32]


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
        os.chmod(path, 0o600)   # round-4 note: a pre-existing valid file may
                                 # carry its creator's mode; the uid is joined
                                 # against every row — owner-only, always
        # Round-3 F2 (verifier's window): a loser can reach this fast path
        # after the winner's os.link but BEFORE the winner's directory fsync —
        # returning a uid whose dirent a power loss could still erase, while
        # the caller starts recording events under it. Fsync the directory
        # before EVERY return, so the published name is durable no matter
        # which racer syncs it first.
        dfd = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return value
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    value = mint_uid("host")
    # Round-3 F2: NEVER publish the final pathname before its content exists.
    # Write+fsync a UNIQUE tmp, then os.link() it into place — link fails
    # EEXIST if a winner already published (create-if-absent), and the final
    # name only ever appears fully written. A crash leaves only tmp litter.
    tmp = state_dir / f".host-uid.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (value + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass                      # loser: the winner's COMPLETE file is there
    finally:
        os.unlink(tmp)
    dfd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return ensure_host_uid(state_dir)   # single read path validates the result
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
  - `FAMILIES: dict[str, type[BaseModel]]` mapping event_type → payload model: `"communication" → Communication`, `"transmission" → Transmission`, `"work_item" → WorkItem`, `"assignment" → Assignment`, `"task" → TaskEvent`.
  - Enums as `Literal` sets: `MESSAGE_CLASSES`, `COMMAND_TYPES`, `ATTEMPT_STATES`, `TASK_EVENTS` (exact values below).
  - `validate_request(raw: dict) -> tuple[EmitRequest, BaseModel]` — parses envelope then family payload; raises `ContractViolation` (carries `.errors`).
  - `export_schemas() -> dict` — JSON Schema per family + envelope (feeds F2's TS codegen later).
  - Body handling: `cap_body(text: str) -> BodyFields` applying the 16 KiB cap + ANSI strip + sha256-of-full.

Vocabulary (spec §7, F11, F17 — exact):
- `MESSAGE_CLASSES = task_request, report, question, answer, alert, notice, briefing, nudge, acknowledgement, chat, config_change, raw_control`
- `COMMAND_TYPES = task, cancel, compact, restart, query`
- `ATTEMPT_STATES = send_attempted, carrier_accepted, pane_submitted, failed, unknown, recipient_acknowledged, duplicate_suppressed`
- `TASK_EVENTS = dispatch_intended, transmission_failed, dispatch_submitted, accepted, rejected, progress, blocked_waiting, returned_blocked, resumed, completed, failed, cancelled, deadline_changed, superseded, reassigned, retry_created, orphaned_by_session_loss, recovered_after_restart, expired`
- `contract_created` is NOT a task-kind event: the `work_item`/`assignment` row IS that event (one fact, one row — spec §8 mapping note).
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
        "communication", "transmission", "work_item", "assignment", "task"
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


def test_fleet_required_for_scoped_types():
    req = _req("comm_intent", _intent_payload()) if False else _req("communication", _intent_payload())
    req.pop("fleet")
    with pytest.raises(ContractViolation):
        validate_request(req)


def test_payload_envelope_duplicates_rejected():
    """Round-3 F4: correlation/causation/trace/span are envelope-only."""
    with pytest.raises(ContractViolation):
        validate_request(_req("communication", _intent_payload(correlation_id="x")))


def test_caps_enforce_from_field_policy(monkeypatch):
    """Round-5/6 F8: FIELD_POLICY is the SSOT for EVERY content family —
    shrinking any cap changes enforcement with no other edit."""
    from claudlobby.plane import registries

    monkeypatch.setitem(
        registries.FIELD_POLICY, ("task", "summary"),
        {"class": "CONTENT", "cap": 8},
    )
    with pytest.raises(ContractViolation):
        validate_request(_req("task", {
            "work_item_id": "wi_" + "0" * 32, "event": "progress",
            "summary": "longer than eight bytes",
        }))
    monkeypatch.setitem(
        registries.FIELD_POLICY, ("work_item", "body"),
        {"class": "CONTENT", "cap": 8},
    )
    with pytest.raises(ContractViolation):
        validate_request(_req("work_item", {
            "work_item_id": "wi_" + "0" * 32, "title": "t",
            "created_by": "bot:example-fleet/alpha",
            "body": "longer than eight bytes",
        }))
    monkeypatch.setitem(
        registries.FIELD_POLICY, ("communication", "body"),
        {"class": "CONTENT", "cap": 8, "proof": True},
    )
    _, payload = validate_request(_req("communication", _intent_payload(
        body="longer than eight bytes")))
    assert payload.truncated is True and payload.body_bytes > 8


def test_work_item_body_cap_is_bytes():
    fat = "\u00e9" * 10_000        # 10k chars, 20k bytes
    with pytest.raises(ContractViolation):
        validate_request(_req("work_item", {
            "work_item_id": "wi_" + "0" * 32, "title": "t",
            "created_by": "bot:example-fleet/alpha", "body": fat,
        }))


def test_receiver_acknowledged_is_gone():
    from claudlobby.plane.contracts import TASK_EVENTS
    assert "receiver_acknowledged" not in TASK_EVENTS and len(TASK_EVENTS) == 19


def test_task_event_vocabulary_enforced():
    good = {"work_item_id": "wi_" + "0" * 32, "event": "blocked_waiting"}
    env, payload = validate_request(_req("task", good))
    assert payload.event == "blocked_waiting"
    with pytest.raises(ContractViolation):
        validate_request(_req("task", {**good, "event": "blocked"}))


def test_transmission_states():
    good = {
                "msg_id": "msg_" + "0" * 32,
        "attempt_no": 1,
        "carrier": "tmux",
        "destination": "bot:example-fleet/beta",
        "state": "pane_submitted",
    }
    _, payload = validate_request(_req("transmission", good))
    assert payload.state == "pane_submitted"
    with pytest.raises(ContractViolation):
        validate_request(
            _req("transmission", {**good, "state": "delivered"})  # banned word
        )


def test_schemas_export():
    schemas = export_schemas()
    assert "envelope" in schemas and "communication" in schemas
    assert schemas["communication"]["title"] == "Communication"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_contracts.py -v 2>&1 | tail -3`
Expected: FAIL — module not found

- [ ] **Step 3a: Implement registries.py — the policy/registry seeds**

```python
"""Package-owned registries (design §9b census + §11 field policy).

Phase 1 ships FIELD_POLICY (the classification registry — the ENFORCEMENT
source of truth: contracts read caps from here, the capture door reads
CONTENT membership from here; editing a cap HERE changes behavior).
SYSTEM_EVENT_TYPES and METRIC_NAMES join in Phase 2b.
"""

from __future__ import annotations

# (family, field) -> {class: CONTENT|SENSITIVE|DIAGNOSTIC|METADATA,
#                     cap: bytes, proof: keep sha/bytes triple on drop}
FIELD_POLICY: dict[tuple[str, str], dict] = {
    ("communication", "body"): {"class": "CONTENT", "cap": 16_384, "proof": True},
    ("communication", "recipient_raw"): {"class": "SENSITIVE"},
    ("work_item", "body"): {"class": "CONTENT", "cap": 16_384},
    ("task", "summary"): {"class": "CONTENT", "cap": 4_096},
    ("transmission", "destination"): {"class": "SENSITIVE"},   # rides detail
    ("system", "data"): {"class": "DIAGNOSTIC", "cap": 16_384},
}

CONTENT_FIELDS: dict[str, tuple[str, ...]] = {}
for (_family, _field), _pol in FIELD_POLICY.items():
    if _pol["class"] == "CONTENT":
        CONTENT_FIELDS[_family] = CONTENT_FIELDS.get(_family, ()) + (_field,)


def cap_for(family: str, field: str) -> int:
    return FIELD_POLICY[(family, field)]["cap"]
```

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
    field_validator,
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
    # 19 — receiver_acknowledged DELETED (F9 v2.1; recount ruled 2026-08-25: the
    # pre-deletion tuple was 20, mis-stated as 19 — the count error predated the
    # deletion): the transmission ack row is
    # the single acknowledgement fact; activation derives through the join.
    "dispatch_intended", "transmission_failed", "dispatch_submitted",
    "accepted", "rejected", "progress",
    "blocked_waiting", "returned_blocked", "resumed", "completed", "failed",
    "cancelled", "deadline_changed", "superseded", "reassigned",
    "retry_created", "orphaned_by_session_loss", "recovered_after_restart",
    "expired",
)
DECLARATION_EVENTS = ("revision_seen", "scan_completed")
WORKSTREAM_EVENTS = (
    "progressed", "renewed", "blocked", "unblocked", "closed", "archived",
    "plan_linked", "plan_unlinked",
)
CARRIERS = ("tmux", "telegram-tgpost", "telegram-bridge")

# THE kind manifest (F16 v2.1) — the SSOT the DDL CHECK and the INSERT-matrix
# test both derive from. require = NOT NULL for the kind; forbid = must be
# NULL; vocab None = registry-governed (F19: system tokens never CHECK).
_STREAM_COLS = ("event", "carrier", "attempt_no", "carrier_ref", "msg_id",
                "work_item_id", "assignment_id", "workstream_id",
                "subject_kind", "subject_uid", "subject_alias", "actor_uid",
                "session_uid", "severity", "deadline", "successor_id",
                "renewed_until")
KIND_MANIFEST: dict[str, dict] = {
    # require = NOT NULL for the kind; allowed = optional; FORBIDDEN IS
    # DERIVED (round-3 F3): every _STREAM_COLS member not required and not
    # allowed must be NULL — hand-listing forbids is how actor_uid and
    # session_uid escaped round 2.
    "transmission": {
        "vocab": ATTEMPT_STATES,
        "require": ("event", "msg_id", "carrier", "attempt_no"),
        "allowed": ("carrier_ref",),
    },
    "task": {
        "vocab": TASK_EVENTS,
        "require": ("event", "work_item_id"),
        "allowed": ("assignment_id", "actor_uid", "session_uid", "deadline",
                     "successor_id"),
    },
    "workstream": {
        "vocab": WORKSTREAM_EVENTS,
        "require": ("event", "workstream_id"),
        "allowed": ("actor_uid", "renewed_until"),
    },
    "system": {
        "vocab": None,   # registry-governed (F19)
        "require": ("event",),
        "allowed": ("severity",),
        # Round-6 (reviewer's exhaustive-subset probe): the DDL's real
        # semantics are a required PAIR with a conditionally-optional alias —
        # kind+uid must appear together; alias is legal only WITH the pair.
        # (kind+uid, alias NULL) is ACCEPTED; any subset missing part of the
        # anchor is rejected. The round-5 "all-three-as-a-unit" model was
        # wrong about the DDL, which was right.
        "allowed_groups": (
            {"anchor": ("subject_kind", "subject_uid"),
             "dependent": ("subject_alias",)},
        ),
    },
    "declaration": {
        "vocab": DECLARATION_EVENTS,
        "require": ("event", "subject_kind", "subject_uid"),
        "allowed": ("subject_alias",),
    },
}


def kind_forbidden(kind: str) -> tuple[str, ...]:
    manifest = KIND_MANIFEST[kind]
    keep = set(manifest["require"]) | set(manifest["allowed"])
    for group in manifest.get("allowed_groups", ()):
        keep |= set(group["anchor"]) | set(group["dependent"])
    return tuple(c for c in _STREAM_COLS if c not in keep)


FLEET_REQUIRED = {"communication", "work_item", "assignment", "transmission", "task"}

# Field policy lives in plane/registries.py (the design's stated home) and is
# imported here so validators ENFORCE from it — one SSOT, no duplicated caps
# (round-5 F8: descriptive-only policy meant editing a cap changed nothing).
from .registries import CONTENT_FIELDS, FIELD_POLICY  # noqa: E402  (re-export)

# BODY_CAP_BYTES retired (round-6): caps are read from FIELD_POLICY at call time.
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
    # Read the cap from the registry AT CALL TIME (round-6): an import-time
    # constant snapshot made FIELD_POLICY descriptive for communications.
    cap = FIELD_POLICY[("communication", "body")]["cap"]
    if len(raw) <= cap:
        return BodyFields(
            body=stripped, body_bytes=len(raw), body_sha256=digest, truncated=False
        )
    cut = raw[:cap].decode("utf-8", errors="ignore")
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
    assignment_id: Optional[str] = Field(None, pattern=ID_PATTERNS["assignment"])
    workstream_id: Optional[str] = None
    deliberation_id: Optional[str] = None      # Phase-5 seam, reserved
    reply_to_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    supersedes_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    body: Optional[str] = None
    privacy: Literal["metadata", "preview", "full"] = "metadata"
    idempotency_key: Optional[str] = None
    # correlation/causation/trace/span live ONLY on the EmitRequest envelope
    # (round-3 F4): payload duplicates were accepted and silently ignored.
    # Derived at validation from `body`; caller-supplied ONLY by the door's
    # capture policy when the body is withheld (metadata mode keeps the proof
    # triple while dropping content — F23):
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


class Transmission(_Strict):
    msg_id: str = Field(pattern=ID_PATTERNS["msg"])
    attempt_no: int = Field(ge=1)
    carrier: Literal[CARRIERS]
    destination: str
    state: Literal[ATTEMPT_STATES]
    carrier_ref: Optional[str] = None
    error: Optional[str] = None
    part_no: Optional[int] = Field(None, ge=1)     # bridge chunking (round-2 F10)
    part_count: Optional[int] = Field(None, ge=1)


class WorkItem(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    title: str = Field(min_length=1)
    created_by: str                             # alias
    workstream_id: Optional[str] = None         # the WHY axis
    repo: Optional[str] = Field(None, pattern=r"[^/\s]+/[^/\s]+")  # WHERE: owner/name
    project_key: Optional[str] = Field(None, pattern=r"[a-z][a-z0-9-]*")  # projects.yaml slug
    # Authored, not relayed: oversized bodies REJECT — and the cap is BYTES
    # (round-3 F8: max_length counts characters; multibyte text could pass
    # the char cap while exceeding the byte budget).
    body: Optional[str] = None

    @field_validator("body")
    @classmethod
    def _body_byte_cap(cls, v):
        cap = FIELD_POLICY[("work_item", "body")]["cap"]
        if v is not None and len(v.encode("utf-8")) > cap:
            raise ValueError(f"work_item.body exceeds {cap} bytes")
        return v


class Assignment(_Strict):
    assignment_id: str = Field(pattern=ID_PATTERNS["assignment"])
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    assignee: str                               # alias
    assigned_by: str                            # alias
    expected_by: Optional[AwareDatetime] = None
    dispatch_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])


class TaskEvent(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    assignment_id: Optional[str] = Field(None, pattern=ID_PATTERNS["assignment"])
    event: Literal[TASK_EVENTS]
    actor: Optional[str] = None                 # alias: who reported it
    session_uid: Optional[str] = Field(None, pattern=ID_PATTERNS["session"])
    progress: Optional[int] = Field(None, ge=0, le=100)
    summary: Optional[str] = None

    @field_validator("summary")
    @classmethod
    def _summary_byte_cap(cls, v):
        # CONTENT-classified (FIELD_POLICY); authored — over-cap REJECTS.
        cap = FIELD_POLICY[("task", "summary")]["cap"]
        if v is not None and len(v.encode("utf-8")) > cap:
            raise ValueError(f"task summary exceeds {cap} bytes")
        return v
    pr_url: Optional[str] = None
    deadline: Optional[AwareDatetime] = None
    successor_id: Optional[str] = None  # reassigned/retry_created -> assignment_id; superseded -> superseding id


FAMILIES: dict[str, type[BaseModel]] = {
    "communication": Communication,
    "transmission": Transmission,
    "work_item": WorkItem,
    "assignment": Assignment,
    "task": TaskEvent,
}


class EmitRequest(_Strict):
    event_type: str
    occurred_at: Optional[AwareDatetime] = None   # None → emit stamps BEFORE first attempt (F6)
    observed_at: Optional[AwareDatetime] = None   # §4: reporter-of-another-system's-fact only
    emitter: str = Field(min_length=1)
    source_ref: Optional[str] = None
    fleet: Optional[str] = None                   # alias; REQUIRED for FLEET_REQUIRED types
    event_id: Optional[str] = Field(None, pattern=ID_PATTERNS["event"])
    correlation_id: Optional[str] = None          # round-2 F4: the envelope rides the wire
    causation_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    origin: Literal["live", "legacy"] = "live"    # F18 representability
    import_batch: Optional[str] = None
    confidence: Optional[str] = None
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
    if env.event_type in FLEET_REQUIRED and not env.fleet:
        raise ContractViolation(
            [{"loc": ("fleet",), "msg": f"{env.event_type} is fleet-scoped"}]
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
git add claudlobby/plane/registries.py claudlobby/plane/contracts.py tests/test_plane_contracts.py
git commit -m "feat(plane): field-policy registry + wire contracts — envelope, five families, closed vocabularies"
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


def test_file_modes_hardened(tmp_path):
    import os as _os, stat as _stat
    p = db_path(tmp_path)
    connect(p).close()
    assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600
    assert _stat.S_IMODE(_os.stat(p.parent).st_mode) == 0o700


def test_migration_failure_is_atomic(tmp_path, monkeypatch):
    """Round-2 F2: a failing script must leave NO tables and version 0 —
    the script owns BEGIN IMMEDIATE...COMMIT, so partial DDL cannot commit."""
    import claudlobby.plane.migrations as mig
    monkeypatch.setattr(mig, "_migration_files",
        lambda: [(1, "BEGIN IMMEDIATE; CREATE TABLE t1 (x); THIS IS NOT SQL; COMMIT;")])
    c = connect(db_path(tmp_path))
    import pytest as _pytest, sqlite3 as _sq
    with _pytest.raises(_sq.OperationalError):
        mig.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert names == []
    c.close()


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
        "work_items", "assignments", "workstreams", "events",
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


def test_kind_matrix_executed_against_installed_schema():
    """Round-3 F3: EXHAUSTIVE — every vocabulary member accepted; every
    required-field omission rejected; every DERIVED-forbidden column rejected.
    Round 2's hand-listed probes missed subject_kind NULL on declaration and
    off-kind actor/session — derivation closes the class."""
    import sqlite3 as sq

    import pytest as _pytest

    from claudlobby.plane import contracts as c
    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import migrate

    conn = connect(":memory:")
    migrate(conn)

    VALID = {
        "transmission": {"msg_id": "msg_" + "0" * 32, "carrier": "tmux",
                          "attempt_no": 1},
        "task": {"work_item_id": "wi_" + "0" * 32},
        "workstream": {"workstream_id": "ws-x"},
        "system": {},
        "declaration": {"subject_kind": "vault",
                         "subject_uid": "vault_" + "0" * 32},
    }
    FIRST_TOKEN = {"transmission": "send_attempted", "task": "progress",
                   "workstream": "progressed", "system": "restart",
                   "declaration": "revision_seen"}
    FVALS = {"event": "progress", "carrier": "tmux", "attempt_no": 2,
             "carrier_ref": "x", "msg_id": "msg_" + "1" * 32,
             "work_item_id": "wi_" + "1" * 32,
             "assignment_id": "asg_" + "1" * 32, "workstream_id": "ws-y",
             "subject_kind": "actor", "subject_uid": "actor_" + "1" * 32,
             "subject_alias": "bot:f/x", "actor_uid": "actor_" + "2" * 32,
             "session_uid": "sess_" + "1" * 32, "severity": "notice",
             "deadline": "t", "successor_id": "x", "renewed_until": "t"}
    eid = [100]

    def attempt(row: dict):
        # Round-4 F3: the ledger's AUTOINCREMENT assigns ingest_seq — use the
        # cursor's lastrowid (a synthetic counter breaks the FK, which the
        # real connection enforces: foreign_keys=ON). Savepoint per attempt so
        # expected failures leave no ledger residue.
        eid[0] += 1
        conn.execute("SAVEPOINT probe")
        try:
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, 'probe', 't')", (f"ev_{eid[0]:032x}",))
            seq = cur.lastrowid
            cols = {"ingest_seq": seq, "event_id": f"ev_{eid[0]:032x}",
                    "schema_version": "1", "occurred_at": "t",
                    "ingested_at": "t", "host_uid": "h", "emitter": "e", **row}
            names = tuple(cols)
            conn.execute(
                f"INSERT INTO events ({','.join(names)})"
                f" VALUES ({','.join('?' * len(names))})",
                tuple(cols.values()))
        except BaseException:
            conn.execute("ROLLBACK TO probe")
            conn.execute("RELEASE probe")
            raise
        conn.execute("RELEASE probe")

    for kind, manifest in c.KIND_MANIFEST.items():
        base = {"kind": kind, "event": FIRST_TOKEN[kind], **VALID[kind]}
        # 1) EVERY vocabulary member is accepted:
        for token in (manifest["vocab"] or (FIRST_TOKEN[kind],
                                            "brand-new-machinery-type")):
            attempt({**base, "event": token})
        if manifest["vocab"] is not None:
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, "event": "no-such-token"})
        # 2) EVERY required-field omission is rejected (incl. event=None):
        for req in manifest["require"]:
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, req: None})
        # 3) EVERY derived-forbidden column is rejected:
        for col in c.kind_forbidden(kind):
            with _pytest.raises(sq.IntegrityError):
                attempt({**base, col: FVALS[col]})
        # 4) EVERY allowed (optional) column is individually ACCEPTED
        #    (round-4 note: parity claimed exhaustive without proving this half):
        for col in manifest["allowed"]:
            attempt({**base, col: FVALS[col]})
        # 5) allowed GROUPS — EVERY nonempty subset enumerated (round-6):
        #    valid iff the subset contains the full anchor; dependents are
        #    legal only alongside it. For system's 2-anchor+1-dependent
        #    group: 7 subsets → 2 accepted ({kind,uid}, {kind,uid,alias}),
        #    5 rejected. Expected totals: 50 accepted / 82 rejected / 0.
        from itertools import chain, combinations

        GROUP_VALS = {"subject_kind": "actor",
                      "subject_uid": "actor_" + "3" * 32,
                      "subject_alias": "bot:f/g"}
        for group in manifest.get("allowed_groups", ()):
            members = tuple(group["anchor"]) + tuple(group["dependent"])
            anchor = set(group["anchor"])
            for subset in chain.from_iterable(
                combinations(members, n) for n in range(1, len(members) + 1)
            ):
                row = {**base, **{g: GROUP_VALS[g] for g in subset}}
                if anchor <= set(subset):
                    attempt(row)
                else:
                    with _pytest.raises(sq.IntegrityError):
                        attempt(row)

    with _pytest.raises(sq.IntegrityError):     # dead vocabulary stays dead
        attempt({"kind": "task", "work_item_id": "wi_" + "2" * 32,
                 "event": "receiver_acknowledged"})
    conn.close()


def test_envelope_is_17_columns_on_every_lane_b_table():
    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import migrate

    conn = connect(":memory:")
    migrate(conn)
    ENVELOPE = ["ingest_seq", "event_id", "schema_version", "occurred_at",
                "observed_at", "ingested_at", "host_uid", "fleet_uid",
                "emitter", "source_ref", "correlation_id", "causation_id",
                "trace_id", "span_id", "origin", "import_batch", "confidence"]
    for table in ("communications", "work_items", "assignments",
                  "workstreams", "events"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert cols[:17] == ENVELOPE, f"{table} envelope drift: {cols[:17]}"
    conn.close()


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

import os
import sqlite3
from pathlib import Path


def db_path(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "plane.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)            # round-2 F8: dirs 0700
    return p


def connect(path: Path) -> sqlite3.Connection:
    # sqlite creates 0644 by default (probe-confirmed) — pre-create 0600 and
    # re-tighten the WAL/SHM siblings, which are created at their own time.
    if str(path) != ":memory:" and not Path(path).exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=5.0)
    conn.isolation_level = None           # autocommit — ingest/migrate own txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            f = str(path) + suffix
            if os.path.exists(f):
                os.chmod(f, 0o600)
    return conn
```

- [ ] **Step 4: Implement migrations.py**

```python
"""user_version-gated migration runner (F2 + round-2 F2/F6).

Forward-only; scripts own their transactions; downgrade raises DowngradeError
(refused loudly at the CLI, never spooled).
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


class DowngradeError(RuntimeError):
    """db user_version newer than this code — refuse loudly, NEVER spool (F6)."""


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    # Scripts own their transactions (BEGIN IMMEDIATE ... PRAGMA user_version;
    # COMMIT) — round-2 F2: executescript's implicit commit made an outer
    # `with conn` a no-op, committing a partial schema stamped version 0.
    conn.isolation_level = None   # autocommit; the SCRIPT is the transaction
    current = _user_version(conn)
    if current > SCHEMA_USER_VERSION:
        raise DowngradeError(
            f"plane.db user_version={current} is newer than this code"
            f" (supports <={SCHEMA_USER_VERSION}) — refusing downgrade"
        )
    for number, sql in _migration_files():
        if number <= current:
            continue
        try:
            conn.executescript(sql)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raced = _user_version(conn)
            if raced >= number:   # concurrent first emitter applied it — benign
                current = raced
                continue
            raise
        current = _user_version(conn)
        if current != number:
            raise RuntimeError(
                f"migration {number} did not stamp user_version (got {current})"
            )
    return current
```

- [ ] **Step 5: Write migrations/0001_kernel.sql**

```sql
-- 0001_kernel -- the script OWNS its transaction (round-2 F2): executescript
-- runs in autocommit; BEGIN IMMEDIATE serializes concurrent first emitters,
-- and the version stamp commits WITH the DDL or not at all.
BEGIN IMMEDIATE;
-- 0001_kernel: ingest ledger, identity registry, constructs + events stream.
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
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
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
    assignment_id   TEXT,
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
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    work_item_id    TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    created_by_uid  TEXT NOT NULL,
    workstream_id   TEXT,
    repo            TEXT,
    project_key     TEXT,
    body            TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

CREATE TABLE assignments (
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
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    assignment_id TEXT NOT NULL UNIQUE,
    work_item_id    TEXT NOT NULL,
    assignee_uid    TEXT NOT NULL,
    assigned_by_uid TEXT NOT NULL,
    expected_by     TEXT,
    dispatch_msg_id TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_assignments_item ON assignments (work_item_id);
CREATE UNIQUE INDEX idx_assignments_dispatch ON assignments (dispatch_msg_id)
    WHERE dispatch_msg_id IS NOT NULL;

-- workstreams construct pulled into 0001 (round-5 F7): the events stream
-- already declares the workstream KIND here, and the workstream-status
-- reducer (a required §14 bench query) needs the construct to exist. The
-- DOOR and Pydantic contract remain Phase 2b — Phase 1 rows arrive only
-- from the bench seed and tests, via direct SQL.
CREATE TABLE workstreams (
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
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    workstream_id   TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    goal            TEXT,
    owner_uid       TEXT,
    opened_by_uid   TEXT NOT NULL,
    project_key     TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_assignments_assignee ON assignments (assignee_uid, ingest_seq);

-- The ONE events stream (F16-v2.1): everything that HAPPENS to a construct.
-- The CHECK is NULL-safe require-AND-forbid per kind (round-2 F3: SQLite
-- passes NULL CHECK results, so every branch requires its columns NOT NULL
-- and forbids off-kind columns IS NULL). Kinds/vocabularies mirror
-- contracts.KIND_MANIFEST — the INSERT-matrix test executes both sides.
CREATE TABLE events (
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
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    kind            TEXT NOT NULL CHECK (kind IN
                      ('transmission','task','workstream','system','declaration')),
    event           TEXT,
    carrier         TEXT,
    attempt_no      INTEGER,
    carrier_ref     TEXT,
    msg_id          TEXT,
    work_item_id    TEXT,
    assignment_id   TEXT,
    workstream_id   TEXT,
    subject_kind    TEXT,
    subject_uid     TEXT,
    subject_alias   TEXT,
    actor_uid       TEXT,
    session_uid     TEXT,
    severity        TEXT,
    deadline        TEXT,
    successor_id    TEXT,
    renewed_until   TEXT,
    detail          TEXT,
    detail_truncated INTEGER NOT NULL DEFAULT 0,
    CHECK (
        (kind = 'transmission'
            AND event IS NOT NULL AND event IN ('send_attempted','carrier_accepted','pane_submitted',
                          'failed','unknown','recipient_acknowledged',
                          'duplicate_suppressed')
            AND msg_id IS NOT NULL AND carrier IS NOT NULL
            AND carrier IN ('tmux','telegram-tgpost','telegram-bridge')
            AND attempt_no IS NOT NULL
            AND work_item_id IS NULL AND assignment_id IS NULL
            AND workstream_id IS NULL AND subject_kind IS NULL
            AND subject_uid IS NULL AND subject_alias IS NULL
            AND severity IS NULL AND deadline IS NULL
            AND successor_id IS NULL AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL)
     OR (kind = 'task'
            AND event IS NOT NULL AND event IN ('dispatch_intended','transmission_failed',
                          'dispatch_submitted','accepted','rejected','progress',
                          'blocked_waiting','returned_blocked','resumed',
                          'completed','failed','cancelled','deadline_changed',
                          'superseded','reassigned','retry_created',
                          'orphaned_by_session_loss','recovered_after_restart',
                          'expired')
            AND work_item_id IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND workstream_id IS NULL
            AND subject_kind IS NULL AND subject_uid IS NULL
            AND subject_alias IS NULL AND severity IS NULL
            AND renewed_until IS NULL)
     OR (kind = 'workstream'
            AND event IS NOT NULL AND event IN ('progressed','renewed','blocked','unblocked','closed',
                          'archived','plan_linked','plan_unlinked')
            AND workstream_id IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND subject_kind IS NULL
            AND subject_uid IS NULL AND subject_alias IS NULL
            AND severity IS NULL AND deadline IS NULL AND successor_id IS NULL
            AND session_uid IS NULL)
     OR (kind = 'system'
            AND event IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND workstream_id IS NULL
            AND deadline IS NULL AND successor_id IS NULL
            AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL
            AND (severity IS NULL OR severity IN ('critical','notice'))
            AND ((subject_uid IS NULL AND subject_kind IS NULL
                  AND subject_alias IS NULL)
                 OR (subject_uid IS NOT NULL AND subject_kind IS NOT NULL
                     AND subject_kind IN ('host','vault','fleet','actor',
                                          'bot_instance','session'))))
     OR (kind = 'declaration'
            AND event IS NOT NULL AND event IN ('revision_seen','scan_completed')
            AND subject_kind IS NOT NULL
            AND subject_kind IN ('vault','host') AND subject_uid IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND workstream_id IS NULL
            AND severity IS NULL AND deadline IS NULL
            AND successor_id IS NULL AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL)
    ),
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_events_kind_seq ON events (kind, ingest_seq);
CREATE INDEX idx_events_msg ON events (msg_id, ingest_seq) WHERE kind = 'transmission';
CREATE INDEX idx_events_item ON events (work_item_id, ingest_seq) WHERE kind = 'task';
CREATE INDEX idx_events_ws ON events (workstream_id, ingest_seq) WHERE kind = 'workstream';
CREATE INDEX idx_events_subject ON events (subject_uid, ingest_seq) WHERE kind = 'system';
CREATE INDEX idx_events_carrier_ref ON events (carrier_ref) WHERE carrier_ref IS NOT NULL;
CREATE INDEX idx_events_assignment ON events (assignment_id) WHERE assignment_id IS NOT NULL;

PRAGMA user_version = 1;
COMMIT;

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
    # NO transaction management here (round-2 F1): ingest() is the sole
    # transaction owner — a nested `with conn` COMMITTED the outer ledger
    # insert early (probe-confirmed), creating the ledger-without-family
    # sequence that made replay delete a never-stored event. Standalone
    # callers run in autocommit; inside ingest these ride its transaction.
    candidate = mint_uid(kind)
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

    def boom(*a, **k):
        raise RuntimeError("family insert failed")

    monkeypatch.setattr(mod, "_family_values", boom)
    with pytest.raises(RuntimeError):
        ingest(conn, e, p, host_uid=host)
    assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 0
    assert not conn.in_transaction   # rollback left no open transaction


def test_duplicate_with_missing_family_row_raises(env):
    """Round-2 F1: a ledger row whose family row is gone is CORRUPTION —
    replay must refuse duplicate classification, never absorb it."""
    conn, host = env
    from claudlobby.plane.ids import mint_event_id as _mint
    eid = _mint()
    e, p = validate_request(_intent_req(event_id=eid))
    ingest(conn, e, p, host_uid=host)
    conn.execute("DELETE FROM communications WHERE event_id = ?", (eid,))
    again = validate_request(_intent_req(event_id=eid))
    with pytest.raises(RuntimeError, match="divergence"):
        ingest(conn, again[0], again[1], host_uid=host)


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
"""The one transactional write path (design v2 §5; round-2 F1 rewrite).

ingest_many() is the SOLE transaction owner: BEGIN IMMEDIATE, ledger+family
inserts for every item, COMMIT — helpers never manage transactions (the
nested-`with` early commit is the probe-confirmed lost-event class). Every
INSERT is built from a column dict, so the placeholder count is right by
construction — hand arithmetic is banned. Duplicate event_id replay is
SUCCESS only after verifying ledger AND family rows both exist; a ledger row
without its family row is corruption and RAISES, never absorbs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import PLANE_SCHEMA_VERSION
from .contracts import (
    Assignment,
    Communication,
    TaskEvent,
    Transmission,
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


_CONSTRUCT_TABLE = {
    "communication": "communications",
    "work_item": "work_items",
    "assignment": "assignments",
}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    cols = tuple(values)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' * len(cols))})",
        tuple(values.values()),
    )


def _envelope(seq, event_id, env, *, host_uid, fleet_uid, now) -> dict:
    return {
        "ingest_seq": seq,
        "event_id": event_id,
        "schema_version": PLANE_SCHEMA_VERSION,
        "occurred_at": env.occurred_at.isoformat() if env.occurred_at else now,
        "observed_at": env.observed_at.isoformat() if env.observed_at else None,
        "ingested_at": now,
        "host_uid": host_uid,
        "fleet_uid": fleet_uid,
        "emitter": env.emitter,
        "source_ref": env.source_ref,
        "correlation_id": env.correlation_id,
        "causation_id": env.causation_id,
        "trace_id": env.trace_id,
        "span_id": env.span_id,
        "origin": env.origin,
        "import_batch": env.import_batch,
        "confidence": env.confidence,
    }


def _family_values(conn, payload, now) -> tuple[str, dict]:
    """(table, family-column dict) — no SQL here; _insert builds it."""
    if isinstance(payload, Communication):
        return "communications", {
            "msg_id": payload.msg_id,
            "sender_uid": resolve_party(conn, payload.sender, now),
            "sender_alias": payload.sender,
            "sender_session_uid": payload.sender_session_uid,
            "recipient_uid": (
                resolve_party(conn, payload.recipient, now)
                if payload.recipient else None
            ),
            "recipient_alias": payload.recipient,
            "recipient_raw": payload.recipient_raw,
            "message_class": payload.message_class,
            "command_type": payload.command_type,
            "work_item_id": payload.work_item_id,
            "assignment_id": payload.assignment_id,
            "workstream_id": payload.workstream_id,
            "deliberation_id": payload.deliberation_id,
            "reply_to_msg_id": payload.reply_to_msg_id,
            "supersedes_msg_id": payload.supersedes_msg_id,
            "body": payload.body,
            "body_bytes": payload.body_bytes,
            "body_sha256": payload.body_sha256,
            "truncated": int(payload.truncated),
            "privacy": payload.privacy,
            "idempotency_key": payload.idempotency_key,
        }
    if isinstance(payload, WorkItem):
        return "work_items", {
            "work_item_id": payload.work_item_id,
            "title": payload.title,
            "created_by_uid": resolve_party(conn, payload.created_by, now),
            "workstream_id": payload.workstream_id,
            "repo": payload.repo,
            "project_key": payload.project_key,
            "body": payload.body,
        }
    if isinstance(payload, Assignment):
        return "assignments", {
            "assignment_id": payload.assignment_id,
            "work_item_id": payload.work_item_id,
            "assignee_uid": resolve_party(conn, payload.assignee, now),
            "assigned_by_uid": resolve_party(conn, payload.assigned_by, now),
            "expected_by": (
                payload.expected_by.isoformat() if payload.expected_by else None
            ),
            "dispatch_msg_id": payload.dispatch_msg_id,
        }
    if isinstance(payload, Transmission):
        detail = {
            k: v for k, v in {
                "destination": payload.destination,
                "error": payload.error,
                "part_no": payload.part_no,
                "part_count": payload.part_count,
            }.items() if v is not None
        }
        return "events", {
            "kind": "transmission",
            "event": payload.state,
            "carrier": payload.carrier,
            "attempt_no": payload.attempt_no,
            "carrier_ref": payload.carrier_ref,
            "msg_id": payload.msg_id,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    if isinstance(payload, TaskEvent):
        detail = {
            k: v for k, v in {
                "progress": payload.progress,
                "summary": payload.summary,
                "pr_url": payload.pr_url,
            }.items() if v is not None
        }
        return "events", {
            "kind": "task",
            "event": payload.event,
            "work_item_id": payload.work_item_id,
            "assignment_id": payload.assignment_id,
            "actor_uid": (
                resolve_party(conn, payload.actor, now) if payload.actor else None
            ),
            "session_uid": payload.session_uid,
            "deadline": payload.deadline.isoformat() if payload.deadline else None,
            "successor_id": payload.successor_id,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    raise TypeError(f"no insert mapping for {type(payload).__name__}")


def ingest_many(conn, items, *, host_uid) -> list[IngestResult]:
    """items: [(EmitRequest, payload)] — ONE transaction, all-or-nothing."""
    now = now_iso()
    prepared = [
        (env.event_id or mint_event_id(), env, payload)
        for env, payload in items
    ]
    try:
        conn.execute("BEGIN IMMEDIATE")
        results = []
        for event_id, env, payload in prepared:
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, ?, ?)",
                (event_id, env.event_type, now),
            )
            seq = cur.lastrowid
            fleet_uid = resolve_fleet(conn, env.fleet, now) if env.fleet else None
            base = _envelope(
                seq, event_id, env,
                host_uid=host_uid, fleet_uid=fleet_uid, now=now,
            )
            table, fam = _family_values(conn, payload, now)
            _insert(conn, table, {**base, **fam})
            results.append(IngestResult(event_id, seq, False))
        conn.execute("COMMIT")
        return results
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "ingest_ledger.event_id" in str(exc):
            return _verify_duplicates(conn, prepared)
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _verify_duplicates(conn, prepared) -> list[IngestResult]:
    """Duplicate replay = success ONLY if every event landed FULLY before:
    ledger row present AND family row present (round-2 F1 — never report
    duplicate success for an event that was never fully stored)."""
    results = []
    for event_id, env, payload in prepared:
        ledger = conn.execute(
            "SELECT family FROM ingest_ledger WHERE event_id = ?", (event_id,)
        ).fetchone()
        if ledger is None:
            raise RuntimeError(
                f"duplicate-classification refused: {event_id} missing from"
                " ledger while the batch collided — mixed state"
            )
        family = ledger["family"]
        table = _CONSTRUCT_TABLE.get(family, "events")
        fam = conn.execute(
            f"SELECT 1 FROM {table} WHERE event_id = ?", (event_id,)
        ).fetchone()
        if fam is None:
            raise RuntimeError(
                f"ledger/family divergence for {event_id} — refusing"
                " duplicate classification (integrity, not idempotency)"
            )
        results.append(IngestResult(event_id, None, True))
    return results


def ingest(conn, env, payload, *, host_uid) -> IngestResult:
    return ingest_many(conn, [(env, payload)], host_uid=host_uid)[0]
```


- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/pytest tests/test_plane_ingest.py -v 2>&1 | tail -3`
Expected: all PASS.

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
- Produces: `spool_dir(root) -> Path` (`<root>/state/plane/spool/`, quarantine subdir); `spool_write(root, finalized_requests: list[dict], error: str) -> Path` (fsynced file+dir, 0600; JSON: `{event_ids, spooled_at, error, attempts, requests}` — event ids and occurred_at are finalized by emit BEFORE the first db attempt); `drain(root, conn, host_uid) -> DrainReport` dataclass `{ingested: int, duplicates: int, quarantined: int, remaining: int}`; `spool_entries(root) -> list[dict]` (listing with age); `quarantine(root, name) -> Path`; `MAX_ATTEMPTS = 5`.

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


def _fin(req: dict, eid: str) -> dict:
    return {**req, "event_id": eid, "occurred_at": "2026-08-24T00:00:00+00:00"}


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield tmp_path, conn, host
    conn.close()


def test_spool_write_is_fsynced_0600_json(env):
    root, conn, host = env
    eid = mint_event_id()
    p = spool_write(root, [_fin(_req(), eid)], "db locked")
    import os as _os, stat as _stat
    assert p.parent == spool_dir(root)
    assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600
    data = json.loads(p.read_text())
    assert data["event_ids"] == [eid] and data["attempts"] == 0
    assert data["requests"][0]["occurred_at"]  # F6: event time survives the spool
    assert not list(spool_dir(root).glob("*.tmp"))


def test_drain_ingests_and_deletes(env):
    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "db locked")
    report = drain(root, conn, host)
    assert report.ingested == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    assert spool_entries(root) == []


def test_drain_duplicate_is_success(env):
    root, conn, host = env
    eid = mint_event_id()
    spool_write(root, [_fin(_req(), eid)], "x")
    drain(root, conn, host)
    spool_write(root, [_fin(_req(), eid)], "x")
    report = drain(root, conn, host)
    assert report.duplicates == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1


def test_malformed_spool_file_quarantined(env):
    root, conn, host = env
    (spool_dir(root) / "garbage.json").write_text("{not json")
    report = drain(root, conn, host)
    assert report.quarantined == 1
    assert list(quarantine_dir(root).iterdir())


def test_contract_violation_quarantined_not_retried(env):
    root, conn, host = env
    req = _req()
    req["payload"]["message_class"] = "no-such-class"
    spool_write(root, [_fin(req, mint_event_id())], "x")
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0


def test_operational_errors_retry_then_quarantine(env, monkeypatch):
    """Only sqlite3.OperationalError is retryable (round-2 F6)."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def busy(*a, **k):
        raise sq.OperationalError("database is locked")

    monkeypatch.setattr(mod, "ingest_many", busy)
    for _ in range(MAX_ATTEMPTS):
        drain(root, conn, host)
    assert spool_entries(root) == []
    assert len(list(quarantine_dir(root).glob("*.json"))) == 1


def test_quarantine_artifacts_are_0600(env):
    """Round-4 F6: reason sidecars and MOVED files both end 0600, whatever
    mode the malformed file arrived with."""
    import os as _os, stat as _stat

    root, conn, host = env
    bad = spool_dir(root) / "garbage.json"
    bad.write_text("{not json")          # arrives 0644 by umask — the trap
    drain(root, conn, host)
    q = quarantine_dir(root)
    moved = q / "garbage.json"
    sidecar = q / "garbage.json.reason"
    assert _stat.S_IMODE(_os.stat(moved).st_mode) == 0o600
    assert _stat.S_IMODE(_os.stat(sidecar).st_mode) == 0o600


def test_sql_bug_operational_errors_quarantine_not_retry(env, monkeypatch):
    """Round-5 F6: EXACT assertion — a code-less 'no such table' (the
    synthetic/3.10 form) quarantines immediately, never retries. The round-4
    either/or blessed the wrong path."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def missing_table(*a, **k):
        raise sq.OperationalError("no such table: events")   # code=None

    monkeypatch.setattr(mod, "ingest_many", missing_table)
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0


def test_codeless_infra_message_still_retries(env, monkeypatch):
    """The fallback's other half: a code-less LOCKED message retries."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def locked(*a, **k):
        raise sq.OperationalError("database is locked")      # code=None

    monkeypatch.setattr(mod, "ingest_many", locked)
    report = drain(root, conn, host)
    assert report.remaining == 1 and report.quarantined == 0
    assert json.loads(next(spool_dir(root).glob("*.json")).read_text())["attempts"] == 1


def test_retry_rewrite_preserves_0600(env, monkeypatch):
    """Round-3 F6: the executed probe caught retry rewrites at 0644."""
    import os as _os, stat as _stat, sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def busy(*a, **k):
        raise sq.OperationalError("database is locked")

    monkeypatch.setattr(mod, "ingest_many", busy)
    drain(root, conn, host)
    f = next(spool_dir(root).glob("*.json"))
    assert _stat.S_IMODE(_os.stat(f).st_mode) == 0o600
    assert json.loads(f.read_text())["attempts"] == 1


def test_non_retryable_quarantines_immediately(env, monkeypatch):
    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def poison(*a, **k):
        raise RuntimeError("ledger/family divergence")

    monkeypatch.setattr(mod, "ingest_many", poison)
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0
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
from .ingest import ingest_many  # patched in tests; keep module-level name

MAX_ATTEMPTS = 5


class SpoolWriteError(RuntimeError):
    """db failed AND the spool write failed — total emit failure (exit 3)."""


def spool_dir(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "spool"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def quarantine_dir(root: Path) -> Path:
    p = spool_dir(root) / "quarantine"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _write_bytes_secure(directory: Path, name: str, data: bytes) -> Path:
    """THE one spool byte-writer (round-3/4 F6): 0600, atomic tmp+rename,
    fsync file AND directory — entries and reason sidecars alike."""
    target = directory / name
    tmp = directory / (name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return target


RETRYABLE_SQLITE_CODES = frozenset({
    5,   # SQLITE_BUSY
    6,   # SQLITE_LOCKED
    8,   # SQLITE_READONLY  (transient perms; also the e2e test's class)
    10,  # SQLITE_IOERR
    13,  # SQLITE_FULL
    14,  # SQLITE_CANTOPEN
})


_RETRYABLE_MESSAGES = (
    # The code-less fallback (Python 3.10, synthetic exceptions): match the
    # KNOWN infrastructure classes; anything else is presumed a bug and
    # quarantines loudly (round-5 F6 — retry-everything blessed SQL bugs).
    "database is locked",
    "database table is locked",
    "database or disk is full",
    "disk i/o error",
    "unable to open database",
    "attempt to write a readonly database",
)


def is_retryable(exc: sqlite3.OperationalError) -> bool:
    """Whitelist by SQLite primary error code; when no code exists (3.10 or a
    synthetic exception), fall back to message-matching the known infra
    classes — never retry-by-default."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in RETRYABLE_SQLITE_CODES
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MESSAGES)


def _write_entry_file(directory: Path, name: str, entry: dict) -> Path:
    return _write_bytes_secure(
        directory, name, (json.dumps(entry, ensure_ascii=False) + "\n").encode()
    )


def spool_write(root: Path, finalized_requests: list[dict], error: str) -> Path:
    """Persist an already-finalized batch (event_ids + occurred_at set by emit
    BEFORE the first db attempt — F6). fsync file AND directory before
    returning: a spool 'success' that evaporates on power loss is a lost
    event wearing a receipt (round-2 F6)."""
    lead = finalized_requests[0]["event_id"]
    entry = {
        "event_ids": [r["event_id"] for r in finalized_requests],
        "spooled_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "attempts": 0,
        "requests": finalized_requests,
    }
    try:
        return _write_entry_file(spool_dir(root), f"{lead}.json", entry)
    except OSError as exc:
        raise SpoolWriteError(f"db failed ({error}) AND spool failed ({exc})") from exc


def spool_entries(root: Path) -> list[dict]:
    out = []
    for f in sorted(spool_dir(root).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            data = {"event_ids": None, "spooled_at": None, "attempts": None}
        data["_file"] = f.name
        out.append(data)
    return out


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def quarantine_entry(root: Path, f: Path, reason: str) -> None:
    """THE quarantine door — drain and the operator CLI both use it.
    Round-5 F6: a cross-directory rename dirties BOTH directories; fsync
    source AND destination, or a crash can resurrect the entry in spool
    (double-processing) or lose it from quarantine."""
    q = quarantine_dir(root)
    _write_bytes_secure(q, f.name + ".reason", (reason + "\n").encode())
    os.chmod(f, 0o600)          # a malformed file arrived at ITS creator's mode
    os.replace(f, q / f.name)
    _fsync_dir(q)
    _fsync_dir(f.parent)


def _quarantine(root: Path, f: Path, reason: str) -> None:
    quarantine_entry(root, f, reason)


@dataclass(frozen=True)
class DrainReport:
    ingested: int = 0
    duplicates: int = 0
    quarantined: int = 0
    remaining: int = 0


def drain(root: Path, conn: sqlite3.Connection, host_uid: str) -> DrainReport:
    ingested = duplicates = quarantined = 0
    entries = []
    for f in spool_dir(root).glob("*.json"):
        try:
            data = json.loads(f.read_text())
            entries.append((data.get("spooled_at") or "", f, data))
        except (json.JSONDecodeError, OSError) as exc:
            _quarantine(root, f, f"malformed spool file: {exc}")
            quarantined += 1
    for _, f, entry in sorted(entries, key=lambda e: (e[0], e[1].name)):
        try:
            raws = entry["requests"]
        except (KeyError, TypeError) as exc:
            _quarantine(root, f, f"malformed spool entry: {exc}")
            quarantined += 1
            continue
        try:
            items = [validate_request(r) for r in raws]
        except ContractViolation as exc:
            _quarantine(root, f, f"contract violation on drain: {exc}")
            quarantined += 1
            continue
        try:
            results = ingest_many(conn, items, host_uid=host_uid)
        except sqlite3.OperationalError as exc:
            if not is_retryable(exc):
                # Missing table / SQL typo are OperationalError too — bugs,
                # not infrastructure (round-4 F6).
                _quarantine(root, f, f"non-retryable operational: {exc}")
                quarantined += 1
                continue
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["error"] = str(exc)
            if entry["attempts"] >= MAX_ATTEMPTS:
                _quarantine_with(root, f, entry, f"retries exhausted: {exc}")
                quarantined += 1
            else:
                _write_entry_file(spool_dir(root), f.name, entry)
            continue
        except Exception as exc:  # noqa: BLE001 — integrity/programming: poison
            _quarantine(root, f, f"non-retryable on drain: {exc}")
            quarantined += 1
            continue
        if all(r.duplicate for r in results):
            duplicates += 1
        else:
            ingested += 1
        f.unlink()  # only after committed ingestion
    remaining = len(list(spool_dir(root).glob("*.json")))
    return DrainReport(ingested, duplicates, quarantined, remaining)


def _quarantine_with(root: Path, f: Path, entry: dict, reason: str) -> None:
    _write_entry_file(spool_dir(root), f.name, entry)
    _quarantine(root, f, reason)
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
  - `emit_api.emit(root, raw) -> EmitOutcome` and `emit_batch(root, raws) -> list[EmitOutcome]` (the atomic unit of work, F4) — dataclass `{event_id, status: Literal["committed","duplicate","spooled"], detail: str | None}`. Flow: validate (ContractViolation propagates — NEVER spooled) → connect+migrate → ingest; ONLY an `sqlite3.OperationalError` that `is_retryable()` accepts spools — other database errors propagate (emit) or quarantine (drain).
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


def test_capture_modes_per_family(tmp_path: Path):
    """Round-3 F8: metadata/full behavior for EVERY content family."""
    import json as _json

    cap = tmp_path / "state" / "plane"
    cap.mkdir(parents=True)
    (cap / "capture.json").write_text('{"*": "metadata"}')
    # communication: body dropped, proof triple kept
    r = _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
             stdin=_intent_json())
    assert r.returncode == 0, r.stderr
    from claudlobby.plane.db import connect, db_path
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT body, body_sha256, privacy FROM communications").fetchone()
    assert row["body"] is None and row["body_sha256"] and row["privacy"] == "metadata"
    # work_item: body dropped silently
    wi = {"event_type": "work_item", "emitter": "t", "fleet": "example-fleet",
          "payload": {"work_item_id": "wi_" + "5" * 32, "title": "x",
                       "created_by": "bot:example-fleet/alpha", "body": "secret"}}
    r = _run(["--root", str(tmp_path), "emit", "work_item", "--json", "-"],
             stdin=_json.dumps(wi))
    assert r.returncode == 0, r.stderr
    assert conn.execute("SELECT body FROM work_items").fetchone()["body"] is None
    # task: summary dropped in metadata mode
    te = {"event_type": "task", "emitter": "t", "fleet": "example-fleet",
          "payload": {"work_item_id": "wi_" + "5" * 32, "event": "progress",
                       "summary": "secret detail"}}
    r = _run(["--root", str(tmp_path), "emit", "task", "--json", "-"],
             stdin=_json.dumps(te))
    assert r.returncode == 0, r.stderr
    detail = conn.execute("SELECT detail FROM events WHERE kind='task'").fetchone()["detail"]
    assert detail is None or "secret" not in detail
    # full mode: EVERY content family survives (round-5 F8 — task alone
    # was tested; communication body and work_item body now asserted too)
    (cap / "capture.json").write_text('{"*": "full"}')
    comm2 = _json.loads(_intent_json())
    comm2["payload"]["msg_id"] = "msg_" + "6" * 32
    comm2["payload"]["body"] = "full-mode communication body"
    _run(["--root", str(tmp_path), "emit", "communication", "--json", "-"],
         stdin=_json.dumps(comm2))
    row2 = conn.execute(
        "SELECT body, privacy FROM communications ORDER BY ingest_seq DESC"
    ).fetchone()
    assert row2["body"] == "full-mode communication body" and row2["privacy"] == "full"
    wi2 = {**wi, "payload": {**wi["payload"], "work_item_id": "wi_" + "6" * 32,
                              "body": "full-mode objective body"}}
    _run(["--root", str(tmp_path), "emit", "work_item", "--json", "-"],
         stdin=_json.dumps(wi2))
    assert conn.execute(
        "SELECT body FROM work_items ORDER BY ingest_seq DESC"
    ).fetchone()["body"] == "full-mode objective body"
    te2 = {**te, "payload": {**te["payload"], "summary": "kept"}}
    _run(["--root", str(tmp_path), "emit", "task", "--json", "-"], stdin=_json.dumps(te2))
    kept = conn.execute(
        "SELECT detail FROM events WHERE kind='task' ORDER BY ingest_seq DESC"
    ).fetchone()["detail"]
    conn.close()
    assert kept and "kept" in kept


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
    assert "envelope" in data and "task" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/pytest tests/test_plane_cli.py -v 2>&1 | tail -4`
Expected: FAIL — argparse error (unknown command `emit`)

- [ ] **Step 3: Implement emit_api.py**

```python
"""emit(): the programmatic spine every writer uses (design v2 §5; round-2 v2.1).

Failure taxonomy is the contract:
  ContractViolation  -> caller bug: propagate, write NOTHING (not even spool)
  DowngradeError     -> db newer than code: propagate LOUDLY, never spooled
  sqlite Operational/Database errors -> spool + report spooled
  spool also failed  -> SpoolWriteError (CLI exit 3)

occurred_at is finalized BEFORE the first db attempt (round-2 F6) so a
spooled replay preserves event time and spool lag stays measurable as
ingested_at - occurred_at. Capture policy is resolved from plane config
keyed by fleet — NEVER from the caller's request (F23): in metadata mode
the body is dropped at the door with its proof triple retained.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from .contracts import (
    CONTENT_FIELDS,
    ContractViolation,
    cap_body,
    validate_request,
)
from .db import connect, db_path
from .ids import ensure_host_uid, mint_event_id
from .ingest import ingest_many
from .migrations import DowngradeError, migrate
from .spool import SpoolWriteError, is_retryable, spool_write


@dataclass(frozen=True)
class EmitOutcome:
    event_id: str
    status: Literal["committed", "duplicate", "spooled"]
    detail: Optional[str] = None


def _capture_mode(root: Path, fleet: str | None) -> str:
    """Fleet-keyed capture mode from plane config; default 'metadata' (F7/F23).
    The caller's request never decides this."""
    cfg = Path(root) / "state" / "plane" / "capture.json"
    try:
        modes = json.loads(cfg.read_text())
    except (OSError, json.JSONDecodeError):
        return "metadata"
    mode = modes.get(fleet or "", modes.get("*", "metadata"))
    return mode if mode in ("full", "metadata") else "metadata"


def _apply_capture(root: Path, raw: dict) -> dict:
    """Round-3 F8: the policy transforms EVERY content-bearing family
    (contracts.CONTENT_FIELDS is the registry's code form), not
    communications alone. Communications keep the proof triple on drop."""
    fields = CONTENT_FIELDS.get(raw.get("event_type"))
    if not fields:
        return raw
    mode = _capture_mode(root, raw.get("fleet"))
    payload = dict(raw.get("payload") or {})
    if raw.get("event_type") == "communication":
        if mode == "full":
            payload["privacy"] = "full"
        else:
            body = payload.get("body")
            payload["privacy"] = "metadata"
            if body is not None:
                proof = cap_body(body)
                payload["body"] = None      # dropped AT THE DOOR (F23)
                payload["body_bytes"] = proof.body_bytes
                payload["body_sha256"] = proof.body_sha256
                payload["truncated"] = proof.truncated
    elif mode != "full":
        for field in fields:
            payload.pop(field, None)        # dropped, no proof triple owed
    return {**raw, "payload": payload}


def _finalize(raw: dict) -> dict:
    out = dict(raw)
    if not out.get("event_id"):
        out["event_id"] = mint_event_id()
    if not out.get("occurred_at"):
        out["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return out


def emit_batch(root: Path, raw_requests: list[dict]) -> list[EmitOutcome]:
    """One atomic unit of work: validate ALL, then ONE transaction (F4).
    The dispatch door commits work_item + assignment + communication here."""
    finalized = [_finalize(_apply_capture(root, r)) for r in raw_requests]
    items = [validate_request(r) for r in finalized]   # ContractViolation propagates
    try:
        conn = connect(db_path(root))
        try:
            migrate(conn)                               # DowngradeError propagates
            host = ensure_host_uid(Path(root) / "state")
            results = ingest_many(conn, items, host_uid=host)
        finally:
            conn.close()
    except (DowngradeError, ContractViolation):
        raise
    except sqlite3.OperationalError as exc:
        # Spool ONLY whitelisted-retryable codes (round-4 F6): IntegrityError
        # never lands here (a bug, propagates), and a missing table / SQL typo
        # — OperationalError but equally bugs — propagate loudly too.
        if not is_retryable(exc):
            raise
        path = spool_write(root, finalized, str(exc))   # raises SpoolWriteError
        return [
            EmitOutcome(r["event_id"], "spooled", detail=str(path))
            for r in finalized
        ]
    return [
        EmitOutcome(res.event_id, "duplicate" if res.duplicate else "committed")
        for res in results
    ]


def emit(root: Path, raw_request: dict) -> EmitOutcome:
    return emit_batch(root, [raw_request])[0]
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
from ..plane.emit_api import emit, emit_batch
from ..plane.identity import provisional_actors
from ..plane.ids import ensure_host_uid
from ..plane.migrations import DowngradeError, migrate
from ..plane.spool import (
    SpoolWriteError, drain, quarantine_dir, spool_dir, spool_entries,
)

_FAMILY_COUNTS = {
    "communication": ("communications", None),
    "transmission": ("events", "transmission"),
    "work_item": ("work_items", None),
    "assignment": ("assignments", None),
    "task": ("events", "task"),
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
    except DowngradeError as exc:
        # Never spooled (round-2 F6): a newer db is an operator condition,
        # not transient infrastructure — retrying it forever helps no one.
        print(f"emit: REFUSED — {exc}", file=sys.stderr)
        return 4
    print(outcome.event_id)
    if outcome.status == "spooled":
        print(f"plane: db unavailable — spooled {outcome.detail}", file=sys.stderr)
    return 0


def cmd_emit_batch(args, root: Path) -> int:
    """One atomic unit of work: {"events": [...]} or a bare JSON array (F4)."""
    try:
        raw = sys.stdin.read() if args.json == "-" else Path(args.json).read_text()
        parsed = json.loads(raw)
        requests = parsed["events"] if isinstance(parsed, dict) else parsed
        assert isinstance(requests, list) and requests
    except (OSError, json.JSONDecodeError, KeyError, AssertionError) as exc:
        print(f"emit-batch: unreadable request: {exc}", file=sys.stderr)
        return 2
    try:
        outcomes = emit_batch(root, requests)
    except ContractViolation as exc:
        first = exc.errors[0] if exc.errors else {}
        print(f"emit-batch: contract violation: {first}", file=sys.stderr)
        return 2
    except SpoolWriteError as exc:
        print(f"emit-batch: TOTAL FAILURE — {exc}", file=sys.stderr)
        return 3
    except DowngradeError as exc:
        print(f"emit-batch: REFUSED — {exc}", file=sys.stderr)
        return 4
    for o in outcomes:
        print(o.event_id)
    if outcomes and outcomes[0].status == "spooled":
        print(f"plane: db unavailable — spooled {outcomes[0].detail}", file=sys.stderr)
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
            for family, (table, kind) in _FAMILY_COUNTS.items():
                if kind is None:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                else:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)
                    ).fetchone()[0]
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
            print(f"{e['_file']}  events={e.get('event_ids')}  attempts={e.get('attempts')}")
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
        import re as _re

        if not _re.fullmatch(r"ev_[0-9a-f]{32}\.json", args.name or ""):
            # Round-2 F9: the name is a filesystem operand — only validated
            # spool basenames, never path components.
            print(f"invalid spool entry name: {args.name!r}", file=sys.stderr)
            return 1
        src = spool_dir(root) / args.name
        if not src.exists():
            print(f"no such spool entry: {args.name}", file=sys.stderr)
            return 1
        import os

        from ..plane.spool import quarantine_entry

        quarantine_entry(root, src, "operator")
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
    pe.add_argument("event_type", help="communication | transmission | work_item | assignment | task")
    pe.add_argument("--json", required=True, help="Request JSON path, or '-' for stdin")
    pe.set_defaults(func=cmd_emit)

    peb = sub.add_parser("emit-batch", help="Atomic multi-event unit of work (F4)")
    peb.add_argument("--json", required=True, help='{"events": [...]} path, or "-"')
    peb.set_defaults(func=cmd_emit_batch)

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
        "event_type": "task",
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
    committed = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'task'").fetchone()[0]
    spooled = len(list((tmp_path / "state" / "plane" / "spool").glob("*.json")))
    conn.close()
    assert committed + spooled == 25  # nothing lost
    # Ordering authority: seqs are gapless 1..N for committed rows
    conn = connect(db_path(tmp_path))
    seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY 1")]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM ingest_ledger l WHERE NOT EXISTS"
        " (SELECT 1 FROM events e WHERE e.event_id = l.event_id)"
    ).fetchone()[0]
    conn.close()
    assert seqs == list(range(1, committed + 1))
    assert orphans == 0   # round-2 F1: ledger↔family 1:1 holds under load


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


def test_derivation_fixtures(tmp_path: Path):
    """Round-4 F7: the reviewer's reassignment counterexample, as a fixture.
    asg1 acked+reassigned (terminal, successor asg2); asg2 acked and OVERDUE.
    Attention must return asg2 and never asg1; task-status must be terminal-
    dominant (late progress after completed does not reopen)."""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.ids import (
        mint_assignment_id, mint_msg_id, mint_work_item_id,
    )
    from claudlobby.plane.migrations import migrate

    conn = connect(db_path(tmp_path)); migrate(conn); conn.close()
    ensure_host_uid(tmp_path / "state")
    wi, a1, a2 = mint_work_item_id(), mint_assignment_id(), mint_assignment_id()
    m1, m2 = mint_msg_id(), mint_msg_id()

    def tx(msg, state):
        return {"event_type": "transmission", "emitter": "fx",
                "fleet": "fx-fleet",
                "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                             "destination": "sock", "state": state}}

    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "title": "t",
                      "created_by": "bot:fx-fleet/mgr"}},
        {"event_type": "assignment", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"assignment_id": a1, "work_item_id": wi,
                      "assignee": "bot:fx-fleet/w1",
                      "assigned_by": "bot:fx-fleet/mgr",
                      "expected_by": "2026-01-01T00:00:00+00:00",
                      "dispatch_msg_id": m1}},
        {"event_type": "communication", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"msg_id": m1, "sender": "bot:fx-fleet/mgr",
                      "recipient": "bot:fx-fleet/w1",
                      "message_class": "task_request", "command_type": "task",
                      "work_item_id": wi, "assignment_id": a1,
                      "privacy": "full"}},
        tx(m1, "pane_submitted"), tx(m1, "recipient_acknowledged"),
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a1,
                      "event": "reassigned", "successor_id": a2}},
        {"event_type": "assignment", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"assignment_id": a2, "work_item_id": wi,
                      "assignee": "bot:fx-fleet/w2",
                      "assigned_by": "bot:fx-fleet/mgr",
                      "expected_by": "2026-01-01T00:00:00+00:00",
                      "dispatch_msg_id": m2}},
        {"event_type": "communication", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"msg_id": m2, "sender": "bot:fx-fleet/mgr",
                      "recipient": "bot:fx-fleet/w2",
                      "message_class": "task_request", "command_type": "task",
                      "work_item_id": wi, "assignment_id": a2,
                      "privacy": "full"}},
        tx(m2, "pane_submitted"), tx(m2, "recipient_acknowledged"),
    ])
    conn = connect(db_path(tmp_path))
    TERMINAL = ("'completed','failed','cancelled','returned_blocked',"
                "'superseded','reassigned','expired'")
    attention = [r[0] for r in conn.execute(
        "SELECT a.assignment_id FROM assignments a"
        " WHERE NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
        f"  AND t.assignment_id = a.assignment_id AND t.event IN ({TERMINAL}))"
        " AND (NOT EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
        "  AND e.msg_id = a.dispatch_msg_id"
        "  AND e.event='recipient_acknowledged')"
        "  OR a.expected_by < '2026-06-01')")]
    assert attention == [a2], f"attention must surface ONLY the successor: {attention}"
    # terminal dominance: complete a2, then a late progress must not reopen
    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a2,
                      "event": "completed"}},
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a2,
                      "event": "progress", "progress": 10}},
    ])
    status = conn.execute(
        "SELECT COALESCE((SELECT t.event FROM events t WHERE t.kind='task'"
        f" AND t.assignment_id = ? AND t.event IN ({TERMINAL})"
        " ORDER BY t.ingest_seq LIMIT 1),"
        " (SELECT t.event FROM events t WHERE t.kind='task'"
        "  AND t.assignment_id = ? ORDER BY t.ingest_seq DESC LIMIT 1),"
        " 'open')", (a2, a2)).fetchone()[0]
    conn.close()
    assert status == "completed", f"terminal must dominate late progress: {status}"


WORKSTREAM_REDUCER_SQL = (
    "SELECT w.workstream_id, CASE"
    " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
    "   AND c.workstream_id = w.workstream_id AND c.event='archived')"
    "   THEN 'archived'"
    " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
    "   AND c.workstream_id = w.workstream_id AND c.event='closed')"
    "   THEN 'closed'"
    " WHEN (SELECT e.event FROM events e WHERE e.kind='workstream'"
    "   AND e.workstream_id = w.workstream_id"
    "   AND e.event IN ('blocked','unblocked')"
    "   ORDER BY e.ingest_seq DESC LIMIT 1) = 'blocked' THEN 'blocked'"
    " WHEN COALESCE((SELECT e.renewed_until FROM events e"
    "   WHERE e.kind='workstream' AND e.event='renewed'"
    "   AND e.workstream_id = w.workstream_id"
    "   ORDER BY e.ingest_seq DESC LIMIT 1), '') < ?"
    "  AND COALESCE((SELECT e.occurred_at FROM events e"
    "   WHERE e.kind='workstream'"
    "   AND e.workstream_id = w.workstream_id"
    "   ORDER BY e.ingest_seq DESC LIMIT 1), w.occurred_at) < ?"
    "   THEN 'stale'"
    " ELSE 'active' END AS status FROM workstreams w"
)


def test_workstream_reducer_fixtures(tmp_path: Path):
    """Round-6 F7: the reducer's semantics gate its timing. Seven cases,
    incl. the reviewer's later-shorter-renewal counterexample and
    out-of-order timestamps — LEDGER ORDER is authoritative. (The workstream
    door is Phase 2b; rows seed via direct SQL, same as the bench.)"""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.migrations import migrate

    conn = connect(db_path(tmp_path))
    migrate(conn)
    eid = [0]

    def seed_ws(wsid):
        eid[0] += 1
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream', 't')", (f"ev_c{eid[0]:031x}",))
        conn.execute(
            "INSERT INTO workstreams (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, workstream_id,"
            " title, opened_by_uid) VALUES (?, ?, '1',"
            " '2026-01-01T00:00:00+00:00', 't', 'h', 'fx', ?, 't', 'actor_x')",
            (cur.lastrowid, f"ev_c{eid[0]:031x}", wsid))

    def ev(wsid, event, occurred="2026-05-01T00:00:00+00:00", renewed=None):
        eid[0] += 1
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream_event', 't')", (f"ev_e{eid[0]:031x}",))
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, kind, event,"
            " workstream_id, renewed_until) VALUES (?, ?, '1', ?, 't', 'h',"
            " 'fx', 'workstream', ?, ?, ?)",
            (cur.lastrowid, f"ev_e{eid[0]:031x}", occurred, event, wsid, renewed))

    cutoff = "2026-08-09T00:00:00+00:00"
    seed_ws("ws-arch");   ev("ws-arch", "archived")
    seed_ws("ws-closed"); ev("ws-closed", "closed")
    seed_ws("ws-unblk");  ev("ws-unblk", "blocked"); ev("ws-unblk", "unblocked",
                             occurred="2026-08-20T00:00:00+00:00")
    seed_ws("ws-renew");  ev("ws-renew", "renewed",
                             renewed="2099-01-01T00:00:00+00:00")
    seed_ws("ws-stale");  ev("ws-stale", "progressed")
    # The counterexample: OLD long renewal, then LATER shortening — latest
    # (by ledger order) governs, so this is STALE:
    seed_ws("ws-short");  ev("ws-short", "renewed",
                             renewed="2099-01-01T00:00:00+00:00")
    ev("ws-short", "renewed", renewed="2026-06-01T00:00:00+00:00")
    # Out-of-order timestamps: ledger-later event carries an OLDER
    # occurred_at; ledger order still decides activity — stale:
    seed_ws("ws-ooo");    ev("ws-ooo", "progressed",
                             occurred="2026-08-20T00:00:00+00:00")
    ev("ws-ooo", "progressed", occurred="2026-04-01T00:00:00+00:00")

    res = dict(conn.execute(WORKSTREAM_REDUCER_SQL, (cutoff, cutoff)).fetchall())
    conn.close()
    assert res == {"ws-arch": "archived", "ws-closed": "closed",
                   "ws-unblk": "active", "ws-renew": "active",
                   "ws-stale": "stale", "ws-short": "stale",
                   "ws-ooo": "stale"}


def test_readonly_db_emit_spools_end_to_end(tmp_path: Path):
    """Round-3 F6: the disk-full raw demo never exercised emit. This does,
    via the same error CLASS (OperationalError at write — readonly here,
    SQLITE_FULL in the wild): emit → spooled entry on disk → drain recovers."""
    import os as _os

    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")
    _os.chmod(db_path(tmp_path), 0o400)
    out = emit(tmp_path, _mk_request(9))
    assert out.status == "spooled"
    spooled = list((tmp_path / "state" / "plane" / "spool").glob("*.json"))
    assert len(spooled) == 1
    _os.chmod(db_path(tmp_path), 0o600)
    conn = connect(db_path(tmp_path))
    from claudlobby.plane.spool import drain
    report = drain(tmp_path, conn, ensure_host_uid(tmp_path / "state"))
    conn.close()
    assert report.ingested == 1 and report.remaining == 0


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
        "event_type": "task",
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
             "emit", "task", "--json", "-"],
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


def _burst_worker(root_str: str, i: int, q) -> None:
    # Module-level: multiprocessing spawn (macOS default) must pickle the
    # worker — a nested closure is not spawn-safe (round-2 F7).
    from claudlobby.plane.emit_api import emit as _emit

    try:
        o = _emit(Path(root_str), _request(200_000 + i))
        q.put(o.status)
    except Exception as exc:  # noqa: BLE001
        q.put(f"error:{exc}")


def _seed_realistic(root: Path, n_items: int = 400) -> None:
    """Dispatch triples + transmissions + task histories through emit_batch,
    plus workstream events seeded directly (that family's door is Phase 2b —
    the bench needs the ROWS, not the door)."""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.ids import (
        mint_assignment_id, mint_msg_id, mint_work_item_id,
    )

    for i in range(n_items):
        wi, asg, msg = mint_work_item_id(), mint_assignment_id(), mint_msg_id()
        who = f"bot:bench-fleet/w{i % 20}"
        batch = [
            {"event_type": "work_item", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"work_item_id": wi, "title": f"objective {i}",
                          "created_by": "bot:bench-fleet/mgr"}},
            {"event_type": "assignment", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"assignment_id": asg, "work_item_id": wi,
                          "assignee": who, "assigned_by": "bot:bench-fleet/mgr",
                          "expected_by": "2026-01-01T00:00:00+00:00",
                          "dispatch_msg_id": msg}},
            {"event_type": "communication", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"msg_id": msg, "sender": "bot:bench-fleet/mgr",
                          "recipient": who, "message_class": "task_request",
                          "command_type": "task", "work_item_id": wi,
                          "assignment_id": asg, "body": "x" * 400,
                          "privacy": "full"}},
            {"event_type": "transmission", "emitter": "bench",
             "fleet": "bench-fleet",
             "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                          "destination": "sock", "state": "pane_submitted"}},
        ]
        if i % 3:
            batch.append({"event_type": "transmission", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"msg_id": msg, "attempt_no": 1,
                                       "carrier": "tmux", "destination": "sock",
                                       "state": "recipient_acknowledged"}})
        for p_ in range(i % 4):
            batch.append({"event_type": "task", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"work_item_id": wi, "assignment_id": asg,
                                       "event": "progress",
                                       "progress": 25 * (p_ + 1),
                                       "summary": "s" * 200, "actor": who}})
        if i % 5 == 0:
            batch.append({"event_type": "task", "emitter": "bench",
                          "fleet": "bench-fleet",
                          "payload": {"work_item_id": wi, "assignment_id": asg,
                                       "event": "completed", "actor": who}})
        emit_batch(root, batch)
    conn = connect(db_path(root))
    conn.execute("BEGIN IMMEDIATE")
    for w in range(40):   # the workstream CONSTRUCTS (door is Phase 2b; direct SQL)
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream', 't')", (f"ev_wc{w:030x}",))
        conn.execute(
            "INSERT INTO workstreams (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, workstream_id,"
            " title, opened_by_uid) VALUES (?, ?, '1',"
            " '2026-01-01T00:00:00+00:00', 't', 'h', 'bench', ?, ?, 'actor_b')",
            (cur.lastrowid, f"ev_wc{w:030x}", f"ws-{w}", f"campaign {w}"))
    for j in range(1000):
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream_event', 't')", (f"ev_ws{j:030x}",))
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, kind, event,"
            " workstream_id, renewed_until, detail) VALUES (?, ?, '1',"
            " '2026-05-01T00:00:00+00:00', 't', 'h', 'bench', 'workstream',"
            " ?, ?, ?, ?)",
            (cur.lastrowid, f"ev_ws{j:030x}",
             "renewed" if j % 11 == 0 else ("progressed" if j % 7 else "blocked"),
             f"ws-{j % 40}",
             "2099-01-01T00:00:00+00:00" if j % 11 == 0 else None,
             '{"note": "' + "n" * 120 + '"}'))
    conn.execute("COMMIT")
    conn.close()


def bench_reads(root: Path) -> None:
    """Round-3 F7: the four DERIVATION-shaped reads, on realistic mixed
    history, with EXPLAIN QUERY PLAN. Pi gate thresholds printed with the
    numbers: p50 <= 50ms per query at this seed AND no un-indexed full scan
    of events — else the F16-v2 flip condition is on the table."""
    from claudlobby.plane.db import connect, db_path

    _seed_realistic(root)
    conn = connect(db_path(root))
    TERMINAL = ("'completed','failed','cancelled','returned_blocked',"
                "'superseded','reassigned','expired'")
    # Round-6 F7: the LATEST renewal governs, selected by ledger order
    # (ingest_seq DESC LIMIT 1) — MAX(renewed_until) let an old long renewal
    # override a later shortening (reviewer counterexample); activity recency
    # likewise reads the ledger-latest event's occurred_at, because
    # producer timestamps can arrive out of order.
    # Round-4 F7: these ARE the derivation reducers (Lane C task_status /
    # workstream_status in SQL form), not sketches — terminal closure
    # correlates by ASSIGNMENT_ID (the reassignment counterexample: a
    # work_item-level correlation suppressed the acknowledged overdue
    # replacement), terminal states DOMINATE (first terminal wins forever),
    # and workstream status is contract × events × clock × policy window.
    QUERIES = {
        "attention: unacked or overdue OPEN assignments":
            "SELECT a.assignment_id FROM assignments a"
            " WHERE NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
            f"   AND t.assignment_id = a.assignment_id AND t.event IN ({TERMINAL}))"
            " AND (NOT EXISTS (SELECT 1 FROM events e WHERE"
            "   e.kind='transmission' AND e.msg_id = a.dispatch_msg_id"
            "   AND e.event='recipient_acknowledged')"
            "  OR a.expected_by < '2026-06-01')",
        "task-status: per-assignment, terminal-dominant":
            "SELECT a.assignment_id, COALESCE("
            " (SELECT t.event FROM events t WHERE t.kind='task'"
            f"  AND t.assignment_id = a.assignment_id AND t.event IN ({TERMINAL})"
            "  ORDER BY t.ingest_seq LIMIT 1),"
            " (SELECT t.event FROM events t WHERE t.kind='task'"
            "  AND t.assignment_id = a.assignment_id"
            "  ORDER BY t.ingest_seq DESC LIMIT 1),"
            " 'open') AS status FROM assignments a",
        "workstream-status: contract x events x clock x policy":
            "SELECT w.workstream_id, CASE"
            " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
            "   AND c.workstream_id = w.workstream_id AND c.event='archived')"
            "   THEN 'archived'"
            " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
            "   AND c.workstream_id = w.workstream_id AND c.event='closed')"
            "   THEN 'closed'"
            " WHEN (SELECT e.event FROM events e WHERE e.kind='workstream'"
            "   AND e.workstream_id = w.workstream_id"
            "   AND e.event IN ('blocked','unblocked')"
            "   ORDER BY e.ingest_seq DESC LIMIT 1) = 'blocked' THEN 'blocked'"
            " WHEN COALESCE((SELECT e.renewed_until FROM events e"
            "   WHERE e.kind='workstream' AND e.event='renewed'"
            "   AND e.workstream_id = w.workstream_id"
            "   ORDER BY e.ingest_seq DESC LIMIT 1), '') < ?"
            "  AND COALESCE((SELECT e.occurred_at FROM events e"
            "   WHERE e.kind='workstream'"
            "   AND e.workstream_id = w.workstream_id"
            "   ORDER BY e.ingest_seq DESC LIMIT 1), w.occurred_at) < ?"
            "   THEN 'stale'"
            " ELSE 'active' END AS status FROM workstreams w",
        "reconciliation: submitted-not-acked transmissions":
            "SELECT COUNT(*) FROM events s WHERE s.kind='transmission'"
            " AND s.event='pane_submitted' AND NOT EXISTS"
            " (SELECT 1 FROM events a WHERE a.kind='transmission'"
            "  AND a.msg_id = s.msg_id AND a.event='recipient_acknowledged')",
    }
    # The staleness cutoff is CLOCK x POLICY (round-5 F7): now minus the
    # fleet policy window, computed here and bound as a parameter — never a
    # constant in the SQL.
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    print("\n### read benchmarks (gate: p50 <= 50ms each; EQP must show an"
          " events index, never a bare SCAN events)")
    for name, sql in QUERIES.items():
        params = (cutoff, cutoff) if "?" in sql else ()
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            conn.execute(sql, params).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        print(f"- {name}: p50={times[2]:.1f}ms max={times[-1]:.1f}ms")
        for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params):
            print(f"    EQP: {r[-1]}")
    conn.close()


def bench_burst(root: Path, n: int) -> dict:
    import multiprocessing as mp

    q: mp.Queue = mp.Queue()
    t0 = time.perf_counter()
    procs = [mp.Process(target=_burst_worker, args=(str(root), i, q)) for i in range(n)]
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
    bench_reads(root)

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

- [ ] **Step 1b: Result fixtures BEFORE timing (round-4 F7)**

The battery gains `test_derivation_fixtures` (below) — the reassignment and
terminal-dominance scenarios must return the RIGHT rows before their speed
means anything. Run: `./.venv/bin/pytest tests/test_plane_crash_battery.py::test_derivation_fixtures -v`

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
