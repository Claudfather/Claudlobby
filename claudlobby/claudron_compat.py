"""Claudron compatibility floor — machine-readable SSOT.

Maps each claudlobby integration surface to the minimum Claudron capability
it needs. ``claudlobby host doctor``'s claudron check (``doctor.check_claudron``)
reads this table; ``documentation/integrations/claudron-integration.md`` is the
human-readable rendering and must be updated when this table changes (a unit
test asserts the doc stays in sync).

Claudron's release numbers are ordinal — their roadmap states version numbers
follow ship order if their Gate G1 re-orders epics — so entries key on
*capability*, with the default-order release recorded as an annotation, never
used as a trigger. ``probe`` is what makes that real: a row is met when the
capability answers, not when a version string compares. (``engine_version``
from Claudron's capability probe is reported by doctor as *health*, not as a
floor trigger.) The authoritative install pin itself lives in pyproject.toml's
``[vault]`` extra, not here — and it governs only the *imported* API; the host
CLI is installed out of band.

A ``parked`` row is a surface that is deliberately **not built** — demand-gated
by a recorded decision. Parked rows are never probed and never render "unmet";
warning about the absence of a door nobody shipped is the exact failure this
table's consumer was built to stop doing.
"""

import json
import subprocess
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Literal

#: Canonical any-agent integration front door — Claudron owns this text; every
#: claudlobby message that tells an operator how bots reach a vault cites it.
CLAUDRON_INTEGRATION_URL = (
    "https://github.com/Claudfather/Claudron/blob/main/docs/INTEGRATION.md"
)

#: ``probe`` vocabulary.
PROBE_NONE = ""  # no automatic probe (parked rows, or capability not probeable)
PROBE_API = "api"  # the ``[vault]`` import seam resolves (paths.vault_api_available)
PROBE_VERB_PREFIX = "verb:"  # ``claudron <verb> --help`` resolves on the host CLI


@dataclass(frozen=True)
class ClaudronCapability:
    feature: str  # the claudlobby surface that consumes it
    requires: str  # what claudron must ship (pure capability — no epic ordinals)
    default_order_release: str  # Claudron's slated release, or "pinned SHA"
    probe: str = PROBE_NONE  # how doctor decides met/unmet — see the vocabulary
    parked: str = ""  # non-empty ⇒ demand-gated; the text names the decision


COMPAT_FLOOR: tuple[ClaudronCapability, ...] = (
    ClaudronCapability(
        feature="vault-based fleet overlay resolution (paths.py .claudron bridge)",
        requires="claudron.vault.detect / Vault.fleets API",
        default_order_release="0.2.0",
        probe=PROBE_API,
    ),
    ClaudronCapability(
        feature="Agent vault lookup (composed instructions and check-in skill)",
        requires="claudron lookup CLI",
        # 0.3.0, not 0.2.0: post-L1 the wedge relies on the CLI reading
        # CLAUDRON_VAULT_PATH (dropped the explicit --vault), which landed in
        # 0.3.0 (C1). On 0.2.0 the CLI reads only the removed CLAUDRON_VAULT, so
        # the wedge would resolve no vault — a floor at 0.2.0 was a false green.
        default_order_release="0.3.0",
        probe=PROBE_VERB_PREFIX + "lookup",
    ),
    ClaudronCapability(
        feature="fleet session loop — engine hooks installed per bot (plan L2)",
        requires="claudron hooks install + per-event hook dispatch",
        # 0.4.0, not 0.2.0: the composed hooks resolve the vault via
        # CLAUDRON_VAULT_PATH (0.3.0+) and defer to the engine's front-end-neutral
        # PreCompact prompt (C2, 0.4.0). On an older engine the loop is dead or
        # double-prompts — a floor at 0.2.0 reported "met" on a broken loop (#680).
        default_order_release="0.4.0",
        probe=PROBE_VERB_PREFIX + "hooks",
    ),
    ClaudronCapability(
        feature="clauDNA harvest from a composed bot (CLAUDNA_HARVEST=1 → claudron capture --stdin / amend)",
        requires="capture --stdin + amend --stdin with expect_trust (subject-filing), memory homes",
        default_order_release="0.8.0",
        probe=PROBE_VERB_PREFIX + "amend",
    ),
    ClaudronCapability(
        feature="MCP fragment library/mcp/claudron.json",
        requires="claudron-mcp stdio server",
        default_order_release="unbuilt — demand-gated",
        parked="decision C",
    ),
)


#: Combined status + doctor budget per distinct vault, shared by both readers.
CLAUDRON_DOCTOR_TIMEOUT_S = 60.0
_RC_TIMEOUT = 124
Run = Callable[..., tuple[int, str]]


@dataclass(frozen=True)
class VaultMigrationState:
    """CLI-owned migration state, with diagnostics retained for doctor's rows.

    ``old_format`` means unequal formats, including a vault newer than its engine.
    Findings are normalized (code, message) pairs; no vault contents are read here.
    """

    status: Literal["ok", "pending", "old_format", "unknown"]
    pending: tuple[tuple[str, str], ...]
    vault_format: int | None
    engine_format: int | None
    detail: str
    findings: tuple[tuple[str, str], ...] = ()
    probe_status: str = "warn"
    probe_detail: str = ""


MigrationReader = Callable[[str], VaultMigrationState]


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Absent binary ⇒ 127; no answer within the budget ⇒ 124."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return _RC_TIMEOUT, ""
    except OSError:
        return 1, ""


def _claudron_probe(vault: str, *, run: Run, timeout: float) -> tuple[str, str, dict | None]:
    """Sanctioned capability probe; doctor capability, never version, gates the next call."""
    rc, out = run(["claudron", "status", "--json", "--vault", vault], timeout=timeout)
    if rc == 3:
        return "warn", f"claudron installed but no vault resolved at {vault}", None
    if rc != 0:
        return "warn", f"`claudron status --json` exited {rc} for {vault}", None
    try:
        data = json.loads(out)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        data = None
    if not isinstance(data, dict):
        return "warn", "`claudron status --json` did not return the CLI envelope", None
    version = data.get("engine_version", "unknown (pre-0.3.0)")
    return "pass", (
        f"engine {version}, vault {data.get('root', vault)} "
        f"({data.get('total_docs', '?')} notes)"
    ), data


def vault_migration_state(
    vault: str, *, run: Run | None = None, timeout: float | None = None
) -> VaultMigrationState:
    """Read status then doctor under one deadline, never applying a migration.

    Only the CLI is consulted. An unusable answer is unknown, never healthy;
    exit 1 is a valid doctor answer when it carries error findings.
    """
    run = _run if run is None else run
    budget = CLAUDRON_DOCTOR_TIMEOUT_S if timeout is None else timeout
    deadline = monotonic() + budget
    remaining = max(0.0, deadline - monotonic())
    if remaining == 0:
        return VaultMigrationState("unknown", (), None, None,
            f"{vault}: capability probe budget ({budget:g}s) exhausted; pending migrations are unknown")
    probe_status, probe_detail, probe = _claudron_probe(vault, run=run, timeout=min(10.0, remaining))

    def state(status, detail="", *, pending=(), vault_format=None, engine_format=None, findings=()):
        return VaultMigrationState(status, pending, vault_format, engine_format, detail,
                                   findings, probe_status, probe_detail)

    if probe is None:
        return state("unknown", f"{vault}: {probe_detail}; pending migrations are unknown "
                     f"(status + doctor budget {budget:g}s)")
    if "doctor" not in (probe.get("capabilities") or []):
        return state("unknown", (
            f"{vault}: this claudron does not declare the `doctor` capability, so pending "
            f"migrations cannot be checked — upgrade Claudron (engine "
            f"{probe.get('engine_version', 'unknown')})"
        ))
    remaining = max(0.0, deadline - monotonic())
    if remaining == 0:
        rc, out = _RC_TIMEOUT, ""
    else:
        rc, out = run(["claudron", "doctor", "--json", "--vault", vault], timeout=remaining)
    if rc == _RC_TIMEOUT:
        return state("unknown", (
            f"{vault}: `claudron doctor` did not answer within {budget:g}s, "
            f"so pending migrations are unknown (shared status + doctor budget) — walking a vault root that holds large "
            f"ignored trees takes minutes (Claudron #201); run `claudron doctor --vault "
            f"{vault}` by hand"
        ))
    if rc == 3:
        return state("unknown", f"{vault}: `claudron doctor` found no vault there (exit 3)")
    try:
        envelope = json.loads(out)
        data = envelope["data"]
        errors, warnings = envelope["errors"], envelope["warnings"]
        shaped = (envelope.get("command") == "doctor" and isinstance(data, dict)
                  and isinstance(errors, list) and isinstance(warnings, list))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        shaped = False
    if rc not in (0, 1) or not shaped:
        return state("unknown", f"{vault}: `claudron doctor --json` exited {rc} without its envelope")
    findings = tuple((str(f.get("code")), str(f.get("message")))
                     for f in errors + warnings if isinstance(f, dict))
    vault_format, engine_format = data.get("vault_format"), data.get("engine_format")
    if type(vault_format) is not int or type(engine_format) is not int:
        return state("unknown", f"{vault}: `claudron doctor --json` did not report integer vault and engine formats; "
                     "pending migrations are unknown", findings=findings)
    raw_pending = data.get("pending")
    if not isinstance(raw_pending, list):
        return state("unknown", f"{vault}: `claudron doctor --json` did not report a pending list; "
                     "pending migrations are unknown", findings=findings)
    pending = tuple((str(p.get("id")), str(p.get("title"))) for p in raw_pending if isinstance(p, dict))
    status = ("pending" if pending or any(code == "D001" for code, _ in findings)
              else "old_format" if vault_format != engine_format else "ok")
    return state(status, pending=pending, vault_format=vault_format, engine_format=engine_format, findings=findings)


def vault_migration_reader(*, run: Run | None = None, timeout: float | None = None) -> MigrationReader:
    """One memo per request; resolve the current prober when called, not at import."""
    states: dict[str, VaultMigrationState] = {}

    def read(vault: str) -> VaultMigrationState:
        if vault not in states:
            states[vault] = vault_migration_state(vault, run=run, timeout=timeout)
        return states[vault]

    return read
