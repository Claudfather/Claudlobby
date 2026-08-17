"""Shared fixtures for claudlobby tests."""

from __future__ import annotations
import json
import os
import stat
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import DEFAULT_GUARDRAILS


@pytest.fixture(autouse=True)
def _isolate_claudlobby_root(monkeypatch):
    """Bot sessions and timer jobs export CLAUDLOBBY_ROOT, and Paths.detect()
    honors it over the cwd walk-up — strip it so hint-less detection in tests
    is hermetic. Tests that need it set it explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)


# Captures chat id, the (expanded) state dir the caller resolved, and the
# message — the observation point for the emit_* fleet-signal paths.
TG_STUB = (
    "#!/bin/bash\n"
    'printf "%s|%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$TELEGRAM_STATE_DIR" "$1" >> "$TG_CAPTURE"\n'
)


def read_fleet_events(root):
    """Concatenated fleet-event JSONL under <root>/state/events (or '')."""
    events_dir = Path(root) / "state" / "events"
    if not events_dir.is_dir():
        return ""
    return "".join(f.read_text() for f in sorted(events_dir.iterdir()))


def _scrubbed_env(**overrides):
    """os.environ minus the bot-session vars that would short-circuit chat
    resolution (FLEET_PULSE_ESCALATION_CHAT_ID et al), repoint the root, or
    reroute per-bot socket/identity resolution (BOT_*).

    DENYLIST — inherit-and-subtract, only as complete as this prefix list
    (#846). constructed_env below is the ratified inverse default; migrating
    this helper's importers onto it is the named follow-up. New tests should
    not add call sites here."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET", "BOT_"))
    }
    env.update(overrides)
    return env


def constructed_env(**overrides):
    """Minimal CONSTRUCTED child env (#846): built from nothing, never from an
    os.environ copy. Inheriting ambient env makes isolation a denylist — every
    production-pointing variable (FLEET_STATE_PATH, escalation chat ids,
    BOT_DIR) must be remembered and subtracted, and the one nobody thought of
    is the one that leaks. Built minimal, a new isolation-sensitive variable
    is absent by construction. PATH is the one deliberate inheritance (host
    tools); pass PATH=... to prepend stub dirs. LANG pins UTF-8 semantics: with
    no locale at all, grep/awk match the UTF-8 pane-fixture glyphs bytewise and
    lib-common's pane classifiers flip one verdict (test_keepalive_classify /
    test_pane_is_idle, measured). Values are str()-coerced so Paths pass
    through."""
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _write_exec(path, content):
    """Write a stub script and set its exec bits (shared shell-test harness helper)."""
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


SETUP_SYSTEM = Path(__file__).resolve().parent.parent / "lib" / "setup-system"


@pytest.fixture(scope="session")
def setup_system_dry_run():
    """One real `setup-system --dry-run` for the whole suite.

    The 10-phase script probes real tools (dpkg/brew/node/python3/claude/jq), so
    it costs ~0.6s per invocation and is worth running exactly once. Session
    scope rather than module scope because more than one module now asserts
    against this output; a per-module fixture ran it again for each.
    """
    return subprocess.run(
        [str(SETUP_SYSTEM), "--dry-run"], capture_output=True, text=True, timeout=120
    )


_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


@pytest.fixture(scope="session")
def sysbin_excluding(tmp_path_factory):
    """Build a mirror of the host's system bin dirs with named tools absent.

    A test that asserts what happens when a tool is MISSING cannot put the real
    system bin dirs on PATH: whether the branch runs then depends on what the
    host happens to have installed. `claude` at /usr/bin/claude turns such a
    test into a false result — the tool resolves, the absent-tool branch never
    executes, and the test reports on a path it never took.

    Mirroring by exclusion keeps every other utility resolvable (no whitelist to
    fall out of date as the scripts under test grow) while making the excluded
    name genuinely unresolvable. Returns a builder; results are cached per name
    set and the mirror is built once per session (~1.9k symlinks, ~0.25s).
    """
    cache: dict[tuple[str, ...], Path] = {}

    def _build(*names: str) -> Path:
        key = tuple(sorted(names))
        if key in cache:
            return cache[key]
        mirror = tmp_path_factory.mktemp("sysbin")
        excluded = set(names)
        for src in _SYSTEM_BIN_DIRS:
            src_dir = Path(src)
            if not src_dir.is_dir():
                continue
            for entry in src_dir.iterdir():
                if entry.name in excluded:
                    continue
                link = mirror / entry.name
                if link.exists() or link.is_symlink():
                    continue  # first dir on the list wins, as PATH order would
                try:
                    link.symlink_to(entry)
                except OSError:
                    pass  # unreadable/racing entry — not worth failing a test over
        for name in excluded:
            assert not (mirror / name).exists(), f"{name} leaked into the mirror"
        cache[key] = mirror
        return mirror

    return _build


def call_lib_fn(fn: str, value: str) -> str:
    """Source lib-common.sh and call one function on a single value, returning
    stdout. The value travels as a positional arg so the shell never
    interprets it."""
    lib = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"
    r = subprocess.run(
        ["bash", "-c", f'. "{lib}"; {fn} "$1"', "_", value],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def load_lib_module(name: str):
    """Import a lib/*.py script as a module (they have no package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).parent.parent / "lib" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call_script_fn(script: Path, fn: str, *args: str, env: dict | None = None) -> str:
    """Source a bash script (guarded-main style) and call one of its functions,
    returning stdout. Args travel as positionals so the shell never interprets
    the values. Generalizes call_lib_fn to scripts beyond lib-common.sh.
    env: child environment for the call — pass constructed_env(...) for #846
    isolation; default inherits os.environ (legacy callers)."""
    argv = ["bash", "-c", f'. "{script}"; {fn} "$@"', "_", *args]
    r = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=dict(os.environ) if env is None else env,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def realboot_skip_reason(opt_in_env: str, extra_bins: tuple[str, ...] = ()) -> str:
    """Shared gate for the opt-in real-boot harness tests (freshbox idiom):
    returns the pytest skip reason, or '' to run. Base deps are the real-boot
    contract — claude binary, jq, claudron, host auth; extra_bins adds
    harness-specific binaries. One home, so the dep contract cannot drift
    between harness wrappers."""
    import shutil

    if os.environ.get(opt_in_env) != "1":
        return f"gated — set {opt_in_env}=1 to run the real-boot harness"
    missing = [
        b for b in ("claude", "jq", "claudron", *extra_bins) if shutil.which(b) is None
    ]
    creds = Path.home() / ".claude" / ".credentials.json"
    if not creds.is_file():
        missing.append(f"auth {creds}")
    return f"real-boot harness needs: {', '.join(missing)}" if missing else ""


def write_jsonl(path: Path, rows: list) -> None:
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def dispatch_row(bot, dispatched_at, expected_by, task_id=None, **extra):
    """Ledger-schema fixture for dispatch-log.jsonl rows (task_id optional)."""
    row = {
        "ts": "2026-05-27T10:00:00Z",
        "manager": "lead",
        "bot": bot,
        "task": "do x",
        "dispatched_at": dispatched_at,
        "expected_by": expected_by,
    }
    if task_id is not None:
        row["task_id"] = task_id
    row.update(extra)
    return row


def report_row(bot, ts, status="completed", task_id=None, **extra):
    """Ledger-schema fixture for report-back.jsonl rows (task_id optional)."""
    row = {
        "ts": ts,
        "bot": bot,
        "status": status,
        "summary": "done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }
    if task_id is not None:
        row["task_id"] = task_id
    row.update(extra)
    return row


def load_test_fleet(fleet_dir: Path):
    """load_fleet against a fleet_dir fixture, returning just the FleetConfig."""
    from claudlobby.config import load_fleet

    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def make_paths(fleet_dir: Path):
    """Paths rooted at a fleet_dir fixture (root == fleet_dir)."""
    from claudlobby.paths import Paths

    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


def install_real_template(root: Path) -> None:
    """Overwrite a test root's stub claude.md.j2 with the repo's real template.

    The fleet_dir fixture ships a minimal stub; tests asserting real template
    sections (Projects table, Fleet You Manage, …) install the real one.
    """
    (root / "templates").mkdir(exist_ok=True)
    (root / "templates" / "claude.md.j2").write_text(
        (Path(__file__).parent.parent / "templates" / "claude.md.j2").read_text()
    )


MINIMAL_FLEET_YAML = dedent("""\
    fleet:
      name: test-fleet
      service_prefix: com.test
      telegram_group_chat_id: "-100999"

      accounts:
        default: ~/.claude

      defaults:
        model: opus
        guardrails: [no-push-main]
        protocols: [report-back]

      teams:
        eng:
          manager: lead
          workers: [worker-1]

      bots:
        lead:
          expertise: [orchestration]
          telegram:
            handle: lead_bot
            token_env: TELEGRAM_TOKEN_LEAD
            require_mention: false
        worker-1:
          expertise: [software-engineering]
          telegram:
            handle: worker1_bot
            token_env: TELEGRAM_TOKEN_WORKER1
""")


@pytest.fixture
def fleet_dir(tmp_path: Path) -> Path:
    """Create a minimal fleet layout under tmp_path and return the root."""
    root = tmp_path / "claudlobby"
    root.mkdir()

    # fleet.yaml
    (root / "fleet.yaml").write_text(MINIMAL_FLEET_YAML)

    # Minimal library
    for kind in (
        "expertise",
        "guardrails",
        "protocols",
        "integrations",
        "mcp",
        "skills",
        "resources",
        "lessons",
    ):
        (root / "library" / kind).mkdir(parents=True)

    # Expertise files the fleet references
    (root / "library" / "expertise" / "orchestration.md").write_text(
        "# Orchestrator\n\nManage the team.\n"
    )
    (root / "library" / "expertise" / "software-engineering.md").write_text(
        "# Engineer\n\nBuild things.\n"
    )

    # A guardrail and protocol the defaults reference
    (root / "library" / "guardrails" / "no-push-main.md").write_text(
        "---\ntitle: No push to main\n---\n\nNever push to main.\n"
    )
    # Every bot composes DEFAULT_GUARDRAILS, so the minimal library must carry
    # them or the validator warns per bot about a guardrail it cannot resolve.
    for _default_guardrail in DEFAULT_GUARDRAILS:
        (root / "library" / "guardrails" / f"{_default_guardrail}.md").write_text(
            f"---\ntitle: {_default_guardrail}\n---\n\nDefault guardrail.\n"
        )
    (root / "library" / "protocols" / "report-back.md").write_text(
        "---\ntitle: Report-Back Protocol\n---\n\nReport back when done.\n"
    )

    # MCP fragment
    mcp_frag = {
        "github": {"command": "gh", "args": ["mcp"]},
        "_env_contract": {
            # `secret` is required on every entry since #1214 Phase 1 — a
            # fragment without it is rejected, so the shared fixture carries it
            # or every test using this fleet fails on a well-formed fragment.
            "GITHUB_PAT": {
                "description": "GitHub PAT",
                "tier": "fleet",
                "secret": True,
            },
        },
    }
    (root / "library" / "mcp" / "github.json").write_text(json.dumps(mcp_frag))

    # Integration doc with env_contract
    (root / "library" / "integrations" / "github.md").write_text(
        dedent("""\
        ---
        title: GitHub MCP
        env_contract:
          GITHUB_PAT:
            description: GitHub personal access token
            tier: fleet
            secret: true
        ---

        # GitHub MCP

        Use GitHub MCP for repo operations.
    """)
    )

    # Template
    (root / "templates").mkdir()
    (root / "templates" / "claude.md.j2").write_text(
        "# {{ bot.name }}\n\n{{ expertise_body }}\n"
    )

    # Voices dir
    (root / "voices").mkdir()

    # Runtime dir
    (root / "runtime" / "bots").mkdir(parents=True)

    return root
