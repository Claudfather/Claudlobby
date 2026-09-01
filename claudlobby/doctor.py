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


# ----------------------------------------------------------------------
# Check: npx package cache
# ----------------------------------------------------------------------


def check_npx_cache(paths: Paths, report: DoctorReport) -> None:
    """Check if npx packages for MCP servers are cached."""
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
            report.add("npx-cache", "pass", "all MCP npx packages cached")
        else:
            # Script outputs missing packages on failure
            detail = (
                result.stdout.strip().split("\n")[-1]
                if result.stdout
                else "packages missing"
            )
            report.add("npx-cache", "warn", detail[:200])
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
    "RAILWAY_API_TOKEN": ("railway", "backboard.railway.app"),
}


def _probe_github(token: str) -> str | None:
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


def _probe_railway(token: str) -> str | None:
    """None on success, else a short failure reason.

    Railway answers an auth failure with HTTP 200 and an ``errors`` body, so the
    code alone is not the verdict.
    """
    try:
        result = _curl_with_config(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            [
                "-X",
                "POST",
                "-w",
                "\n%{http_code}",
                "-d",
                '{"query":"query{me{name}}"}',
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
            reason = _PROBE_FNS[kind](value)
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


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


def run_doctor(fleet: FleetConfig, paths: Paths) -> DoctorReport:
    """Run all doctor checks and return the report."""
    report = DoctorReport()
    check_fleet_validation(fleet, paths, report)
    check_env_vars(fleet, paths, report)
    check_mcp_configs(fleet, paths, report)
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
        else:
            icon = "FAIL"
        detail = f" — {check.detail}" if check.detail else ""
        lines.append(f"  [{icon}] {check.name}{detail}")
    lines.append("")
    p, w, f = len(report.passed), len(report.warnings), len(report.failures)
    lines.append(f"  {p} passed, {w} warnings, {f} failures")
    lines.append("")
    return "\n".join(lines)
