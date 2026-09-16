"""The switch registry — every knob the shipped system has, in ONE place.

**The rule (ruled 2026-09-07).** A job or door is **ON by default** unless it

* deletes data,
* spends money,
* mutates operator source,
* sends outbound to people at scale, or
* **has no deployment gate** (added #1265).

The cost reasons are about what a door DOES when it runs. This one is about
how it ARRIVES — a different axis, so the list is deliberately not numbered
here or in its test (``weekly-worker-restart`` already states a reason outside
the original four, and a prose count over a registry is the thing that rots).
It exists and it exists because ``lib/`` is read on demand, per use: a root
pull is in force for every bot on its next call, with no restart gate and no
canary window. Where that is the whole delivery mechanism there is no step at
which one bot can be staged ahead of the others, so the flag is not a hedge
about the behaviour — it is the only stageable rollout available, and arming
one fleet IS the canary. It is deliberately narrow, and the test is DELIBERATENESS
rather than mechanism: a gate is something a human has to CHOOSE, never
something that happens on the next scheduled run. A restart, a per-fleet
compose, or an enrollment that is ALREADY opt-in all count. Automatic
enrollment does not — ``lib/setup-fleet`` skips only the jobs in the composed
DORMANT manifest, so a job that is not opt-in is enrolled on the next setup run
with nobody deciding to. Naming enrollment itself as a gate would therefore
disqualify ``boot-capture``, whose enrollment is automatic *precisely absent
this flag*: the flag is what creates the gate, so it cannot also be the reason
the category does not apply. A door claiming the category must additionally do
nothing from the four above.

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
* the three schema/architecture docs — a GENERATED block rendered by
  :func:`format_markdown`, pinned by test, rather than a fourth hand-kept copy.

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
comparison — called, never re-implemented — so this module agrees with the
composer, the bash gates and the runtime about what "set" means, including
that an empty assignment is a WIN (#1213) and therefore is NOT a ``"0"``.

**And the carrier is a field too.** The first build hand-wrote each row's
``arm`` / ``disarm`` string, and four of them named a carrier that cannot
reach the door they gate — which makes the table worse than no table, because
a reader who follows it watches the flag do nothing and concludes the door is
broken. So the carrier is declared and the two lines are DERIVED from it:

``.env`` tier (of the switch's own scope)
    Read at ``generate`` time, and carried by the composer onto the unit
    (``Environment=``) and into ``bot.conf``. On its own it does NOT reach a
    bot session: ``start-bot.sh`` sources the tiers BEFORE ``set -a``, so a
    bare ``VAR=value`` is assigned unexported and dies with that shell.
``fleet.yaml env:`` → ``bot.conf``
    The session carrier, and the only one for a door that runs INSIDE a bot's
    Claude session (the SessionEnd digest; a spin-down the bot runs itself).
    Declared per bot — ``defaults.env`` is not merged into a bot's env.
``system.yaml enroll`` / ``fleet.yaml defaults.jobs``
    Not a flag at all: compose-time. Unarmed composes no unit, so there is
    nothing for a setup run to enroll.
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

# --- carriers: what an operator actually writes, and where it lands ---------
ENV_FLEET = "fleet .env"
ENV_HOST = "host/root .env"
BOT_CONF = "fleet.yaml env: → bot.conf"
ENROLL_HOST = "system.yaml enroll"
ENROLL_FLEET = "fleet.yaml"

#: Carriers whose scope is a FLEET. A host-wide run (``lib/setup-system``,
#: ``plane doctor`` without ``--fleet``) has not read these, and saying so is
#: the whole of F5: an unread scope reported as "shipped default" is an
#: assertion about something nobody looked at.
FLEET_SCOPED_CARRIERS = frozenset({ENV_FLEET, BOT_CONF, ENROLL_FLEET})

_ENV_WHERE = {
    ENV_FLEET: "the fleet-tier .env",
    ENV_HOST: "the host or root .env",
    BOT_CONF: ("fleet.yaml bots.NAME.env: (then generate; the bot reads it at"
               " its next start — a .env tier does NOT reach a session)"),
}


@dataclass(frozen=True)
class Switch:
    """One shipped knob: what it is, how it is spelled, which way it points,
    and — the field the fold added — which carrier actually reaches it."""

    key: str  #: stable id — the job name where there is one
    scope: str
    polarity: str
    carrier: str
    what: str  #: one line, what turning it on actually does
    env: str | None = None  #: the variable a tier sets, when there is one
    job: str | None = None  #: the system.yaml job whose `enroll` also gates it
    #: the dotted config key an enroll carrier writes, when it is not the
    #: default ``host.jobs.<job>.enroll`` / ``defaults.jobs.<job>.enroll``
    config: str = ""
    #: what else the arming stanza needs beside the boolean, when it needs
    #: anything (the sweep names an owner bot and its repos)
    config_extra: str = ""
    #: the optional install extra the door needs to RUN. A door whose extra is
    #: absent is not composed at all (F1): supervision would turn "exits
    #: saying so" into a crash loop every 5s, forever.
    requires_extra: str = ""
    plane: bool = False  #: rendered by `plane doctor`'s scoped subset
    #: named by `claudlobby status`'s header when OFF — a door whose being off
    #: means a REACTION does not happen (the dispatch→re-check→escalate→close
    #: loop), never merely a slower or quieter one.
    target_workflow: bool = False
    why_opt_in: str = ""  #: which of the four categories keeps it off

    @property
    def default_on(self) -> bool:
        return self.polarity != OPT_IN

    @property
    def fleet_scoped(self) -> bool:
        return self.carrier in FLEET_SCOPED_CARRIERS

    @property
    def arm(self) -> str:
        """The ONE line that turns it on — derived from the carrier."""
        return _carrier_lines(self)[0]

    @property
    def disarm(self) -> str:
        """The ONE line that turns it off — derived from the carrier."""
        return _carrier_lines(self)[1]


def _carrier_lines(sw: Switch) -> tuple[str, str]:
    """(arm, disarm) for *sw*, from its carrier. One derivation, so a row
    cannot name a carrier in one field and a different one in the other."""
    where = _ENV_WHERE.get(sw.carrier)
    if where is not None:
        var = sw.env
        if sw.polarity == OPT_IN:
            return f"{var}=1 in {where}", f"unset {var} — off by default"
        if sw.polarity == SILENCER:
            return (f"unset {var} — recording by default",
                    f"{var}=1 in {where} — the ruled harness exemption;"
                    " silences EVERY door at once")
        return f"unset {var} — on by default", f"{var}=0 in {where}"
    if sw.carrier == ENROLL_HOST:
        key = sw.config or f"host.jobs.{sw.job}.enroll"
        return (
            f"{key}: true in THIS host's system.yaml (host jobs bypass the"
            " fleet merge), then generate + lib/setup-system",
            f"{key}: false in THIS host's system.yaml, then generate (composes"
            " no unit) + lib/setup-system (walks back the installed one)",
        )
    key = sw.config or f"defaults.jobs.{sw.job}.enroll"
    extra = f" (plus {sw.config_extra})" if sw.config_extra else ""
    return (f"{key}: true in fleet.yaml{extra}, then generate + lib/setup-fleet",
            f"{key}: false in fleet.yaml, then generate + lib/setup-fleet")


#: Every switch the shipped system has. Adding a door with a knob means adding
#: a row here — that is the whole contract, and the validator's dead-flag
#: warning is the enforcement (a flag no row claims is reported as dead).
SWITCHES: tuple[Switch, ...] = (
    # ---------------- the target workflow, on ------------------------------
    Switch(
        key="task-recheck",
        scope=FLEET_JOB,
        polarity=OPT_OUT,
        carrier=ENV_FLEET,
        env="TASK_RECHECK_ENABLED",
        job="task-recheck",
        plane=True,
        target_workflow=True,
        what="every 6h, hand each manager ONE re-check of its rows past "
             "deadline, with the four verbs (chase/supersede/withdraw/escalate)",
    ),
    Switch(
        key="plane-expire",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        carrier=ENV_HOST,
        env="PLANE_EXPIRE_ENABLED",
        job="plane-expire",
        plane=True,
        target_workflow=True,
        what="age the attention queue: a terminal `expired` task event for a "
             "deadline nothing closed in 7 days",
    ),
    Switch(
        key="plane-recording",
        scope=DOOR,
        polarity=SILENCER,
        # The .env tier, and the composer carries the resolved value BOTH ways
        # — onto every fleet job unit (a timer sources no .env) and into
        # bot.conf (a session sees no unexported tier assignment). Before the
        # fold this switch reached neither: silencing a fleet left its timers
        # recording, which is the shape of an off switch that is not one.
        carrier=ENV_FLEET,
        env="PLANE_EMIT_DISABLED",
        plane=True,
        target_workflow=True,
        what="every door records on the plane (dispatch, report, heartbeat, "
             "hooks) — the loop has no memory without it",
    ),
    # ---------------- the plane's own equipment, on ------------------------
    Switch(
        key="plane-daemon",
        scope=HOST_SERVICE,
        polarity=OPT_OUT,
        carrier=ENROLL_HOST,
        job="plane-daemon",
        plane=True,
        what="the resident ingest daemon — without it every emit takes the "
             "cold CLI rung (slower, still recorded)",
    ),
    Switch(
        key="plane-view",
        scope=HOST_SERVICE,
        polarity=OPT_OUT,
        carrier=ENROLL_HOST,
        job="plane-view",
        requires_extra="plane-ui",
        plane=True,
        what="the read-only operator plane on localhost (needs the [plane-ui] "
             "extra; fronting it with Tailscale Serve stays yours)",
    ),
    Switch(
        key="plane-host-probe",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        # Enroll, and ONLY enroll. The probe reads the estate silencer, but a
        # host timer's env is closed and this switch has no flag of its own to
        # stamp — so an arm line naming PLANE_EMIT_DISABLED pointed at a
        # carrier that never arrives. Silencing the estate is its own row.
        carrier=ENROLL_HOST,
        job="plane-host-probe",
        plane=True,
        what="per-minute host facets (load, RAM, disk, Pi thermal) as "
             "host.* metric samples for the Host card",
    ),
    Switch(
        key="plane-prune",
        scope=HOST_JOB,
        polarity=OPT_OUT,
        carrier=ENV_HOST,
        env="PLANE_PRUNE_ENABLED",
        job="plane-prune",
        plane=True,
        what="metric-sample retention: age raw host.*/bot.* samples past 30 "
             "days by ingested_at — family-scoped, the ledger is never touched",
    ),
    Switch(
        key="registry-scan",
        scope=GENERATE,
        polarity=OPT_OUT,
        carrier=ENV_FLEET,
        env="PLANE_EMIT_ENABLED",
        plane=True,
        what="one registry keyframe scan per `generate` — what the fleet IS, "
             "so every metric sample has something to join to",
    ),
    # ---------------- other doors, on --------------------------------------
    Switch(
        key="pane-send-chunking",
        scope=DOOR,
        polarity=OPT_OUT,
        # bot.conf, for the same reason session-digest and spindown-receipt use
        # it: the door runs inside a bot's own session (every dispatch, every
        # boot, every keepalive reload), and start-bot.sh sources the .env tiers
        # BEFORE `set -a`, so a bare tier assignment never reaches it. A
        # host-side sender — a hand-run lib/ script, a timer's dispatch — reads
        # the host or root .env instead, which is why the `what` below names
        # both: one door, two kinds of caller.
        carrier=BOT_CONF,
        env="PANE_SEND_CHUNK_BYTES",
        # A REACTION door, and it earns the label the same way the other three
        # do: with chunking off, a dispatch over 1 KB arrives TAIL ONLY — the
        # `[BOTCOMMAND] <manager> | task | …` envelope and the task id gone. The
        # worker then cannot report against an id it never received, the row
        # never closes, and the re-check chases something nobody can answer. The
        # loop does not merely get slower; it stops closing.
        target_workflow=True,
        what="hand every pane send to the pty in 900-byte chunks — a single "
             "write over 1 KB loses its head on macOS (94 of 180 large sends "
             "in a week). NOT a boolean: the value is a byte cap, and only an "
             "exact 0 turns chunking off, restoring the pre-fix send. Reaches "
             "a bot through fleet.yaml env: -> bot.conf; a host-side sender "
             "reads it from the host or root .env",
    ),
    Switch(
        key="spindown-receipt",
        scope=DOOR,
        polarity=OPT_OUT,
        # bot.conf, not a .env tier: spin-down loads the bot's own bot.conf and
        # is commonly run BY a bot session, where an unexported tier assignment
        # was never in the environment to begin with.
        carrier=BOT_CONF,
        env="SPINDOWN_RECEIPT_ENABLED",
        what="a bot_teardown_started receipt before spin-down runs its legs, "
             "so a --purge still leaves a record of who tore what down",
    ),
    # ---------------- the opt-ins (rendered FIRST) -------------------------
    Switch(
        key="update-siblings",
        scope=HOST_JOB,
        polarity=OPT_IN,
        carrier=ENROLL_HOST,
        job="update-siblings",
        why_opt_in="mutates operator source",
        what="weekly fast-forward of sibling framework checkouts to their "
             "newest cut release (notify-behind REPORTS regardless)",
    ),
    Switch(
        key="boot-capture",
        scope=HOST_JOB,
        polarity=OPT_IN,
        carrier=ENROLL_HOST,
        job="boot-capture",
        why_opt_in="no deployment gate — lib/ is read on demand per use, so "
                   "the pull that delivers it is in force on every bot at once "
                   "and nothing can be staged ahead. Enrollment is the only "
                   "canary available; flip it on once one host has run it "
                   "through a real boot",
        what="record every declared bot at its first observation after a host "
             "boot — session_created, .spawn with the instant it was read, the "
             "journal Started time and the derived self-start label — so a boot "
             "measurement no longer needs a bot to be awake to take it",
    ),
    Switch(
        key="boot-capture-stamp",
        scope=DOOR,
        polarity=OPT_IN,
        # start-bot.sh runs before any session exists and loads the bot conf
        # itself; a .env tier assignment is not in that environment.
        carrier=BOT_CONF,
        env="BOOT_CAPTURE_ENABLED",
        why_opt_in="no deployment gate, and more sharply than boot-capture: "
                   "this half has no enrollment step at all, so a root pull "
                   "reaches every bot start immediately",
        what="stamp the instant start-bot.sh actually injects a payload into a "
             "pane, which the service rung is not a proxy for (measured: rungs "
             "fire to the second, sessions appear 36-168s later)",
    ),
    Switch(
        key="session-digest",
        scope=DOOR,
        polarity=OPT_IN,
        # The SessionEnd hook runs inside the bot's Claude session, so bot.conf
        # is the only carrier that reaches it.
        carrier=BOT_CONF,
        env="SESSION_DIGEST_ENABLED",
        why_opt_in="model spend (a Haiku pass per finished session)",
        what="distil each finished session into one structured JSONL row for "
             "the fleet monitor",
    ),
    Switch(
        key="code-audit-sweep",
        scope=FLEET_JOB,
        polarity=OPT_IN,
        carrier=ENROLL_FLEET,
        config="sweep.enabled",
        config_extra="owner_bot and repos",
        why_opt_in="model spend + outbound GitHub issues",
        what="rolling code audit: pick the stalest repo and dispatch the "
             "audit into its owner bot's session",
    ),
    Switch(
        key="weekly-worker-restart",
        scope=FLEET_JOB,
        polarity=OPT_IN,
        carrier=ENROLL_FLEET,
        job="weekly-worker-restart",
        why_opt_in="bounces live worker sessions (context is the thing this "
                   "system exists to keep)",
        what="Sunday restart of worker bots onto the staged Claude Code binary",
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
    registry scan, the estate silencer) contributes nothing here: no timer
    script reads it by that name. The silencer reaches units through the
    fleet-job BASELINE stamp instead — every unit, not one job's.
    """
    out: dict[str, tuple[str, ...]] = {}
    for s in SWITCHES:
        if s.job and s.env and (not scopes or s.scope in scopes):
            out[s.job] = (*out.get(s.job, ()), s.env)
    return out


# ---------------------------------------------------------------------------
# runnability — a door that cannot start must not be supervised
# ---------------------------------------------------------------------------

_EXTRA_MODULES = {"plane-ui": ("fastapi", "uvicorn")}


def extra_available(extra: str) -> bool:
    """Does *extra* import in THIS install — the venv that would run the door?

    Asked at compose time, because the alternative is a supervised unit that
    exits 1 and is relaunched every 5s forever. "The unit exits saying so" is
    an honest failure for a hand run and a crash loop under supervision, and
    only the second is what enrolling by default produces (F1).
    """
    from importlib.util import find_spec

    mods = _EXTRA_MODULES.get(extra)
    if not mods:
        return True
    for mod in mods:
        try:
            if find_spec(mod) is None:
                return False
        except (ImportError, ValueError):  # a broken/partial install
            return False
    return True


def missing_extra(job: str) -> str:
    """The extra *job* needs and does not have, or ""."""
    for s in SWITCHES:
        if s.job == job and s.requires_extra:
            if not extra_available(s.requires_extra):
                return s.requires_extra
    return ""


def extra_install_line(extra: str) -> str:
    return f"pip install -e '.[{extra}]' in the install's venv, then generate"


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
    #: the state could not be READ — ``on`` is the DECLARED default, never a
    #: measurement. Said, never quietly rendered as fact. Two causes ship, and
    #: :attr:`unknown_reason` names which: the env resolver was unreachable,
    #: or this run named no fleet and the switch is fleet-scoped.
    unknown: bool = False
    unknown_reason: str = ""  #: "resolver" | "no-fleet"
    #: overrides the switch's derived arm line when a RUNTIME fact changes it
    #: (today: the [plane-ui] extra being absent).
    arm_override: str = ""

    @property
    def label(self) -> str:
        if self.unknown:
            return "unknown"
        if self.on:
            return "on"
        return "off (opt-in)" if self.switch.polarity == OPT_IN else "off"

    @property
    def arm(self) -> str:
        return self.arm_override or self.switch.arm

    @property
    def disarm(self) -> str:
        return self.switch.disarm


def _env_state(cascade, sw: Switch) -> tuple[bool | None, str]:
    """(on, tier) from the cascade, or (None, "") when no tier assigns it.

    The ``"0"``/``"1"`` compare is ``env_tiers.resolves_to``'s and nothing
    else's — the same call the composer's stamp and the bash gates make. An
    EMPTY assignment is a win at its tier (#1213) but is neither value, so it
    leaves the door at its default, which is the honest reading of
    ``export FLAG=``.
    """
    if not sw.env:
        return None, ""
    res = cascade.get(sw.env)
    if res is None:
        return None, ""
    from .env_tiers import resolves_to

    tier = f"{res.tier} .env"
    if sw.polarity == OPT_IN:
        return resolves_to(cascade, sw.env, "1"), tier
    if sw.polarity == SILENCER:
        return not resolves_to(cascade, sw.env, "1"), tier
    return not resolves_to(cascade, sw.env, "0"), tier


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


#: What a host-wide run says about a scope it never opened. F5: the first build
#: reported these as "shipped default", which is an assertion about a fleet
#: nobody named — the same class as an unreachable reader answering "nothing".
NO_FLEET_DETAIL = ("no fleet named — fleet-tier switches not read; run"
                   " `claudlobby --fleet <name> doctor --switches`")
RESOLVER_DETAIL = "env resolver unreachable — showing the shipped default"


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

    ``fleet=None`` is a HOST run (``lib/setup-system``, ``plane doctor`` with
    no ``--fleet``). Its host rows are true; its fleet-scoped rows are UNKNOWN
    and say so, because the fleet tier was never read.
    """
    from . import env_tiers as _env_tiers
    from .config import load_host_jobs

    unresolved = False
    if cascade is None:
        try:
            cascade = _env_tiers.cascade(
                _env_tiers.read_tiers(
                    paths, fleet_name=fleet.name if fleet else None
                )
            )
        except _env_tiers.ResolverUnavailable:
            cascade, unresolved = {}, True

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
        reason = ""
        unknown = False
        if unresolved and sw.env:
            detail, reason, unknown = RESOLVER_DETAIL, "resolver", True
        elif fleet is None and sw.fleet_scoped:
            detail, reason, unknown = NO_FLEET_DETAIL, "no-fleet", True
            source = "?"

        arm_override = ""
        extra = missing_extra(sw.job) if sw.job else ""
        if extra:
            # F1: the door cannot run here, so the unit is not composed and
            # "on" would be a claim about a process that does not exist.
            on, unknown, reason = False, False, ""
            source = f"the [{extra}] extra is not installed"
            detail = (f"{extra} does not import in this install — no unit is"
                      " composed (a supervised unit that cannot start is a"
                      " crash loop, not an honest failure)")
            arm_override = extra_install_line(extra) + " + lib/setup-system"

        rows.append(
            SwitchState(
                switch=sw, on=on, source=source, detail=detail,
                unknown=unknown, unknown_reason=reason,
                arm_override=arm_override,
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
            # An UNKNOWN row prints BOTH directions: we do not know which way
            # it is set, so printing only one would be picking a side.
            if st.unknown:
                out.append(f"    {'':<{name_w}}  arm: {st.arm}")
                out.append(f"    {'':<{name_w}}  turn off: {st.disarm}")
            else:
                line = st.arm if not st.on else st.disarm
                verb = "arm" if not st.on else "turn off"
                out.append(f"    {'':<{name_w}}  {verb}: {line}")
            if st.detail:
                out.append(f"    {'':<{name_w}}  ! {st.detail}")
            out.append("")

    render(opt_in, "opt-in — ships OFF, arm it yourself",
           "(the only four reasons a door ships off: it deletes data, spends"
           " money, mutates operator source, or sends outbound at scale)")
    render(rest, "on by default", "")
    n_off = sum(1 for s in rest if not s.on and not s.unknown)
    n_unknown = sum(1 for s in rows if s.unknown)
    tail = (f"  {len(rows)} shipped · {len(opt_in)} opt-in (off)"
            f" · {n_off} on-by-default door(s) turned off here")
    if n_unknown:
        tail += f" · {n_unknown} not read here"
    out.append(tail)
    out.append("")
    return "\n".join(out)


def summary_line(states: list[SwitchState]) -> str:
    """The one-line form the doctor rung carries in its Check detail."""
    opt_in = [s for s in states if s.switch.polarity == OPT_IN]
    rest = [s for s in states if s.switch.polarity != OPT_IN]
    off = [s for s in rest if not s.on and not s.unknown]
    unknown = [s for s in states if s.unknown]
    parts = [f"{len(states)} shipped",
             f"{len(rest) - len(off) - sum(1 for s in rest if s.unknown)} on",
             f"{len(opt_in)} opt-in (off)"]
    if off:
        parts.append("turned off here: " + ", ".join(s.switch.key for s in off))
    if any(s.unknown_reason == "resolver" for s in unknown):
        parts.append("env resolver unreachable — env states are the shipped"
                     " defaults, not a reading")
    if any(s.unknown_reason == "no-fleet" for s in unknown):
        parts.append("no fleet named — fleet-tier switches not read")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# the DOC block — the fourth copy, deleted (F8)
# ---------------------------------------------------------------------------

#: The three hand-written tables the fold replaced. Each doc carries the block
#: between these markers; `tests/test_switches.py` asserts the file's block
#: equals this render, and `claudlobby doctor --switches --markdown` prints
#: them for regeneration. A doc table is a copy of the registry like any other,
#: and the estate's recurring defect is a copy drifting (#892/#1143).
DOC_BEGIN = "<!-- BEGIN GENERATED: switches -->"
DOC_END = "<!-- END GENERATED: switches -->"

#: doc path (repo-relative) -> the filter its table carries.
DOC_BLOCKS: dict[str, dict] = {
    "documentation/system-yaml-schema.md": {},
    "documentation/fleet-yaml-schema.md": {"fleet_only": True},
    "documentation/architecture/observable-plane.md": {"plane_only": True},
}


def format_markdown(*, plane_only: bool = False,
                    fleet_only: bool = False) -> str:
    """The SHIPPED registry as a markdown table — host-independent by
    construction (it renders declarations, never this host's state), so the
    same bytes belong in a doc and a diff of them is a real change."""
    rows = [s for s in SWITCHES
            if (s.plane or not plane_only) and (s.fleet_scoped or not fleet_only)]
    out = [DOC_BEGIN,
           "<!-- Generated from claudlobby/switches.py — do not hand-edit."
           " Regenerate: claudlobby doctor --switches --markdown -->",
           "",
           "| Switch | Ships | Scope | Carrier | Flip it with |",
           "|---|---|---|---|---|"]
    for s in sorted(rows, key=lambda s: (s.polarity != OPT_IN, s.key)):
        ships = (f"**off** — {s.why_opt_in}" if s.polarity == OPT_IN
                 else "**on**")
        flip = s.arm if s.polarity == OPT_IN else s.disarm
        out.append(f"| `{s.key}` | {ships} | {s.scope} | {s.carrier} |"
                   f" {flip} |")
    out += ["", DOC_END]
    return "\n".join(out)
