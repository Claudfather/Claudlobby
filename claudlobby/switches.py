"""The switch registry — every knob the shipped system has, in ONE place.

**The rule (ruled 2026-09-07).** A job or door is **ON by default** unless it

* deletes data,
* spends money,
* mutates operator source, or
* sends outbound to people at scale.

Whatever stays opt-in must be NAMED where the operator looks — ``claudlobby
doctor``, ``claudlobby plane doctor``, and the closing summary of
``lib/setup-fleet`` / ``lib/setup-system`` — with the one line that arms it.

The reason the rule exists is not caution about defaults; it is that a
behavior nobody can SEE is a behavior nobody has. A dozen doors shipped dormant
here, each for a defensible local reason, and the composite was a system whose
advertised workflow — a dispatch carries a deadline, the manager is re-checked
on a schedule, it acts or escalates, the operator answers one question, the row
closes — did not run anywhere unless an operator had read a dozen source
comments and armed a dozen flags. That is opacity, not safety.

**Why a registry rather than the flags themselves.** The flags already
existed, spread across ``system.yaml`` enroll keys, ``lib/*.sh`` self-gates,
``composer.py`` arming tables and a validator's hardcoded prefix list. Four
copies of "what knobs exist" means the fifth reader gets it wrong: the F18
closure deleted the shadow and ``PLANE_SHADOW_ENABLED`` kept sitting in a live
``.env`` with nothing to say so. So the set is declared once, here, and every
consumer DERIVES from it:

* ``composer.FLEET_JOB_ARMING`` / ``HOST_JOB_ARMING`` — which unit carries
  which ``Environment=`` line (a scheduler env is closed; #1383).
* ``validator`` — a ``*_ENABLED`` key in a claudlobby namespace that no
  switch claims is a DEAD flag, warned without anyone maintaining a list.
* ``doctor`` / ``plane doctor`` / ``setup-fleet`` / ``setup-system`` — the
  table the operator reads.
* ``status`` — the header line that names a target-workflow door turned off.

**Polarity is a field, not a convention.** Three shapes ship:

``opt-out``
    ON unless the env var resolves to exactly ``"0"``. The default for
    everything the rule puts on. An explicit ``0`` is honoured and is said
    LOUDLY by the door itself, so an off switch is visible in the logs of the
    host it is off on — a silent no-op is how a disarmed door reads as a
    broken one.
``opt-in``
    OFF unless the env var resolves to exactly ``"1"`` (or, for a pure enroll
    knob, until the manifest arms it). Reserved for the four categories above.
``silencer``
    ON unless the env var resolves to ``"1"`` — the inverted spelling
    ``PLANE_EMIT_DISABLED`` already had. Kept inverted rather than normalised
    because it is the ruled harness exemption and every door reads it by that
    name; renaming it would break the exemption for one release's benefit.

Only an exact ``"0"`` / ``"1"`` decides. ``env_tiers.resolves_to`` is the one
comparison, so this module agrees with the composer, the bash gates and the
runtime about what "set" means — including that an empty assignment is a WIN
(#1213) and therefore is NOT a ``"0"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import FleetConfig
    from .paths import Paths

# --- scopes -----------------------------------------------------------------
HOST_JOB = "host job"
HOST_SERVICE = "host service"
FLEET_JOB = "fleet job"
GENERATE = "generate"
DOOR = "door"

# --- polarities -------------------------------------------------------------
OPT_OUT = "opt-out"
OPT_IN = "opt-in"
SILENCER = "silencer"


@dataclass(frozen=True)
class Switch:
    """One shipped knob: what it is, how it is spelled, which way it points."""

    key: str  #: stable id — the job name where there is one
    scope: str
    polarity: str
    what: str  #: one line, what turning it on actually does
    arm: str  #: the ONE line that turns it on
    disarm: str  #: the ONE line that turns it off
    env: str | None = None  #: the variable a tier sets, when there is one
    job: str | None = None  #: the system.yaml job whose `enroll` also gates it
    plane: bool = False  #: rendered by `plane doctor`'s scoped subset
    #: named by `claudlobby status`'s header when OFF — a door whose being off
    #: means a REACTION does not happen (the dispatch→re-check→escalate→close
    #: loop), never merely a slower or quieter one.
    target_workflow: bool = False
    why_opt_in: str = ""  #: which of the four categories keeps it off

    @property
    def default_on(self) -> bool:
        return self.polarity != OPT_IN


#: Every switch the shipped system has. Adding a door with a knob means adding
#: a row here — that is the whole contract, and the validator's dead-flag
#: warning is the enforcement (a flag no row claims is reported as dead).
SWITCHES: tuple[Switch, ...] = (
    # ---------------- the target workflow, on ------------------------------
    Switch(
        key="task-recheck",
        scope=FLEET_JOB,
        polarity=OPT_OUT,
        env="TASK_RECHECK_ENABLED",
        job="task-recheck",
        plane=True,
        target_workflow=True,
        what="every 6h, hand each manager ONE re-check of its rows past "
             "deadline, with the four verbs (chase/supersede/withdraw/escalate)",
        arm="unset TASK_RECHECK_ENABLED (on by default)",
        disarm="TASK_RECHECK_ENABLED=0 in the fleet .env",
    ),
    Switch(
        key="plane-expire",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        env="PLANE_EXPIRE_ENABLED",
        job="plane-expire",
        plane=True,
        target_workflow=True,
        what="age the attention queue: a terminal `expired` task event for a "
             "deadline nothing closed in 7 days",
        arm="unset PLANE_EXPIRE_ENABLED (on by default)",
        disarm="PLANE_EXPIRE_ENABLED=0 in the host or root .env",
    ),
    Switch(
        key="plane-recording",
        scope=DOOR,
        polarity=SILENCER,
        env="PLANE_EMIT_DISABLED",
        plane=True,
        target_workflow=True,
        what="every door records on the plane (dispatch, report, heartbeat, "
             "hooks) — the loop has no memory without it",
        arm="unset PLANE_EMIT_DISABLED (recording by default)",
        disarm="PLANE_EMIT_DISABLED=1 — the ruled harness exemption; silences "
               "EVERY door at once",
    ),
    # ---------------- the plane's own equipment, on ------------------------
    Switch(
        key="plane-daemon",
        scope=HOST_SERVICE,
        polarity=OPT_OUT,
        job="plane-daemon",
        plane=True,
        what="the resident ingest daemon — without it every emit takes the "
             "cold CLI rung (slower, still recorded)",
        arm="host.jobs.plane-daemon.enroll: true in system.yaml, then "
            "generate + lib/setup-system",
        disarm="host.jobs.plane-daemon.enroll: false in system.yaml, then "
               "generate (prunes the units), then stop the installed one: "
               "systemctl --user disable --now claudlobby-plane-daemon.service"
               " / launchctl bootout gui/$UID/claudlobby-plane-daemon",
    ),
    Switch(
        key="plane-view",
        scope=HOST_SERVICE,
        polarity=OPT_OUT,
        job="plane-view",
        plane=True,
        what="the read-only operator plane on localhost (needs the [plane-ui] "
             "extra; fronting it with Tailscale Serve stays yours)",
        arm="host.jobs.plane-view.enroll: true in system.yaml, then generate "
            "+ lib/setup-system",
        disarm="host.jobs.plane-view.enroll: false in system.yaml, then "
               "generate, then stop the installed unit",
    ),
    Switch(
        key="plane-host-probe",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        job="plane-host-probe",
        plane=True,
        what="per-minute host facets (load, RAM, disk, Pi thermal) as "
             "host.* metric samples for the Host card",
        arm="unset PLANE_EMIT_DISABLED (on by default)",
        disarm="host.jobs.plane-host-probe.enroll: false in system.yaml "
               "(or PLANE_EMIT_DISABLED=1, which silences every door)",
    ),
    Switch(
        key="plane-prune",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        env="PLANE_PRUNE_ENABLED",
        job="plane-prune",
        plane=True,
        what="metric-sample retention: age raw host.*/bot.* samples past 30 "
             "days by ingested_at — family-scoped, the ledger is never touched",
        arm="unset PLANE_PRUNE_ENABLED (on by default)",
        disarm="PLANE_PRUNE_ENABLED=0 in the host or root .env",
    ),
    Switch(
        key="registry-scan",
        scope=GENERATE,
        polarity=OPT_OUT,
        env="PLANE_EMIT_ENABLED",
        plane=True,
        what="one registry keyframe scan per `generate` — what the fleet IS, "
             "so every metric sample has something to join to",
        arm="unset PLANE_EMIT_ENABLED (on by default)",
        disarm="PLANE_EMIT_ENABLED=0 in the fleet .env",
    ),
    # ---------------- other doors, on --------------------------------------
    Switch(
        key="spindown-receipt",
        scope=DOOR,
        polarity=OPT_OUT,
        env="SPINDOWN_RECEIPT_ENABLED",
        what="a bot_teardown_started receipt before spin-down runs its legs, "
             "so a --purge still leaves a record of who tore what down",
        arm="unset SPINDOWN_RECEIPT_ENABLED (on by default)",
        disarm="SPINDOWN_RECEIPT_ENABLED=0 in the fleet .env",
    ),
    # ---------------- the opt-ins (rendered FIRST) -------------------------
    Switch(
        key="update-siblings",
        scope=HOST_JOB,
        polarity=OPT_IN,
        job="update-siblings",
        why_opt_in="mutates operator source",
        what="weekly fast-forward of sibling framework checkouts to their "
             "newest cut release (notify-behind REPORTS regardless)",
        arm="host.jobs.update-siblings.enroll: true in system.yaml, then "
            "generate + lib/setup-system",
        disarm="host.jobs.update-siblings.enroll: false in system.yaml, then "
               "generate + lib/setup-system",
    ),
    Switch(
        key="session-digest",
        scope=DOOR,
        polarity=OPT_IN,
        env="SESSION_DIGEST_ENABLED",
        why_opt_in="model spend (a Haiku pass per finished session)",
        what="distil each finished session into one structured JSONL row for "
             "the fleet monitor",
        arm="SESSION_DIGEST_ENABLED=1 in the fleet .env",
        disarm="unset SESSION_DIGEST_ENABLED (off by default)",
    ),
    Switch(
        key="code-audit-sweep",
        scope=FLEET_JOB,
        polarity=OPT_IN,
        why_opt_in="model spend + outbound GitHub issues",
        what="rolling code audit: pick the stalest repo and dispatch the "
             "audit into its owner bot's session",
        arm="sweep: { enabled: true, owner_bot: <bot>, repos: [...] } in "
            "fleet.yaml",
        disarm="remove the fleet.yaml sweep: block (off by default)",
    ),
    Switch(
        key="weekly-worker-restart",
        scope=FLEET_JOB,
        polarity=OPT_IN,
        job="weekly-worker-restart",
        why_opt_in="bounces live worker sessions (context is the thing this "
                   "system exists to keep)",
        what="Sunday restart of worker bots onto the staged Claude Code binary",
        arm="defaults: { jobs: { weekly-worker-restart: { enroll: true } } } "
            "in fleet.yaml",
        disarm="remove that stanza (off by default)",
    ),
)

#: Flags a shipped door once read and no longer does. A key here gets its own
#: reason in the validator's warning; a claudlobby-namespace ``*_ENABLED`` key
#: in NEITHER this map nor :data:`SWITCHES` is reported as dead generically, so
#: the next deletion warns without anyone remembering to add a row.
RETIRED: dict[str, str] = {
    "PLANE_SHADOW_ENABLED": (
        "the dual-write shadow was deleted in the F18 closure (R2a) — nothing"
        " compares, nothing reads this"
    ),
    "PLANE_DUAL_WRITE_ENABLED": (
        "the dual-write era ended with the F18 closure — the plane is the only"
        " record"
    ),
}

#: Retired PREFIXES — a whole family of transition flags, none with a reader.
RETIRED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("PLANE_READ_", "a retired cutover read flag with no reader since the F18"
                    " closure — the plane is the only source; nothing flips"),
    ("PLANE_LEGACY_WRITE_", "a retired cutover write flag with no reader since"
                            " the F18 closure — there is no legacy ledger left"),
)


def by_key(key: str) -> Switch:
    for s in SWITCHES:
        if s.key == key:
            return s
    raise KeyError(key)


def env_names() -> frozenset[str]:
    """Every variable a shipped switch reads."""
    return frozenset(s.env for s in SWITCHES if s.env)


def namespaces() -> frozenset[str]:
    """The first token of every switch variable — the claudlobby namespaces.

    Derived, never listed: it is what bounds the validator's generic dead-flag
    sweep to OUR variables. A fleet's own ``MYTOOL_ENABLED`` is none of our
    business, and warning about it would train operators to ignore the check.
    """
    return frozenset(name.split("_", 1)[0] for name in env_names())


def jobs_with_env(*scopes: str) -> dict[str, tuple[str, ...]]:
    """job name -> the variables its unit must carry an ``Environment=`` for.

    The composer's arming tables are THIS, not a second list: a timer starts
    with a closed environment, so a door that reads a flag needs it stamped or
    the flag is unreachable however loudly a tier sets it (#1383, measured
    twice — briefing, then keepalive). The stamp exists FOR THE SCRIPT THAT
    READS THE FLAG, which is why a switch with no ``job`` (the generate-time
    registry scan) contributes nothing here: no timer script reads it.
    """
    out: dict[str, tuple[str, ...]] = {}
    for s in SWITCHES:
        if s.job and s.env and (not scopes or s.scope in scopes):
            out[s.job] = (*out.get(s.job, ()), s.env)
    return out


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchState:
    """What a switch is actually set to here, and what set it."""

    switch: Switch
    on: bool
    source: str  #: "default" | "<tier> .env" | "system.yaml" | "fleet.yaml"
    detail: str = ""
    #: the env cascade could not be reached — ``on`` is the DECLARED default,
    #: never a measurement. Said, never quietly rendered as fact.
    unknown: bool = False

    @property
    def label(self) -> str:
        if self.unknown:
            return "unknown"
        if self.on:
            return "on"
        return "off (opt-in)" if self.switch.polarity == OPT_IN else "off"


def _env_state(cascade, sw: Switch) -> tuple[bool | None, str]:
    """(on, tier) from the cascade, or (None, "") when no tier assigns it."""
    if not sw.env:
        return None, ""
    res = cascade.get(sw.env)
    if res is None:
        return None, ""
    tier = f"{res.tier} .env"
    if sw.polarity == OPT_IN:
        return res.value == "1", tier
    if sw.polarity == SILENCER:
        return res.value != "1", tier
    # opt-out: only an exact "0" turns it off. An EMPTY assignment is a win
    # (#1213) but is not "0", so it leaves the door on — which is the honest
    # reading of `export FLAG=` and the same rule env_tiers.resolves_to applies.
    return res.value != "0", tier


def _enroll_state(sw: Switch, host_jobs: dict, fleet_jobs: dict,
                  sweep_on: bool | None) -> tuple[bool | None, str]:
    """(enrolled, where) from the composed manifests' own config truth."""
    if sw.key == "code-audit-sweep":
        if sweep_on is None:
            return None, ""
        return sweep_on, "fleet.yaml sweep:"
    if not sw.job:
        return None, ""
    if sw.scope == HOST_SERVICE:
        cfg = host_jobs.get(sw.job)
        if cfg is None:
            return False, "system.yaml (job absent)"
        # Compose-time dormancy: a service is dormant unless EXACTLY true.
        return cfg.get("enroll") is True, "system.yaml"
    cfg = (host_jobs if sw.scope == HOST_JOB else fleet_jobs).get(sw.job)
    if cfg is None:
        return None, ""
    where = "system.yaml" if sw.scope == HOST_JOB else "fleet defaults.jobs"
    return cfg.get("enroll", True) is not False, where


def resolve(
    paths: Paths,
    fleet: FleetConfig | None = None,
    *,
    cascade: dict | None = None,
) -> list[SwitchState]:
    """Every switch, its state here, and what set it — opt-ins FIRST.

    Opt-ins lead the list on purpose: they are the rows a reader must act on,
    and a table that buries the four off switches under the nine on ones has
    named them without surfacing them.
    """
    from . import env_tiers as _env_tiers
    from .config import load_host_jobs

    unknown = False
    if cascade is None:
        try:
            cascade = _env_tiers.cascade(
                _env_tiers.read_tiers(
                    paths, fleet_name=fleet.name if fleet else None
                )
            )
        except _env_tiers.ResolverUnavailable:
            cascade, unknown = {}, True

    try:
        host_jobs = load_host_jobs()
    except Exception:  # noqa: BLE001 — a broken system.yaml must not kill the table
        host_jobs = {}
    fleet_jobs: dict = {}
    sweep_on: bool | None = None
    if fleet is not None:
        # FleetConfig.defaults IS the merged system<fleet tier (config.py
        # writes it there), so a fleet's `enroll: true` override is already
        # folded in — re-merging here would be a second copy of that rule.
        fleet_jobs = (getattr(fleet, "defaults", None) or {}).get("jobs") or {}
        sweep_on = fleet.sweep_enabled()

    rows: list[SwitchState] = []
    for sw in SWITCHES:
        env_on, tier = _env_state(cascade, sw)
        enrolled, where = _enroll_state(sw, host_jobs, fleet_jobs, sweep_on)

        # A door runs only when BOTH gates allow it: the manifest may enroll a
        # unit whose script still no-ops on its own flag, and that combination
        # is precisely what an opt-out flag exists for.
        enroll_ok = True if enrolled is None else enrolled
        env_ok = sw.default_on if env_on is None else env_on
        on = enroll_ok and env_ok

        # Attribution answers "who decided this", so only something that moved
        # the switch AWAY from its shipped default is named as the decider.
        # An opt-in sitting off because nobody armed it reads "default", not
        # "fleet.yaml" — the config is not what turned it off; it was never on.
        # A tier that merely RESTATES the default is still named, because
        # "someone wrote this down" is a different fact from "nobody touched
        # it" and the two lead to different edits.
        source = "default"
        if enrolled is not None and enrolled is not sw.default_on:
            source = where
        if env_on is not None and env_on is not sw.default_on:
            source = tier
        elif source == "default" and env_on is not None:
            source = tier
        detail = ""
        if unknown and sw.env:
            detail = "env resolver unreachable — showing the shipped default"
        rows.append(
            SwitchState(
                switch=sw, on=on, source=source, detail=detail,
                unknown=bool(unknown and sw.env),
            )
        )
    rows.sort(key=lambda r: (r.switch.polarity != OPT_IN, r.switch.key))
    return rows


def target_workflow_off(states: list[SwitchState]) -> list[SwitchState]:
    """The reaction-chain doors that are OFF here — status's header line."""
    return [s for s in states if s.switch.target_workflow and not s.on
            and not s.unknown]


# ---------------------------------------------------------------------------
# rendering — ONE definition, called by doctor, plane doctor and both shell
# setup doors (through `claudlobby doctor --switches`). A second copy in bash
# is how the table and the truth drift apart.
# ---------------------------------------------------------------------------


def format_table(states: list[SwitchState], *, plane_only: bool = False) -> str:
    rows = [s for s in states if (s.switch.plane or not plane_only)]
    scope_label = "plane switches" if plane_only else "switches"
    out = ["", f"=== {scope_label} ===", ""]
    if not rows:
        return "\n".join(out + [""])

    name_w = max(len(s.switch.key) for s in rows)
    opt_in = [s for s in rows if s.switch.polarity == OPT_IN]
    rest = [s for s in rows if s.switch.polarity != OPT_IN]

    def render(group: list[SwitchState], heading: str, note: str) -> None:
        if not group:
            return
        out.append(f"  {heading}")
        if note:
            out.append(f"  {note}")
        for st in group:
            sw = st.switch
            src = st.source if st.source != "default" else "shipped default"
            out.append(f"    {sw.key:<{name_w}}  {st.label:<12}  "
                       f"[{sw.scope}]  set by: {src}")
            out.append(f"    {'':<{name_w}}  {sw.what}")
            if sw.why_opt_in:
                out.append(f"    {'':<{name_w}}  stays off — "
                           f"{sw.why_opt_in}")
            line = sw.arm if not st.on else sw.disarm
            verb = "arm" if not st.on else "turn off"
            out.append(f"    {'':<{name_w}}  {verb}: {line}")
            if st.detail:
                out.append(f"    {'':<{name_w}}  ! {st.detail}")
            out.append("")

    render(opt_in, "opt-in — ships OFF, arm it yourself",
           "(the only four reasons a door ships off: it deletes data, spends"
           " money, mutates operator source, or sends outbound at scale)")
    render(rest, "on by default", "")
    n_off = sum(1 for s in rest if not s.on)
    out.append(f"  {len(rows)} shipped · {len(opt_in)} opt-in (off)"
               f" · {n_off} on-by-default door(s) turned off here")
    out.append("")
    return "\n".join(out)


def summary_line(states: list[SwitchState]) -> str:
    """The one-line form the doctor rung carries in its Check detail."""
    opt_in = [s for s in states if s.switch.polarity == OPT_IN]
    rest = [s for s in states if s.switch.polarity != OPT_IN]
    off = [s for s in rest if not s.on]
    unknown = [s for s in states if s.unknown]
    parts = [f"{len(states)} shipped", f"{len(rest) - len(off)} on",
             f"{len(opt_in)} opt-in (off)"]
    if off:
        parts.append("turned off here: " + ", ".join(s.switch.key for s in off))
    if unknown:
        parts.append("env resolver unreachable — env states are the shipped"
                     " defaults, not a reading")
    return " · ".join(parts)
