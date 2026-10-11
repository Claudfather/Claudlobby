"""Shared fixtures for claudlobby tests."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

import claudlobby

_TEST_TREE = Path(__file__).resolve().parent.parent
REALBOOT_HOST_CREDS = Path.home() / ".claude" / ".credentials.json"
if Path(claudlobby.__file__).resolve().parent != _TEST_TREE / "claudlobby":
    raise pytest.UsageError("test package origin does not match the tree under test")

from claudlobby.config import DEFAULT_GUARDRAILS


def _require_prepared_resources():
    package = _TEST_TREE / "claudlobby"
    metadata = package / "_artifact.json"
    guidance = "prepare resources in this disposable checkout with tests/prepare_resources.py"
    try:
        manifest = json.loads(metadata.read_text())
        sources = manifest["resource_sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError("missing source inventory")
        indexed = subprocess.check_output(
            ["git", "ls-files", "-z", "--", "claudlobby/_runtime_scripts", "library",
             "templates", "voices", "fleet.yaml.seed", "fleet.yaml.example",
             "projects.yaml.seed", ".env.seed.example", "missions/fleet.md.seed"],
            cwd=_TEST_TREE, timeout=10).decode().split("\0")
        indexed = {name for name in indexed if name and name not in {
            "claudlobby/_runtime_scripts/CLAUDE.md",
            "claudlobby/_runtime_scripts/personal/finance-presync.sh"}}
        if indexed != set(sources):
            raise ValueError("prepared resource inventory differs from the source index")
        for name in sources:
            source = _TEST_TREE / name
            # Runtime scripts are authored at their installed package path.
            target = (source if name.startswith("claudlobby/_runtime_scripts/")
                      else package / "_resources" / "seeds" / name if name in {
                          "fleet.yaml.seed", "fleet.yaml.example", "projects.yaml.seed",
                          ".env.seed.example", "missions/fleet.md.seed"}
                      else package / "_resources" / name)
            if (not source.is_file() or not target.is_file()
                    or hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest()
                    or source.stat().st_mode & 0o111 != target.stat().st_mode & 0o111):
                raise ValueError(f"stale prepared resource: {name}")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise pytest.UsageError(f"{guidance}: {exc}") from exc


_require_prepared_resources()


_HOME_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR")


def _short_test_directory(prefix):
    candidates = (os.environ.get("CLAUDLOBBY_TEST_TMPDIR"), "/tmp", os.environ.get("TMPDIR"))
    for candidate in dict.fromkeys(candidates):
        if not candidate:
            continue
        try:
            return Path(tempfile.mkdtemp(prefix=prefix, dir=candidate)).resolve()
        except OSError:
            continue
    raise pytest.UsageError("no writable short test temp root; set CLAUDLOBBY_TEST_TMPDIR")


def _isolate_home(patch, base):
    for key, name in zip(_HOME_KEYS, ("home", "config", "cache", "state", "data", "tmp")):
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        patch.setenv(key, str(directory))
    patch.setattr(tempfile, "tempdir", str(base / "tmp"))


def _silence_plane(patch):
    patch.setenv("PLANE_EMIT_DISABLED", "1")
    for key in ("CLAUDLOBBY_ROOT", "CLAUDLOBBY_CLI", "PLANE_SOCKET", "PLANE_EMIT_CLI"):
        patch.delenv(key, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _isolate_plane_session(tmp_path_factory, request):
    """Guard session fixtures too; undo only our changes when pytest exits.

    Collection-time subprocesses must use constructed_env themselves: no
    fixture can protect code that ran before fixture setup.
    """
    # Keep Unix socket paths short and environment state outside tests' data
    # directories. A nested pytest tmp_path can exceed sun_path before tmux
    # even opens its socket, and adding children changes directory-scan tests.
    base = _short_test_directory("ct-")
    with pytest.MonkeyPatch.context() as patch:
        _isolate_home(patch, base)
        _silence_plane(patch)
        # Installed-wheel subprocesses must import their own package, not a
        # source tree inherited from the parent validation environment (#1316).
        patch.delenv("PYTHONPATH", raising=False)
        # Pytest chooses this lazily. Initialize it while TMPDIR belongs to
        # the session, before a function fixture selects a shorter-lived dir.
        tmp_path_factory.getbasetemp()
        yield base
    if request.session.testsfailed:
        print(f"retained failed-test files: {base}", file=sys.stderr)
    else:
        shutil.rmtree(base)


@pytest.fixture(autouse=True)
def _isolate_claudlobby_root(monkeypatch, _isolate_plane_session):
    """Reset the default for each test; explicit local overrides still win."""
    # The session owns cleanup after every test monkeypatch has been undone.
    # Deleting here runs before this dependent monkeypatch fixture tears down;
    # source-state tests deliberately replace os.scandir and break rmtree then.
    base = Path(tempfile.mkdtemp(prefix="t-", dir=_isolate_plane_session)).resolve()
    _isolate_home(monkeypatch, base)
    _silence_plane(monkeypatch)
    from claudlobby import paths
    original = paths._is_host_data_root

    def refuse_source_checkout(path):
        resolved = Path(path).resolve()
        if resolved == _TEST_TREE or resolved.is_relative_to(_TEST_TREE):
            raise AssertionError("cwd discovery reached the source checkout; pass a private --root")
        return original(path)

    monkeypatch.setattr(paths, "_is_host_data_root", refuse_source_checkout)
    yield base


@pytest.fixture(autouse=True)
def _isolate_host_override(monkeypatch):
    """A host that runs the suite may carry a real host override
    (~/.config/claudlobby/system.yaml, #1251). Point the loader at a file that
    does not exist, or every host-job assertion reads that host's pauses."""
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", "/nonexistent/claudlobby-host-override.yaml")


from tests.quarantine_policy import pytest_collection_modifyitems  # noqa: F401


# Captures chat id, the (expanded) state dir the caller resolved, and the
# message — the observation point for the emit_* fleet-signal paths.
TG_STUB = (
    "#!/bin/bash\n"
    'printf "%s|%s|%s\\n" "$TELEGRAM_GROUP_CHAT_ID" "$TELEGRAM_STATE_DIR" "$1" >> "$TG_CAPTURE"\n'
)


def read_fleet_events(root, *, allow_absent=False):
    """Every fleet event on the plane under <root>, rendered as the legacy
    JSONL rows (compact, one per line, oldest first). An absent or staged-only
    plane is a test setup error unless the caller explicitly allows it.
    F18 closure R1: the state/events/ file this once
    concatenated is gone; the rows a door lands (bot-, fleet- or
    host-anchored) come back in the exact row shape the file had, so an
    assertion like `'"type":"disk_high"' in read_fleet_events(root)` keeps
    its meaning. Negative reads require a served recording channel: staged
    batches have not become queryable facts and must not count as silence."""
    plane = Path(root) / "state" / "plane"
    staged = tuple((plane / "staged").glob("*.batch"))
    if staged:
        raise AssertionError(f"{len(staged)} plane batch(es) staged but not committed")
    db = plane / "plane.db"
    if not db.exists():
        if allow_absent:
            return ""
        raise AssertionError("plane database is absent; enable fixture recording before reading events")
    from claudlobby.plane.db import connect_ro
    pr = load_lib_module("plane-readers")
    conn = connect_ro(db)
    try:
        rows = conn.execute(
            "SELECT e.occurred_at, e.event, e.severity, e.subject_kind, e.subject_alias,"
            " e.detail, e.detail_truncated, f.alias FROM events e"
            " LEFT JOIN identity_registry f ON f.uid = e.fleet_uid"
            " WHERE e.source_ref LIKE 'fleet-events:%' ORDER BY e.ingest_seq").fetchall()
    finally:
        conn.close()
    return "".join(
        json.dumps(pr.public(pr.legacy_event_row(*row)), separators=(",", ":")) + "\n"
        for row in rows)


class ScratchPlaneEnv:
    """Couple a recording opt-in to storage and transports owned by pytest."""

    def __init__(self, base: Path, cli: Path):
        self.base = base.resolve()
        self.cli = cli.resolve()
        self._socket_dirs = []

    def _owned(self, path: Path, label: str, *, sockets=False) -> Path:
        resolved = Path(path).resolve()
        owners = [self.base]
        if sockets:
            owners.extend(d.resolve() for d in self._socket_dirs)
        if not any(resolved != owner and resolved.is_relative_to(owner) for owner in owners):
            raise ValueError(f"{label} must be inside a fixture-owned directory: {path}")
        return resolved

    def socket_dir(self) -> Path:
        """Allocate and register one short directory for a real Unix daemon.

        macOS sun_path cannot hold pytest's long basetemp paths. This owns
        exactly the mkdtemp directory, never all of /tmp.
        """
        directory = _short_test_directory("pe-")
        self._socket_dirs.append(directory)
        return directory

    def close(self):
        for directory in self._socket_dirs:
            shutil.rmtree(directory)

    def __call__(self, root: Path, *, socket: Path | None = None,
                 cli: Path | None = None, initialize: bool = False) -> dict[str, str]:
        root = self._owned(root, "Plane root")
        source = Path(__file__).resolve().parent.parent
        if root == source or root.is_relative_to(source):
            raise ValueError("Plane root must not be the source checkout")
        default_socket = socket is None
        socket = self._owned(socket if socket is not None else root / "no-daemon.sock",
                             "Plane socket", sockets=True)
        if default_socket and socket.exists():
            raise ValueError(f"default Plane socket must be absent: {socket}")
        if cli is None:
            cli = self.cli
        elif Path(cli).resolve() != self.cli:
            cli = self._owned(cli, "Plane CLI")
        if initialize:
            from tests.plane_setup import initialize_plane
            initialize_plane(root)
        return {"CLAUDLOBBY_ROOT": str(root), "PLANE_EMIT_DISABLED": "0",
                "PLANE_SOCKET": str(socket), "PLANE_EMIT_CLI": str(cli)}


@pytest.fixture(scope="session")
def test_cli(_isolate_plane_session, tmp_path_factory):
    """Refuse a globally installed CLI or an editable install of another tree."""
    cli = Path(sys.executable).parent / "claudlobby"
    assert sys.prefix != sys.base_prefix, "CLI tests require a dedicated venv"
    assert cli.is_file(), f"install this checkout in the test venv: {cli}"
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import claudlobby; print(claudlobby.__file__)"],
        cwd=tmp_path_factory.mktemp("plane-cli-preflight"),
        env=constructed_env(), text=True, capture_output=True, check=True,
    )
    assert Path(result.stdout.strip()).resolve().parent == repo / "claudlobby"
    # The console script must use THIS interpreter, not an ambient installation.
    assert str(Path(sys.executable)) in cli.read_text().splitlines()[0]
    return cli


@pytest.fixture
def selected_test_cli(test_cli, monkeypatch, _isolate_claudlobby_root):
    """Explicit CLI selection for non-recording source harnesses (#1316)."""
    monkeypatch.setenv("CLAUDLOBBY_CLI", str(test_cli))
    return test_cli


@pytest.fixture(scope="session")
def built_test_cli(tmp_path_factory):
    """A private wheel CLI for source instruments that need built resources."""
    from tests.prepare_resources import _copy_indexed_source
    from tests.test_package_resources import _copy_installed_dependencies

    owned = tmp_path_factory.mktemp("built-cli")
    source = owned / "source"
    source.mkdir()
    env = constructed_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                          GIT_TERMINAL_PROMPT="0")
    # Use the same history-free source snapshot as prepare_resources. Building
    # the checkout directly adds its HEAD to the artifact ID in CI, so its wheel
    # correctly fails the harness's prepared-artifact identity check.
    _copy_indexed_source(_TEST_TREE, source, env)
    dist = owned / "dist"
    subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
                    "--outdir", str(dist), str(source)], check=True,
                   capture_output=True, text=True, env=env)
    wheel, = dist.glob("*.whl")
    venv = owned / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    # The source test process may export PYTHONPATH; pip must not mistake that
    # checkout for a wheel already installed in this otherwise empty venv.
    subprocess.run([python, "-I", "-m", "pip", "install", "--no-index", "--no-deps",
                    "--no-compile", str(wheel)], check=True,
                   capture_output=True, text=True)
    installed = Path(subprocess.check_output(
        [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True).strip())
    _copy_installed_dependencies(wheel, installed)
    return venv / "bin/claudlobby"


@pytest.fixture
def scratch_plane_env(tmp_path_factory, test_cli):
    """Explicit opt-in for intentional recording; use with constructed_env."""
    builder = ScratchPlaneEnv(tmp_path_factory.getbasetemp(), test_cli)
    yield builder
    builder.close()


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
        if not k.startswith(("TELEGRAM", "CLAUDLOBBY", "FLEET", "BOT_", "PLANE_"))
    }
    env["PLANE_EMIT_DISABLED"] = "1"
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def equip_grammar(root: Path) -> Path:
    """Put the install's REAL `mcp-package-grammar.py` under ``root/lib/``.

    Opt-in, per test module, rather than a side effect of `fleet_dir`: the
    grammar is needed by the four modules that drive composition or
    warm-cache, and planting a one-file `claudlobby/_runtime_scripts/` in all 71 fixtures to serve 4
    is what made `claudlobby/_runtime_scripts/` EXIST without being WIRED. Thirteen helpers across
    the suite key on `(root / "lib").exists()` to decide whether to link the
    real tree; a partial `claudlobby/_runtime_scripts/` makes that check answer yes and skip, and the
    test then runs against doors it cannot read (#1633's ignition tests, where
    `task-recheck` fell back to ARMED and every scenario passed vacuously).

    Copied real, never stubbed: `mcp_grammar` REFUSES when it cannot load the
    grammar — a fallback would be the second copy it exists to prevent — so a
    module that needs this and omits it FAILS LOUDLY here rather than quietly
    testing a broken install.
    """
    lib = root / "lib"
    lib.mkdir(exist_ok=True)
    repo = Path(__file__).resolve().parent.parent
    dest = lib / "mcp-package-grammar.py"
    shutil.copy(repo / "claudlobby/_runtime_scripts" / "mcp-package-grammar.py", dest)
    return dest


def constructed_env(**overrides):
    """Minimal CONSTRUCTED child env (#846): built from nothing, never from an
    os.environ copy. Inheriting ambient env makes isolation a denylist — every
    production-pointing variable (FLEET_STATE_PATH, escalation chat ids,
    BOT_DIR) must be remembered and subtracted, and the one nobody thought of
    is the one that leaks. Built minimal, a new isolation-sensitive variable
    is absent by construction. PATH deliberately inherits host tools; pass
    PATH=... to prepend stub dirs. HOME/XDG/TMPDIR carry the
    shared fixtures' private directories; explicit overrides still win.
    LANG pins UTF-8 semantics: with
    no locale at all, grep/awk match the UTF-8 pane-fixture glyphs bytewise and
    lib-common's pane classifiers flip one verdict (test_keepalive_classify /
    test_pane_is_idle, measured). Values are str()-coerced so Paths pass
    through."""
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
           "PLANE_EMIT_DISABLED": "1"}
    env.update({key: os.environ[key] for key in _HOME_KEYS if key in os.environ})
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def git_isolation_env(cfg, **extra):
    """Subprocess env for driving real git against a COMPOSED gitconfig only.

    One owner of the isolation contract (GIT_CONFIG_GLOBAL + system config off
    + no terminal prompts) — it exists in several batteries and a drifted copy
    weakens an assertion silently (dropping GIT_TERMINAL_PROMPT would hang a
    harness rather than fail a check)."""
    return {
        **os.environ,
        **extra,
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _write_exec(path, content):
    """Write a stub script and set its exec bits (shared shell-test harness helper)."""
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def booby_trap_git(bindir):
    """Plant a `git` that records any invocation; returns the sentinel path.

    The App-auth D10 helper-direct pin: token acquisition must never consult
    git (a pathless `git credential fill` silently serves whatever identity
    ambient config answers). Callers put `bindir` first on PATH with
    STUB_DIR=bindir in the env, run the surface under test, and assert the
    returned sentinel does not exist. Shared because trap and assert are
    stringly coupled through the sentinel name — one owner keeps every
    battery's pin identical.
    """
    _write_exec(
        Path(bindir) / "git",
        '#!/bin/bash\ntouch "$STUB_DIR/git-was-called"\nexit 1\n',
    )
    return Path(bindir) / "git-was-called"


def fake_tmux_input_box(path, state, *, echoes=True):
    """Plant a fake `tmux` whose pane is an idle Claude Code input box.

    The send presses Enter only once the box SHOWS the payload (#1236), so a
    test that drives a real send through a fake TMUX_BIN needs a box that does:
    each typed `send-keys` chunk is appended and drawn after a "> " prompt, and
    Enter submits it, leaving the box empty. echoes=False is a TUI that never
    draws what is typed, so the send must withhold its Enter. A leading
    "-L <socket>" is skipped, has-session succeeds, anything else exits 0.
    Shared so every such test models one box, not its own variant of it.
    """
    typed = 'printf %s "${@: -1}" >> "$S"' if echoes else ":"
    _write_exec(Path(path), (
        "#!/bin/bash\n"
        f"S={shlex.quote(str(state))}\n"
        '[ "$1" = "-L" ] && shift 2\n'
        'case "$1" in\n'
        "    has-session) exit 0 ;;\n"
        "    capture-pane) printf '> %s\\n' \"$(cat \"$S\" 2>/dev/null)\" ;;\n"
        '    send-keys) if [ "${@: -1}" = Enter ]; then : > "$S"; else ' + typed + "; fi ;;\n"
        "esac\n"
        "exit 0\n"))
    return Path(path)

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


@pytest.fixture(scope="session")
def native_stand_ins(tmp_path_factory):
    """`bun` and `claude` stand-ins for node, built once per session and shared by
    every test that needs a native process by those names
    (tests/stand_in_fixtures.py). A test keeps its own scripts and state under
    its own tmp_path."""
    from tests.stand_in_fixtures import NODE, make_stand_ins

    if NODE is None:
        pytest.skip("native stand-ins need node (a claudlobby prerequisite)")
    return make_stand_ins(tmp_path_factory.mktemp("native-bins"), NODE)


def call_lib_fn(fn: str, value: str) -> str:
    """Source lib-common.sh and call one function on a single value, returning
    stdout. The value travels as a positional arg so the shell never
    interprets it."""
    lib = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts" / "lib-common.sh"
    r = subprocess.run(
        ["bash", "-c", f'. "{lib}"; {fn} "$1"', "_", value],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=10,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def load_lib_module(name: str, *, directory: str = "claudlobby/_runtime_scripts"):
    """Import an un-packaged runtime or harness script as a module."""
    import importlib.util

    if directory not in {"claudlobby/_runtime_scripts", "harness"}:
        raise ValueError("unknown source script directory")
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).parent.parent / directory / f"{name}.py"
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
    harness-specific binaries. The credential path is captured at import,
    before HOME is isolated, and is the path handed to both harnesses."""
    import shutil

    if os.environ.get(opt_in_env) != "1":
        return f"gated — set {opt_in_env}=1 to run the real-boot harness"
    missing = [
        b for b in ("claude", "jq", "claudron", *extra_bins) if shutil.which(b) is None
    ]
    if not REALBOOT_HOST_CREDS.is_file():
        missing.append(f"auth {REALBOOT_HOST_CREDS}")
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

    from tests.package_fixtures import source_package

    return Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package())


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
      manager: lead
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
                "default_tier": "fleet",
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
            default_tier: fleet
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


def equip_bot_with_mcp(fleet_dir: Path, fragments: dict[str, dict]) -> Path:
    """Write each MCP fragment into the fleet library and equip the lead bot.

    The replace below is coupled to MINIMAL_FLEET_YAML's indentation, which is
    why it asserts: a silent no-op leaves the fleet with no MCP servers at all,
    so the command under test returns early and every assertion downstream reads
    the empty path instead of the code. Kept here in ONE copy so a change to the
    template breaks one place, and the failure text says which.
    """
    for name, server in fragments.items():
        (fleet_dir / "library" / "mcp" / f"{name}.json").write_text(
            json.dumps({name: server})
        )
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    after = before.replace(
        "    lead:\n      expertise: [orchestration]\n",
        f"    lead:\n      expertise: [orchestration]\n      mcp: [{', '.join(fragments)}]\n",
    )
    assert after != before, (
        "MINIMAL_FLEET_YAML indentation changed — equip_bot_with_mcp no longer equips the bot"
    )
    fy.write_text(after)
    return fleet_dir


def warm_cache_args(root: Path, dry_run: bool = False) -> argparse.Namespace:
    """The argparse namespace cmd_warm_cache reads."""
    return argparse.Namespace(root=str(root), fleet=None, seed=False, dry_run=dry_run)


class SubprocessRecorder:
    """Stand in for subprocess.run and keep every argv it was handed.

    `missing` names a binary to raise FileNotFoundError for, so a test can model
    an absent toolchain without touching PATH.
    """

    def __init__(
        self, returncode: int = 0, stderr: str = "", missing: str | None = None
    ):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.missing = missing

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        if self.missing is not None and argv[0] == self.missing:
            raise FileNotFoundError(2, "No such file or directory", self.missing)
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout="", stderr=self.stderr
        )

    def argv_for(self, runtime: str) -> list[list[str]]:
        """Every recorded argv whose binary is `runtime`."""
        return [c for c in self.calls if c and c[0] == runtime]
