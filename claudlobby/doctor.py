"""claudlobby doctor — pre-flight fleet health diagnostic.

Consolidates checks from creds-check.sh, check-npx-cache.sh, and
reconcile-fleet.sh into a single Python entry point with structured output.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import dotenv
from .claudron_compat import (
    CLAUDRON_INTEGRATION_URL,
    COMPAT_FLOOR,
    PROBE_API,
    PROBE_VERB_PREFIX,
)
from .config import FleetConfig
from .paths import Paths, tmux_socket_for_bot, vault_api_available
from .validator import validate

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    status: str  # "pass", "warn", "fail"
    detail: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    #: the switch table, carried alongside the checks so `format_report` can
    #: print it whole. A Check's `detail` is one line by construction and the
    #: switches are the one thing here an operator has to READ rather than
    #: scan, so the rung carries a summary and the rows ride here.
    switch_rows: list = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    @property
    def passed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "pass"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)


# ----------------------------------------------------------------------
# Check: environment variables
# ----------------------------------------------------------------------


def check_env_vars(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """Verify all contracted env vars are present and non-empty."""
    from .composer import collect_env_contracts

    env_vars = collect_env_contracts(fleet, paths)
    effective_env = dict(os.environ)

    # Merge fleet-level .env
    if paths.env_file and paths.env_file.is_file():
        for k, v in dotenv.read(paths.env_file).items():
            if k not in effective_env:
                effective_env[k] = v

    missing = []
    empty = []
    for ev in env_vars:
        val = effective_env.get(ev.name)
        if val is None:
            missing.append(f"{ev.name} ({ev.source})")
        elif val == "":
            empty.append(f"{ev.name} ({ev.source})")

    if missing:
        report.add(
            "env-vars",
            "fail",
            f"{len(missing)} missing: {', '.join(missing[:5])}"
            + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""),
        )
    elif empty:
        report.add(
            "env-vars",
            "warn",
            f"{len(empty)} empty: {', '.join(empty[:5])}",
        )
    else:
        report.add("env-vars", "pass", f"{len(env_vars)} contracted vars present")


# ----------------------------------------------------------------------
# Check: MCP server configs resolve
# ----------------------------------------------------------------------


def check_mcp_configs(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """Verify MCP fragment files exist and reference valid commands."""
    missing_frags = []
    missing_cmds = []

    for bot in fleet.bots.values():
        for entry in bot.mcp:
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                if entry.name not in missing_frags:
                    missing_frags.append(entry.name)
                continue
            try:
                frag = json.loads(frag_path.read_text())
            except (json.JSONDecodeError, OSError):
                if entry.name not in missing_frags:
                    missing_frags.append(entry.name)
                continue
            # Check that the command binary exists
            for k, v in frag.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue
                cmd = v.get("command")
                if cmd and cmd != "npx" and shutil.which(cmd) is None:
                    key = f"{entry.name}/{cmd}"
                    if key not in missing_cmds:
                        missing_cmds.append(key)

    if missing_frags:
        report.add(
            "mcp-configs",
            "fail",
            f"missing fragments: {', '.join(missing_frags)}",
        )
    elif missing_cmds:
        report.add(
            "mcp-configs",
            "warn",
            f"commands not found: {', '.join(missing_cmds)}",
        )
    else:
        total = sum(len(b.mcp) for b in fleet.bots.values())
        report.add("mcp-configs", "pass", f"{total} MCP entries resolve")


def check_mcp_packages(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """Is each declared MCP package PINNED? (#1058)

    The offline half of the package check, and the half worth a doctor rung:
    it costs no network call, and on the shared library every declaration found
    dead so far was an unpinned one. Deliberately does NOT probe the registry —
    `claudlobby doctor` is run to answer a question quickly, and the network
    signal is opt-in at compose time where its cost is a considered choice.

    WARN, never fail. An unpinned package is unverified, not broken: measured
    here, one of the three unpinned declarations on the shared library resolves
    perfectly well.
    """
    from .mcp_grammar import GrammarUnavailable, grammar

    try:
        gram = grammar(paths)
    except GrammarUnavailable:
        report.add("mcp-packages", "warn", "shared package grammar unavailable — not checked")
        return

    from . import mcp_packages as _mp

    rows = _mp.fleet_declarations(fleet, paths, gram)
    if not rows:
        report.add("mcp-packages", "pass", "no npx/uvx MCP packages declared")
        return
    unpinned = _mp.pinning_findings(rows)
    if unpinned:
        listed = ", ".join(f"{f.fragment}:{f.spec}" for f in unpinned)
        report.add(
            "mcp-packages",
            "warn",
            f"{len(unpinned)} of {len(rows)} declared packages carry NO VERSION "
            f"({listed}) — whatever the registry serves at boot is what runs",
        )
    else:
        report.add("mcp-packages", "pass", f"{len(rows)} declared packages are version-pinned")


# ----------------------------------------------------------------------
# Check: npx package cache
# ----------------------------------------------------------------------


def _npx_cache_detail(result: subprocess.CompletedProcess) -> str:
    """What the probe actually SAID, never a default standing in for it.

    `check-npx-cache.sh` writes its missing-package list to stdout, but both of
    its refusals — no MCP library, and the shared grammar unreachable — exit 2
    and write the reason to STDERR ONLY. A stdout-only reader therefore renders
    an incomplete install as the routine "packages missing", sending an operator
    to `warm-cache` for a condition `warm-cache` cannot fix: the estate's
    unreachable-is-not-empty rule (`source_state.py`) inverted inside the rung
    that reports it.

    The refusal's own words are the detail, because they name the path that is
    missing; a fixed string could only ever name the class.
    """
    out = result.stdout.strip() if result.stdout else ""
    err = result.stderr.strip() if result.stderr else ""
    if result.returncode == 2:
        # A refusal names the path it could not reach, on stderr today. Read
        # either stream rather than pinning that: what must never happen is
        # this rung inventing a cause the probe did not give.
        if err:
            return err.splitlines()[0]
        if out:
            return out.splitlines()[-1]
        return "probe refused (exit 2) without saying why"
    if out:
        return out.splitlines()[-1]
    if err:
        return err.splitlines()[0]
    return f"probe failed (exit {result.returncode}) and said nothing"


def check_npx_cache(paths: Paths, report: DoctorReport) -> None:
    """Check that the MCP servers' packages are cached — npx AND uvx, since
    #1577 taught the probe both runtimes. The rung keeps its `npx-cache` key
    so an operator's muscle memory and any log grep still work."""
    script = paths.lib / "check-npx-cache.sh"
    if not script.is_file():
        report.add("npx-cache", "warn", "check-npx-cache.sh not found")
        return
    try:
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(paths.root),
        )
        if result.returncode == 0:
            report.add("npx-cache", "pass", "all MCP packages cached (npx + uvx)")
        else:
            # Missing packages land on stdout; a refusal (exit 2) lands on
            # stderr, so the detail cannot be read from one stream alone.
            report.add("npx-cache", "warn", _npx_cache_detail(result)[:200])
    except (subprocess.TimeoutExpired, OSError) as e:
        report.add("npx-cache", "warn", f"check failed: {e}")


# ----------------------------------------------------------------------
# Check: bot services enrolled and running
# ----------------------------------------------------------------------


def check_services(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """Check systemd/launchd enrollment and tmux session presence per bot."""
    import platform

    is_linux = platform.system() == "Linux"
    is_mac = platform.system() == "Darwin"
    down = []
    not_enrolled = []
    misconfigured = []

    for bot_id, bot in fleet.bots.items():
        service_name = f"{fleet.service_prefix}.{bot_id}"
        # Resolve the bot's per-bot tmux socket from its bot.conf via the SSOT
        # resolver (honors TMUX_SOCKET/BOT_SERVICE) rather than reconstructing it
        # from service_name — so the diagnostic checks the socket start-bot.sh
        # actually binds. In a fleet context (FLEET_NAME set) the resolver
        # fail-fasts on a bot.conf with no socket field; record that as a finding
        # (don't hide the misconfig) and fall back to service_name so the liveness
        # probe still runs rather than aborting the whole sweep.
        try:
            socket = tmux_socket_for_bot(paths.bot_runtime(bot_id)) or service_name
        except ValueError:
            misconfigured.append(bot_id)
            socket = service_name

        # Check tmux session
        tmux_ok = False
        try:
            result = subprocess.run(
                # Each bot runs on its own tmux server (-L <socket>); a
                # default-socket check is blind.
                ["tmux", "-L", socket, "has-session", "-t", bot_id],
                capture_output=True,
                timeout=5,
            )
            tmux_ok = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Check service enrollment
        enrolled = False
        if is_linux:
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "is-enabled", service_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                enrolled = result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        elif is_mac:
            plist = Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
            enrolled = plist.is_file()

        if not enrolled:
            not_enrolled.append(bot_id)
        elif not tmux_ok:
            down.append(bot_id)

    if down:
        report.add(
            "services",
            "fail",
            f"{len(down)} bot(s) enrolled but not running: {', '.join(down)}",
        )
    elif not_enrolled:
        report.add(
            "services",
            "warn",
            f"{len(not_enrolled)} bot(s) not enrolled: {', '.join(not_enrolled)}",
        )
    else:
        report.add("services", "pass", f"{len(fleet.bots)} bot(s) enrolled and running")

    if misconfigured:
        report.add(
            "bot-sockets",
            "fail",
            f"{len(misconfigured)} bot(s) with no resolvable tmux socket "
            f"(bot.conf missing TMUX_SOCKET/BOT_SERVICE — regenerate): "
            f"{', '.join(misconfigured)}",
        )


# ----------------------------------------------------------------------
# Check: credentials valid (lightweight probe)
# ----------------------------------------------------------------------


def _curl_with_config(
    headers: dict[str, str], extra_args: list[str]
) -> subprocess.CompletedProcess:
    """Run curl with auth headers in a tmpfile to avoid leaking tokens in ps output."""
    import tempfile

    fd, cfg_path = tempfile.mkstemp(suffix=".cfg")
    try:
        with os.fdopen(fd, "w") as f:
            for k, v in headers.items():
                f.write(f'header = "{k}: {v}"\n')
        return subprocess.run(
            ["curl", "-sS", "--config", cfg_path, "--max-time", "10"] + extra_args,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        os.unlink(cfg_path)


#: Probes, keyed by the env-var name a fleet DECLARES. Membership here is what
#: makes a credential probeable; it is never what makes it probed. A var reaches
#: a probe only by being declared AND resolving to a value through the .env
#: cascade -- see `check_credentials`.
#:
#: Each entry names the host it contacts, because a diagnostic that opens a
#: network connection should be able to say where to (#1377).
_CREDENTIAL_PROBES: dict[str, tuple[str, str]] = {
    "GITHUB_PAT": ("github", "api.github.com"),
    "GITHUB_PERSONAL_ACCESS_TOKEN": ("github", "api.github.com"),
    "RAILWAY_PERSONAL_TOKEN": ("railway", "backboard.railway.app"),
    "RAILWAY_PERSONAL_PROJECT_TOKEN": ("railway", "backboard.railway.app"),
}

#: The query each Railway token is DEFINITIONALLY able to answer:
#: (graphql query, scope).
#:
#: ONE PROBE FOR ALL TOKENS IS THE BUG. Railway issues two kinds of token and
#: they answer different questions -- a workspace-scoped token is not bound to
#: an account, so it cannot answer `me` BY CONSTRUCTION, and probing it that
#: way reports a working credential as dead.
#:
#: A CREDENTIAL HAS AN IDENTITY AND A REACH; THEY ARE INDEPENDENT. An identity
#: endpoint answers nothing about access -- probe the OPERATION you need. The
#: GitHub App branch in `lib/creds-check.sh` already embodies this (it probes
#: `/installation/repositories` because a `ghs_` token 403s on `/user`, D13).
#: Durable home for the rule: Claudlobby#1400.
#:
#: KNOWN DUPLICATION, named rather than left to be rediscovered. This table
#: also exists in bash, as `_railway_token_specs` in `lib/creds-check.sh`, and
#: the two cannot share a literal across languages. A previous version carried
#: the comment "matches creds-check.sh" -- a copy kept in sync by hand, which
#: went stale the moment the bash side was fixed, and that is how `doctor` came
#: to probe a retired variable. Change one, change both.
#:
#: `TestRailwayProbesMatchTheDeclaredContract` is the part that EXECUTES: it
#: fails when this table and `library/integrations/railway.md` stop naming the
#: same variables.
#:
#: It exists because the two are changed by DIFFERENT PRs and nothing watched
#: the pair. #1381 (661e4db, taking fork (a) of #1377) moved this block from a
#: direct `os.environ` read to a declaration-keyed registry and carried
#: `RAILWAY_API_TOKEN` into it -- correct at the time, since the contract still
#: declared that name. The change that retires the name is a different PR. So
#: neither is wrong alone, and together they leave the registry and the contract
#: naming different variables, with Railway declared but never probed.
#:
#: Measured in exactly that state, on a fleet declaring both integrations and
#: holding every declared token in the cascade:
#:
#:     [pass] 1 probed OK (GITHUB_PAT via mcp/github); contacted api.github.com;
#:            2 declared var(s) have no probe: RAILWAY_PERSONAL_PROJECT_TOKEN,
#:            RAILWAY_PERSONAL_TOKEN
#:
#: One outbound call. Railway declared, credentials stored, never contacted --
#: and the verdict is PASS. The loss is disclosed, but in a trailing clause
#: under a green headline, so it is not silent and not loud either. On a fleet
#: declaring ONLY railway the primary clause reads "no probeable credential
#: declared by this fleet", which is false for a fleet that declares two.
#:
#: That `else` branch is main's and is tracked separately (#1513); what this
#: table fixes is the drift that reaches it. The test is what keeps it fixed:
#: a comment saying "change one, change both" cannot see a second PR.
_RAILWAY_QUERIES: dict[str, tuple[str, str]] = {
    "RAILWAY_PERSONAL_TOKEN": ("me{email}", "account"),
    "RAILWAY_PERSONAL_PROJECT_TOKEN": ("projects{edges{node{id}}}", "workspace"),
}


def _probe_github(token: str, _var: str) -> str | None:
    """None on success, else a short failure reason."""
    try:
        result = _curl_with_config(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            ["-o", "/dev/null", "-w", "%{http_code}", "https://api.github.com/user"],
        )
    except (subprocess.TimeoutExpired, OSError):
        return "timeout"
    code = result.stdout.strip()
    return None if code == "200" else f"HTTP {code}"


def _probe_railway(token: str, var: str) -> str | None:
    """None on success, else a short failure reason.

    Railway answers an auth failure with HTTP 200 and an ``errors`` body, so the
    code alone is not the verdict.

    The query is chosen by SCOPE (`_RAILWAY_QUERIES`), never fixed: probing a
    workspace token with `me` reports a working credential as dead.
    """
    query, _scope = _RAILWAY_QUERIES[var]
    try:
        result = _curl_with_config(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            [
                "-X",
                "POST",
                "-w",
                "\n%{http_code}",
                "-d",
                f'{{"query":"query{{{query}}}"}}',
                "https://backboard.railway.app/graphql/v2",
            ],
        )
    except (subprocess.TimeoutExpired, OSError):
        return "timeout"
    lines = result.stdout.strip().rsplit("\n", 1)
    body = lines[0] if len(lines) == 2 else ""
    code = lines[-1]
    if code != "200":
        return f"HTTP {code}"
    if body:
        try:
            resp = json.loads(body)
            if "errors" in resp:
                return "graphql error: " + resp["errors"][0].get("message", "unknown")[:120]
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    return None


_PROBE_FNS = {"github": _probe_github, "railway": _probe_railway}


def check_credentials(
    fleet: FleetConfig, paths: Paths, report: DoctorReport
) -> None:
    """Probe the credentials THIS FLEET DECLARES, resolved through the .env cascade.

    Two opposite defects used to live here, and fixing either alone makes the
    other worse, which is why they move together (#1377).

    **It read `os.environ` directly.** A `RAILWAY_API_TOKEN` in the operator's
    shell was probed -- a live authenticated call to a third party -- and its
    result reported as *this fleet's* health, on a fleet declaring no Railway
    anything. So a fleet's verdict depended on what happened to be exported in
    the shell that ran the command, and a stranger following the README made an
    outbound call they were never told about. Measured: a clean seed fleet
    reported `[FAIL] credentials -- invalid: RAILWAY_API_TOKEN`.

    **It also never read the .env cascade**, which is where the runtime actually
    resolves credentials. So the mirror case reported `no credential env vars
    found to probe` on all four fleets of this estate, every one of which
    declares `GITHUB_PAT` and stores it in a tier. Gating on declaration WITHOUT
    fixing this would have turned "probes the wrong thing" into "probes
    nothing" -- a check that had stopped working while reading as clean.

    So: declarations come from `credentials.declared_for_fleet` (which routes to
    `mcp_resolve.required_vars` and covers both the MCP and integration
    surfaces) and values from `credentials.resolved_view` (the same four-tier
    cascade `Paths.env_resolved` gives the runtime). Neither is re-derived here
    -- a private copy is how the two disagree, and a checker disagreeing with
    the runtime is the bug class this function is an instance of.

    **The process environment is deliberately NOT a value source.** A bot
    resolves from the cascade, never from the operator's interactive shell, so
    an ambient-only value says nothing about whether the fleet works. It is
    detected and REPORTED rather than silently probed or silently dropped: that
    state is real, confusing, and worth naming.

    Coverage is stated on every outcome. "Nothing was probed" has three causes
    with different remedies -- nothing probeable is declared, a declared var has
    no value, or a value exists only in the shell -- and collapsing them into
    one reassuring line is the defect this function already shipped once.
    """
    from .credentials import declared_for_fleet, resolved_view

    try:
        declarations, _ = declared_for_fleet(fleet, paths)
    except Exception:  # noqa: BLE001 - a broken manifest must not crash doctor
        report.add("credentials", "warn", "could not read fleet declarations")
        return
    # ResolverUnavailable is RAISED by design and must not be folded into an
    # empty mapping: "the cascade could not be read" and "the cascade holds no
    # value" have opposite remedies, and collapsing them would report every
    # declared credential as unset — a fresh instance of the unreachable-vs-empty
    # defect, inside a fix for its sibling. Caught by this function's own
    # positive control, which is the only test here that asserts a probe FIRES.
    from .env_tiers import ResolverUnavailable

    try:
        _, values, _ = resolved_view(paths)
    except ResolverUnavailable as exc:
        report.add(
            "credentials",
            "warn",
            f"cannot read the .env cascade, so nothing was probed: {exc}",
        )
        return
    except Exception:  # noqa: BLE001 - an unreadable tier file must not crash doctor
        report.add("credentials", "warn", "cannot read the .env cascade, so nothing was probed")
        return

    declared_names = {d.var for d in declarations}
    source_of = {d.var: f"{d.kind}/{d.source}" for d in declarations}

    probed_ok: list[str] = []
    failures: list[str] = []
    shell_only: list[str] = []
    no_value: list[str] = []
    hosts: set[str] = set()

    for var in sorted(declared_names & set(_CREDENTIAL_PROBES)):
        kind, host = _CREDENTIAL_PROBES[var]
        value = values.get(var)
        if value:
            hosts.add(host)
            reason = _PROBE_FNS[kind](value, var)
            if reason:
                failures.append(f"{var} ({reason})")
            else:
                probed_ok.append(f"{var} via {source_of.get(var, kind)}")
        elif os.environ.get(var):
            shell_only.append(var)
        else:
            no_value.append(var)

    # Declared, but nothing here knows how to probe it. Named so a pass cannot
    # be read as "every declared credential was checked".
    unprobeable = sorted(declared_names - set(_CREDENTIAL_PROBES))

    scope: list[str] = []
    if hosts:
        scope.append("contacted " + ", ".join(sorted(hosts)))
    if shell_only:
        scope.append(
            f"{', '.join(shell_only)} set in your shell but absent from every "
            f".env tier, so a bot would not see it — not probed"
        )
    if no_value:
        scope.append(f"declared with no value: {', '.join(no_value)} (see env-vars)")
    if unprobeable:
        scope.append(f"{len(unprobeable)} declared var(s) have no probe: {', '.join(unprobeable)}")
    tail = ("; " + "; ".join(scope)) if scope else ""

    if failures:
        report.add("credentials", "fail", f"invalid: {', '.join(failures)}{tail}")
    elif probed_ok:
        report.add(
            "credentials", "pass", f"{len(probed_ok)} probed OK ({', '.join(probed_ok)}){tail}"
        )
    elif shell_only or no_value:
        report.add("credentials", "warn", f"nothing probed{tail}")
    else:
        # Correct-and-complete silence: this fleet declares nothing this check
        # knows how to probe, so there was never anything to do. Says so, rather
        # than implying credentials were validated.
        report.add(
            "credentials",
            "pass",
            f"no probeable credential declared by this fleet{tail}",
        )


# ----------------------------------------------------------------------
# Check: the Claudron door (CLI presence, capability probe, compat floor,
# session-loop evidence)
# ----------------------------------------------------------------------

#: A hook degradation older than this is history, not a live symptom.
_LOOP_DEGRADATION_WINDOW_DAYS = 7


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run *argv*, returning (returncode, stdout). Absent binary ⇒ (127, "")."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except FileNotFoundError:
        return 127, ""
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def _claudron_probe(vault: str) -> tuple[str, str]:
    """Claudron's sanctioned capability probe (CLI_CONTRACT §Capability probe).

    Returns (status, detail) for a doctor row. Branches on the exit code, never
    on message text: 0 ⇒ envelope on stdout, 3 ⇒ engine present but no vault.
    """
    rc, out = _run(["claudron", "status", "--json", "--vault", vault])
    if rc == 3:
        return "warn", f"claudron installed but no vault resolved at {vault}"
    if rc != 0:
        return "warn", f"`claudron status --json` exited {rc} for {vault}"
    try:
        data = json.loads(out)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "warn", "`claudron status --json` did not return the CLI envelope"
    # engine_version does not exist before 0.3.0 — index it defensively.
    version = data.get("engine_version", "unknown (pre-0.3.0)")
    return "pass", (
        f"engine {version}, vault {data.get('root', vault)} "
        f"({data.get('total_docs', '?')} notes)"
    )


def _floor_row_state(cap, cli_present: bool) -> tuple[str, str]:
    """Resolve one COMPAT_FLOOR row to (status, detail).

    Parked rows never probe and never read "unmet" — they are demand-gated by a
    recorded decision, not missing capability.
    """
    if cap.parked:
        return "pass", f"parked ({cap.parked}) — demand-gated, not built, not required"
    if cap.probe == PROBE_API:
        if vault_api_available():
            return "pass", f"met — {cap.requires}"
        return "warn", (
            f"unmet — {cap.requires}: install `claudlobby[vault]` "
            f"(the .claudron bridge parser is the degraded fallback)"
        )
    if cap.probe.startswith(PROBE_VERB_PREFIX):
        verb = cap.probe[len(PROBE_VERB_PREFIX) :]
        if not cli_present:
            return "warn", f"unmet — no claudron CLI on PATH (needs `{verb}`)"
        rc, _ = _run(["claudron", verb, "--help"], timeout=10.0)
        return (
            ("pass", f"met — `claudron {verb}` resolves")
            if rc == 0
            else ("warn", f"unmet — `claudron {verb}` not available (exit {rc})")
        )
    return "pass", (
        f"not probeable — {cap.requires} "
        f"(annotation: {cap.default_order_release})"
    )


def _loop_evidence(vault: str) -> tuple[str, str]:
    """Loop-execution evidence for one vault: is a wired loop actually running?

    Two independent signals, neither of which needs the network:

    * **git** — SessionEnd pushes. Commits piling up unpushed is the silent
      push-loss symptom the program's risk table names; a stale HEAD is a loop
      that stopped capturing.
    * **hooks.log** — the engine only writes there when a hook *degrades*
      (fail-open by contract), so its absence proves nothing but its recent
      contents prove a lot.
    """
    root = Path(vault).expanduser()
    if not root.is_dir():
        return "warn", f"{vault}: not present on this host"

    bits: list[str] = []
    degraded = False

    rc, out = _run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], 5.0)
    if rc != 0:
        bits.append("not a git repo — no sync evidence")
    else:
        rc, out = _run(["git", "-C", str(root), "log", "-1", "--format=%cI"], 5.0)
        bits.append(
            f"last commit {out.strip()}"
            if rc == 0 and out.strip()
            else "no commits yet"
        )
        rc, out = _run(
            ["git", "-C", str(root), "rev-list", "--count", "@{upstream}..HEAD"], 5.0
        )
        if rc != 0:
            bits.append("no upstream — pushes are not configured")
        elif out.strip() not in ("0", ""):
            bits.append(f"{out.strip()} commit(s) unpushed")
            degraded = True
        else:
            bits.append("in sync with upstream")

    log_path = root / ".claudron" / "hooks.log"
    if not log_path.is_file():
        bits.append("no hook degradation logged")
    else:
        try:
            last = [ln for ln in log_path.read_text().splitlines() if ln.strip()][-1]
        except (OSError, IndexError):
            last = ""
        if not last:
            bits.append("no hook degradation logged")
        else:
            bits.append(f"last hook degradation: {last[:160]}")
            stamp = last.split(" ", 1)[0]
            try:
                age = datetime.now() - datetime.fromisoformat(stamp)
                if age <= timedelta(days=_LOOP_DEGRADATION_WINDOW_DAYS):
                    degraded = True
            except ValueError:
                degraded = True  # unparseable stamp — surface it rather than hide it

    return ("warn" if degraded else "pass"), f"{vault}: " + "; ".join(bits)


def check_claudron(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """The claudron door check ``claudron_compat``'s docstring promises.

    Silent for a fleet with no vault-wired bot — nothing to diagnose. For a
    vault-wired fleet it reports, in order: CLI presence, the engine capability
    probe, every COMPAT_FLOOR row as met/unmet/parked, and per-vault
    loop-execution evidence so a wired-but-dead session loop is visible in
    steady state.

    Warn-level throughout, deliberately: a host can be composing for a fleet it
    does not itself run, and a diagnostic that fails the whole doctor run on
    that basis would be asserting more than the contract does.
    """
    wired = {
        bot_id: bot.claudron_vault_path
        for bot_id, bot in fleet.bots.items()
        if bot.claudron_vault_path
    }
    if not wired:
        return

    cli_present = shutil.which("claudron") is not None
    if cli_present:
        report.add("claudron-cli", "pass", f"{len(wired)} vault-wired bot(s)")
        status, detail = _claudron_probe(sorted(set(wired.values()))[0])
        report.add("claudron-engine", status, detail)
    else:
        report.add(
            "claudron-cli",
            "warn",
            f"{len(wired)} vault-wired bot(s) but no claudron on PATH — bots reach "
            f"the vault through the CLI (see {CLAUDRON_INTEGRATION_URL})",
        )

    for cap in COMPAT_FLOOR:
        status, detail = _floor_row_state(cap, cli_present)
        report.add(f"claudron-floor: {cap.feature}", status, detail)

    for vault in sorted(set(wired.values())):
        status, detail = _loop_evidence(vault)
        report.add("claudron-loop", status, detail)


# ----------------------------------------------------------------------
# Check: fleet.yaml validates
# ----------------------------------------------------------------------


def check_switches(fleet: FleetConfig, paths: Paths, report: DoctorReport) -> None:
    """The `switches` rung — every knob the system ships, and its state here.

    This rung exists because of what the defaults flip does NOT change: four
    doors still ship off, and a door that ships off with no surface is
    indistinguishable from a door that does not exist. The ruling that put
    everything else on is the same ruling that says what stays off must be
    NAMED where the operator looks, with the one line that arms it — so this
    is not a nice-to-have beside the flip, it is the other half of it.

    Never a FAILURE, and never a warning either. A fleet that turned the
    re-check off did so on purpose; flagging it would train operators to
    ignore the rung, which is how the surface stops working. It reports.
    """
    from . import switches as _sw

    try:
        rows = _sw.resolve(paths, fleet)
    except Exception as exc:  # noqa: BLE001 — a health command never crashes
        report.add("switches", "warn", f"could not resolve: {exc}")
        return
    report.switch_rows = rows
    report.add("switches", "pass", _sw.summary_line(rows))


def check_ignition(
    fleet: FleetConfig,
    paths: Paths,
    report: DoctorReport,
    doors: list | None = None,
) -> None:
    """The composite question #1633 exists for: does ANYTHING give an idle
    bot on this fleet a turn?

    Five doors can start a bot's own turn, and a reviewed 21-bot host had
    four of five unarmed with no surface saying so — an operator reading
    `task-recheck: on` and a validator warning buried in a count concluded
    the beat ran. This rung answers the composite question directly.

    Reads DECLARED state only (:func:`ignition.ignition_doors`) — whether an
    armed door is actually ENROLLED is composed-vs-enrolled drift, #839/#1040's
    door, not this one's; the detail line says so rather than silently
    claiming a narrower guarantee than "armed" sounds like.

    Never a failure, and a WARN only when idle turns are actually in play —
    see :func:`ignition.ignition_gap` for why a manager-less fleet PASSes on
    that fact alone, and `TestIgnitionGapIsTheRungsOwnPredicate` for the pin
    that keeps this ladder and that predicate answering alike.

    ``doors`` is passed by :func:`run_doctor`, which resolves them once for
    every rung that asks — see :func:`ignition.ignition_gap`.
    """
    from .ignition import ignition_doors, ignition_warning_tail

    doors = ignition_doors(fleet, paths) if doors is None else doors
    armed = [d for d in doors if d.armed]
    if armed:
        report.add(
            "ignition",
            "pass",
            f"{len(armed)}/{len(doors)} door(s) armed (declared state): "
            + ", ".join(d.name for d in armed),
        )
        return
    if not fleet.leaf_manager_bots():
        report.add(
            "ignition",
            "pass",
            "no leaf manager on this fleet — no idle-turn beat applies",
        )
        return
    cheapest = next(d for d in doors if d.name == "briefing.slots")
    report.add(
        "ignition",
        "warn",
        f"0/{len(doors)} door(s) armed — nothing gives an idle bot a turn "
        "(declared state; an armed-but-unenrolled door reads differently — "
        "see #839/#1040)."
        # A leaf manager exists on this path, and doctor's goal-binding rung
        # warns for ANY leaf manager with no projects (its own plain line
        # covers the bot the check-in is not composed onto), so `not
        # fleet.projects` is exactly that rung's condition here. `validate`
        # has no such line and so asks a narrower question — #1680.
        + ignition_warning_tail(
            cheapest.arm_line, goal_binding_warns=not fleet.projects
        ),
    )


def check_delivery(
    fleet: FleetConfig, paths: Paths, report: DoctorReport, *,
    run: bool = True,
) -> None:
    """Is finished work actually DELIVERED? (#1745)

    Two states where work is 100% done and 0% delivered: a branch ahead of the
    default branch with no PR, and an open PR whose head is BEHIND its own
    branch ref. Neither covers the other. The live instances are in #1745 and
    #1746 respectively.

    WARN, never fail. Like `check_claudron` and `check_manifest_provenance` this
    reports on repositories claudlobby does not own, and "a branch with no PR"
    is not automatically wrong — only a human can tell stranded delivery from
    work legitimately in flight, which is why the rung reports and stops.

    IT STATES ITS BOUNDS WHATEVER THE VERDICT, including when it is clean:
    silence that could mean "nothing undelivered" or could mean "did not look"
    is the class this rung exists to close, and reproducing it here would be the
    defect one level up. When it is not run at all, it says THAT rather than
    saying nothing.
    """
    from .delivery import DEFAULT_BUDGET_S, check_repo

    if not run:
        report.add("delivery", "skip",
                   "not run (--no-delivery): undelivered work is UNCHECKED, "
                   "not clean — re-run without the flag")
        return

    repos: dict[str, str] = {}
    for bot in fleet.bots.values():
        sc = getattr(bot, "scope", None)
        for repo in (getattr(sc, "repos", None) or []):
            qualified = repo if "/" in repo else (
                f"{sc.org}/{repo}" if getattr(sc, "org", None) else repo)
            if "/" not in qualified:
                continue
            # A local checkout makes the branch half local-only; without one the
            # rung still runs its PR half and says the other did not run.
            name = qualified.split("/", 1)[1]
            found = ""
            for bot_name in fleet.bots:
                cand = paths.runtime_bots / bot_name / "projects" / name
                if (cand / ".git").exists():
                    found = str(cand)
                    break
            repos.setdefault(qualified, found)

    if not repos:
        report.add("delivery", "pass",
                   "no repos in any bot's scope — nothing to reconcile")
        return

    # ONE deadline for the whole run, not one per repo (#1745 review). A
    # per-repo budget has no run-wide ceiling -- it is budget x repos, so what a
    # human waits through grew with fleet size, and that ceiling is what gets
    # `--no-delivery` aliased permanently.
    import time as _time
    run_deadline = _time.monotonic() + DEFAULT_BUDGET_S

    findings: list[str] = []
    bounds: list[str] = []
    full = partial = unreached = 0
    not_clean: list[str] = []
    for repo, checkout in sorted(repos.items()):
        f = check_repo(repo, checkout, deadline=run_deadline)
        for b in f.no_pr:
            findings.append(f"{repo} {b}: ahead of default, NO PR — committed and "
                            "pushed, invisible to every read door")
        for line in f.stale_pr_head:
            findings.append(f"{repo} {line}")
        bounds.append(f"{repo}: {f.bound_line()}")
        # THE RUNG CONSULTS `clean` -- it is the stated invariant, so it has to
        # be the thing that decides, not a property only tests mention. An
        # invariant nothing reads is decoration, and a correct one nothing reads
        # protects exactly as much as a wrong one (review).
        if not f.clean:
            not_clean.append(repo)
        if not f.checked:
            unreached += 1
        elif f.unchecked:
            partial += 1
        else:
            full += 1

    # Run-wide, the budget can stop PART-WAY THROUGH a repo, which a per-repo
    # clock could not do -- so coverage is stated as three counts rather than
    # left to be inferred from the per-repo bounds. Leading with it matters when
    # it is short: "nothing undelivered" over two of four repos is a different
    # claim from the same words over all four.
    coverage = f"{full}/{len(repos)} repo(s) fully checked"
    if partial:
        coverage += f", {partial} partially"
    if unreached:
        coverage += f", {unreached} NOT REACHED (run budget spent)"
    detail = coverage + " — " + "; ".join(bounds)
    if findings:
        report.add("delivery", "warn",
                   f"{len(findings)} undelivered: " + " | ".join(findings[:4])
                   + (f" (+{len(findings) - 4} more)" if len(findings) > 4 else "")
                   + f" — bounds: {detail}")
    elif not_clean:
        # NO FINDINGS IS NOT A PASS WHEN COVERAGE WAS SHORT. An earlier version
        # reported `pass` here on the reasoning that "no findings is still a
        # pass; the bound is what changed" -- and a test pinned that as correct,
        # so the suite went green certifying that a repo nobody looked at may be
        # reported healthy. A health check that did not finish looking has not
        # produced a clean answer; it has produced no answer, and the two must
        # not share a status.
        report.add("delivery", "warn",
                   f"nothing undelivered IN WHAT WAS CHECKED, but "
                   f"{len(not_clean)} repo(s) were not fully checked "
                   f"({', '.join(sorted(not_clean)[:3])}"
                   + (", …" if len(not_clean) > 3 else "")
                   + f") — this is not a clean answer for those — bounds: {detail}")
    else:
        report.add("delivery", "pass", f"nothing undelivered — bounds: {detail}")


def check_manifest_provenance(
    fleet: FleetConfig, paths: Paths, report: DoctorReport
) -> None:
    """Did the fleet's manifest move under the running fleet? (#1722)

    `diff` answers "what would generate change now"; nothing answered "did my
    INPUTS change since the runtime was built". In the outage this comes from, a
    stopped rebase checked out another branch's tree, the manifest reverted on
    disk, the next generate composed from the reverted file, and every surface
    read healthy.

    WARN, never fail, for the same reason `check_claudron` is warn-level: this
    rung reports the state of a sibling checkout that claudlobby does not own.
    """
    from .composer import (
        MANIFEST_PROVENANCE_SCHEMA,
        changed_manifest_inputs,
        manifest_change_attribution,
        manifest_warnings,
        read_manifest_provenance,
    )

    # SILENT on a fleet that was never composed, the way check_claudron is
    # silent for a fleet with no vault-wired bot: there is no runtime whose
    # inputs could have moved, and "regenerate to record what this runtime was
    # composed from" is nonsense addressed to a runtime that does not exist.
    # doctor already has rungs for "you have not generated yet".
    if not paths.runtime_bots.is_dir() or not any(paths.runtime_bots.iterdir()):
        return

    prov = read_manifest_provenance(paths)
    if prov is None:
        report.add("manifest-provenance", "warn",
                   "composed by a claudlobby without provenance — run `generate` "
                   "to record what this runtime was composed from")
        return
    if prov.get("schema") != MANIFEST_PROVENANCE_SCHEMA:
        report.add("manifest-provenance", "warn",
                   f"provenance schema {prov.get('schema')!r} is not the "
                   f"{MANIFEST_PROVENANCE_SCHEMA} this build reads — not "
                   "interpreting it; run `generate` to re-record")
        return

    changed = changed_manifest_inputs(fleet, paths, prov)
    if changed:
        # HOW it changed, asked of the tree NOW — the compose-time record is a
        # snapshot and cannot answer this once the git state has been repaired.
        how = manifest_change_attribution(fleet, paths)
        report.add("manifest-provenance", "warn",
                   f"manifest changed since the running fleet was composed "
                   f"({', '.join(changed)}; composed {prov.get('composed_at')})"
                   + (f"; {how}" if how else "") +
                   " — run `generate`, then restart the bots that read it at "
                   "session start (bot.conf and CLAUDE.md are read once, at startup)")
        return

    # Compose-time conditions are reported even when nothing has changed since:
    # a fleet composed FROM a wedged checkout is not made sound by the manifest
    # sitting still afterwards.
    for warning in manifest_warnings(prov):
        report.add("manifest-provenance", "warn", f"at compose time: {warning}")
        return

    report.add("manifest-provenance", "pass",
               f"inputs unchanged since compose ({prov.get('composed_at')})")


def check_fleet_validation(
    fleet: FleetConfig, paths: Paths, report: DoctorReport
) -> None:
    """Run the standard fleet validator and surface errors/warnings."""
    val_report = validate(fleet, paths)
    if val_report.has_errors:
        report.add(
            "fleet-yaml",
            "fail",
            f"{len(val_report.errors)} error(s): {val_report.errors[0][:100]}",
        )
    elif val_report.warnings:
        report.add(
            "fleet-yaml",
            "warn",
            f"{len(val_report.warnings)} warning(s)",
        )
    else:
        report.add("fleet-yaml", "pass", "fleet.yaml valid")


def check_goal_binding(
    fleet: FleetConfig,
    paths: Paths,
    report: DoctorReport,
    doors: list | None = None,
) -> None:
    """Whether this fleet is bound to a goal it can actually dispatch against.

    Its own named rung rather than part of `fleet-yaml`'s `N warning(s)`:
    these three findings each stop the check-in beat from producing work, and
    a count is not a thing an operator can act on. Calls the validator's
    helper — one definition, so the two surfaces cannot drift.
    """
    from .ignition import NO_DOOR_CO_REQUISITE, ignition_gap
    from .validator import ValidationReport, _validate_goal_binding

    sub = ValidationReport()
    _validate_goal_binding(fleet, paths, sub, doors=doors)
    if sub.warnings:
        report.add(
            "goal-binding",
            "warn",
            "; ".join(sub.warnings),
        )
        return

    if not fleet.projects:
        # The applicability gate adopted from `check_ignition` (#1680) —
        # rationale beside the conjunct itself, in `ignition.ignition_gap`.
        # Without it the two rungs printed a WARN and a PASS about one
        # question, each internally consistent, which is how it survived.
        if not fleet.leaf_manager_bots():
            report.add(
                "goal-binding",
                "pass",
                "no leaf manager on this fleet — nothing dispatches, so no "
                "project registry applies",
            )
            return
        detail = (
            "no projects: neither a projects.yaml nor any bot's scope.repos — "
            "nothing to dispatch against"
        )
        if ignition_gap(fleet, paths, doors):
            detail += NO_DOOR_CO_REQUISITE
        report.add("goal-binding", "warn", detail)
        return

    source = "derived from scope.repos" if fleet.projects_derived else "projects.yaml"
    report.add(
        "goal-binding",
        "pass",
        f"{len(fleet.projects)} project(s) ({source})"
        + ("; fleet mission declared" if fleet.mission else "; no fleet.mission"),
    )


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


def run_doctor(fleet: FleetConfig, paths: Paths, *,
               delivery: bool = True) -> DoctorReport:
    """Run all doctor checks and return the report."""
    report = DoctorReport()
    # Resolved ONCE for the three rungs that ask the same question (#1680):
    # ignition_doors goes through the switch cascade, which shells out to
    # lib/env-tiers.sh. Falling back to None rather than guarding here keeps
    # the HOIST itself from becoming a new failure point — where a resolver
    # failure surfaces is then whatever it is on main. Deliberately not a
    # claim about WHICH rung that is: on a fleet with a leaf manager
    # `check_fleet_validation` runs first and re-resolves inside validate(),
    # so it would land there rather than at `check_ignition`.
    try:
        from .ignition import ignition_doors

        doors = ignition_doors(fleet, paths)
    except Exception:  # noqa: BLE001 — a health command never crashes early
        doors = None
    check_fleet_validation(fleet, paths, report)
    check_manifest_provenance(fleet, paths, report)
    check_delivery(fleet, paths, report, run=delivery)
    check_goal_binding(fleet, paths, report, doors=doors)
    check_switches(fleet, paths, report)
    check_ignition(fleet, paths, report, doors=doors)
    check_env_vars(fleet, paths, report)
    check_mcp_configs(fleet, paths, report)
    check_mcp_packages(fleet, paths, report)
    check_npx_cache(paths, report)
    check_services(fleet, paths, report)
    check_credentials(fleet, paths, report)
    check_claudron(fleet, paths, report)
    return report


def format_report(report: DoctorReport) -> str:
    """Format the doctor report for terminal output."""
    lines = ["", "=== claudlobby doctor ===", ""]
    for check in report.checks:
        if check.status == "pass":
            icon = "PASS"
        elif check.status == "warn":
            icon = "WARN"
        elif check.status == "skip":
            # A rung an operator deliberately turned off is not a failure, and
            # rendering it as one (the `else` below) trains people to ignore
            # FAIL. It is also not a WARN: a routine `--no-delivery` run would
            # then always warn, which trains the same thing one level down. SKIP
            # is its own word, counted separately, and the rung's own detail says
            # what is consequently unchecked (#1745).
            icon = "SKIP"
        else:
            icon = "FAIL"
        detail = f" — {check.detail}" if check.detail else ""
        lines.append(f"  [{icon}] {check.name}{detail}")
    lines.append("")
    p, w, f = len(report.passed), len(report.warnings), len(report.failures)
    sk = len([c for c in report.checks if c.status == "skip"])
    lines.append(f"  {p} passed, {w} warnings, {f} failures"
                 + (f", {sk} skipped" if sk else ""))
    lines.append("")
    if report.switch_rows:
        from . import switches as _sw
        lines.append(_sw.format_table(report.switch_rows))
    return "\n".join(lines)
