"""#2023 — three gaps in the heavy-slot classifier found by the cross-fleet canary:
a whole-run `pytest --collect-only` took the slot though it runs nothing; pip and uv
installs were not matched; and the guard did not see through `flock`/`xargs` wrappers
(the habit the manual serialize rule taught). Each case here is red on the prior head.

The classifier's false-positive discipline is unchanged: a run that names its test files
stays ungated, `--dry-run`/`--help`/`--version` are not heavy, and a heavy word that is
not the command (an argument, a path) is left alone.

Round 2 (ravi's review of 5db1d42) adds cases that are red there: `xargs -i`,
`--replace` and `-l` take only an ATTACHED optional argument in GNU xargs, so the
word after them is the command; `uv pip sync` and `uv add` install like `uv pip
install` and `uv sync`, except that `uv add --frozen`/`--no-sync`/`--script` sync
nothing (uv 0.11.3's help), unlike `uv sync --frozen`, which installs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hs():
    spec = importlib.util.spec_from_file_location("heavy_slot", REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# (1) collect-only / version / help run nothing → not heavy
COLLECT_NOT_HEAVY = [
    "pytest --collect-only",
    "pytest --collect-only -q",
    "pytest --co",
    "python3 -m pytest --collect-only -q",
    "./.venv/bin/pytest --collect-only",
]


@pytest.mark.parametrize("command", COLLECT_NOT_HEAVY)
def test_collect_only_is_not_heavy(hs, command):
    assert hs.gate(command, "W") is None, command


# (2) pip and uv installs — heavy when they fetch or build
PIP_UV_GATED = [
    ("pip install pytest", "W pip install pytest"),
    ("pip install -e '.[dev]'", "W pip install -e '.[dev]'"),
    ("pip3 install -r requirements.txt", "W pip3 install -r requirements.txt"),
    ("python3 -m pip install -e '.[dev]'", "W python3 -m pip install -e '.[dev]'"),
    ("./.venv/bin/python -m pip install -e .", "W ./.venv/bin/python -m pip install -e ."),
    ("uv pip install -e .", "W uv pip install -e ."),
    ("uv sync", "W uv sync"),
    ("uv sync --frozen", "W uv sync --frozen"),
    ("pip -q install build", "W pip -q install build"),
    ("uv pip sync requirements.txt", "W uv pip sync requirements.txt"),
    ("uv add requests", "W uv add requests"),
    ("uv add --dev pytest", "W uv add --dev pytest"),
    ("uv add --locked requests", "W uv add --locked requests"),
]


@pytest.mark.parametrize("command,want", PIP_UV_GATED, ids=[c for c, _ in PIP_UV_GATED])
def test_pip_and_uv_installs_are_heavy(hs, command, want):
    assert hs.gate(command, "W") == want


PIP_UV_NOT_HEAVY = [
    "pip install --dry-run pytest",
    "pip install --help",
    "pip --version",
    "pip list",
    "pip show pytest",
    "pip uninstall -y pytest",
    "python3 -m pip --version",
    "python3 -m pip install --dry-run x",
    "uv --version",
    "uv sync --dry-run",
    "uv pip install --dry-run x",
    "uv pip list",
    "uv lock",
    "uv pip sync --dry-run requirements.txt",
    "uv pip sync --help",
    # uv add syncs the environment unless told not to; --frozen here means no sync
    "uv add --no-sync requests",
    "uv add --frozen requests",
    "uv add --script tool.py requests",
    "uv add --script=tool.py requests",
    "uv add --help",
]


@pytest.mark.parametrize("command", PIP_UV_NOT_HEAVY)
def test_pip_and_uv_non_installs_are_not_heavy(hs, command):
    assert hs.gate(command, "W") is None, command


# (3) flock / xargs wrappers — the guard must see the heavy command through them
WRAPPER_GATED = [
    ("flock /tmp/x.lock pytest", "W flock /tmp/x.lock pytest"),
    ("flock -w 60 /tmp/x.lock ./.venv/bin/pytest -q", "W flock -w 60 /tmp/x.lock ./.venv/bin/pytest -q"),
    ("flock /tmp/x.lock npm ci", "W flock /tmp/x.lock npm ci"),
    ("flock -n /tmp/x.lock uv sync", "W flock -n /tmp/x.lock uv sync"),
    # the -c form runs the string through sh -c: wrap the heavy command INSIDE it
    ("flock /tmp/x.lock -c 'pytest -q'", "flock /tmp/x.lock -c 'W pytest -q'"),
    ("flock /tmp/x.lock -c \"npm ci\"", "flock /tmp/x.lock -c \"W npm ci\""),
    ("xargs pytest", "W xargs pytest"),
    ("xargs -0 npm ci", "W xargs -0 npm ci"),
    # GNU xargs: -i/--replace/-l take an ATTACHED optional argument, never the next word
    ("xargs -i pytest {}", "W xargs -i pytest {}"),
    ("xargs --replace pytest {}", "W xargs --replace pytest {}"),
    ("xargs -l pytest", "W xargs -l pytest"),
    ("xargs -I {} pytest {}", "W xargs -I {} pytest {}"),
    # sudo -u <user> is already a runner; confirm it still sees the suite
    ("sudo -u ci pytest -q", "W sudo -u ci pytest -q"),
]


@pytest.mark.parametrize("command,want", WRAPPER_GATED, ids=[c for c, _ in WRAPPER_GATED])
def test_wrappers_do_not_hide_a_heavy_command(hs, command, want):
    assert hs.gate(command, "W") == want


WRAPPER_NOT_HEAVY = [
    "flock /tmp/x.lock ls -la",
    "flock -n /tmp/x.lock echo done",
    "flock /tmp/x.lock -c 'echo done'",
    "xargs rm -f",
    "xargs -n1 echo",
    "sudo -u ci apt-get install -y jq",
]


@pytest.mark.parametrize("command", WRAPPER_NOT_HEAVY)
def test_wrappers_around_light_commands_are_left_alone(hs, command):
    assert hs.gate(command, "W") is None, command


# the wrapper's own re-check (heavy_family) must agree, so the slot it takes is never refused
FAMILY_ACCEPT = [
    ["flock", "/tmp/x.lock", "pytest"],
    ["flock", "-w", "60", "/tmp/x.lock", "npm", "ci"],
    ["xargs", "pytest"],
    ["xargs", "-i", "pytest", "{}"],
    ["xargs", "--replace", "pytest", "{}"],
    ["xargs", "-l", "pytest"],
    ["pip", "install", "pytest"],
    ["pip3", "install", "-e", "."],
    ["python3", "-m", "pip", "install", "-e", "."],
    ["uv", "pip", "install", "-e", "."],
    ["uv", "sync"],
    ["uv", "pip", "sync", "requirements.txt"],
    ["uv", "add", "requests"],
    # the wrapper checks the TOOL: a quiet flag it only sees after expansion
    # (`uv add $FLAGS x`) must never refuse a job the hook sent
    ["uv", "add", "--no-sync", "requests"],
    ["sudo", "-u", "ci", "pytest"],
]


@pytest.mark.parametrize("argv", FAMILY_ACCEPT, ids=[" ".join(a) for a in FAMILY_ACCEPT])
def test_heavy_family_accepts_the_new_tools(hs, argv):
    assert hs.heavy_family(argv)


FAMILY_REFUSE = [
    ["flock", "/tmp/x.lock", "ls"],
    ["xargs", "rm"],
    ["pip", "list"],
    ["uv", "--version"],
    ["uv", "lock"],
]


@pytest.mark.parametrize("argv", FAMILY_REFUSE, ids=[" ".join(a) for a in FAMILY_REFUSE])
def test_heavy_family_refuses_light_forms(hs, argv):
    assert not hs.heavy_family(argv)
