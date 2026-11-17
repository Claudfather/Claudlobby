"""The shared shell recursion parser validates shape, never executes authority."""
from pathlib import Path
import subprocess

import pytest

HELPER = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/cli-context.sh"


@pytest.fixture
def runtime(tmp_path):
    root, native = tmp_path / "data", tmp_path / "native"
    root.mkdir()
    native.mkdir()
    cli = tmp_path / "claudlobby"
    cli.write_text("#!/bin/bash\nexit 99\n")  # Must never execute in parser tests.
    cli.chmod(0o755)
    fleet_root = root / "local/example"
    return {"HOME": str(tmp_path), "CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_CLI": str(cli),
            "CLAUDLOBBY_NATIVE_DIR": str(native), "CLAUDLOBBY_INSTALLATION": str(tmp_path / "installation"),
            "CLAUDLOBBY_NATIVE_ADMITTED": "1", "CLAUDLOBBY_FLEET": "example", "FLEET_ROOT": str(fleet_root)}


def parse(runtime, mode, *args):
    script = '. "$1"; shift; _claudlobby_parse_runtime_scope "$@" || exit $?; '
    script += 'printf "%s\\n" "$_CL_RUNTIME_FLEET" "$_CL_RUNTIME_FLEET_ROOT" "$_CL_RUNTIME_BOTS_DIR" "$_CL_RUNTIME_DRY_RUN"; '
    script += 'printf "bot:%s\\n" ${_CL_RUNTIME_BOTS[@]+"${_CL_RUNTIME_BOTS[@]}"}; '
    script += 'printf "plugin:%s\\n" ${_CL_RUNTIME_PLUGINS[@]+"${_CL_RUNTIME_PLUGINS[@]}"}; '
    script += 'printf "declares:%s\\n" ${_CL_RUNTIME_DECLARES[@]+"${_CL_RUNTIME_DECLARES[@]}"}'
    return subprocess.run(["/bin/bash", "-u", "-c", script, "parser", str(HELPER), mode,
                           runtime["CLAUDLOBBY_NATIVE_DIR"], *args], env=runtime,
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("mode", ["reload", "credentials", "runtime", "siblings"])
def test_git_boolean_marker_is_not_a_release_argument(runtime, mode):
    args = ["--admitted-runtime"]
    if mode in {"reload", "credentials"}:
        args += ["--fleet", "example", "--bots-dir", runtime["FLEET_ROOT"] + "/runtime/bots", "--bot", "worker"]
    if mode == "credentials":
        args += ["--fleet-root", runtime["FLEET_ROOT"]]
    if mode == "reload":
        args += ["--plugin", "literal plugin"]
    if mode == "siblings":
        args += ["--dry-run"]
    result = parse(runtime, mode, *args)
    assert result.returncode == 0, result.stderr
    if mode == "reload":
        assert "bot:worker" in result.stdout.splitlines()
        assert "plugin:literal plugin" in result.stdout.splitlines()
    if mode == "siblings":
        assert result.stdout.splitlines()[3] == "1"


@pytest.mark.parametrize("change,args", [
    ({"CLAUDLOBBY_NATIVE_ADMITTED": ""}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_NATIVE_ADMITTED": "0"}, ["--admitted-runtime"]),
    ({}, ["--admitted-runtime", "some-value"]),
    ({}, ["--admitted-runtime", "--admitted-runtime"]),
    ({}, ["--selected-release", "old-release"]),
    ({"CLAUDLOBBY_ROOT": "relative"}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_CLI": "/missing/cli"}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_NATIVE_DIR": "relative"}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_RELEASE_ID": "old-release"}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_RELEASE_ID": ""}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_ARTIFACT_ID": "old-artifact"}, ["--admitted-runtime"]),
    ({"CLAUDLOBBY_INSTALLATION": ""}, ["--admitted-runtime"]),
])
def test_invalid_runtime_shape_refuses_before_any_native_body(runtime, change, args):
    result = parse({**runtime, **change}, "runtime", *args)
    assert result.returncode == 2 and result.stdout == ""


def test_legacy_sealed_marker_is_refused_even_when_exact(runtime):
    runtime.pop("CLAUDLOBBY_INSTALLATION")
    runtime.pop("CLAUDLOBBY_NATIVE_ADMITTED")
    runtime["CLAUDLOBBY_RELEASE_ID"] = "sealed-fixture"
    assert parse(runtime, "runtime", "--selected-release", "sealed-fixture").returncode == 2
    assert parse(runtime, "runtime", "--selected-release", "different").returncode == 2


@pytest.mark.parametrize("marker,expected", [("1", 0), ("0", 3), ("", 3)])
def test_git_pulse_requires_its_own_recursion_marker(runtime, marker, expected):
    runtime["CLAUDLOBBY_PRIVATE_PULSE_RUNTIME"] = marker
    result = subprocess.run(["/bin/bash", "-u", "-c", '. "$1"; _claudlobby_require_pulse_runtime "$2" example',
                             "pulse", str(HELPER), runtime["CLAUDLOBBY_NATIVE_DIR"]], env=runtime,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == expected


@pytest.mark.parametrize("tail", [
    ["--fleet", "other", "--bot", "worker"],
    ["--fleet", "example", "--bot", "../worker"],
    ["--fleet", "example"],
    ["--fleet", "example", "--bot", "worker", "--fleet-root", "/different"],
])
def test_credentials_require_exact_fleet_paths_and_nonempty_literal_roster(runtime, tail):
    result = parse(runtime, "credentials", "--admitted-runtime", "--fleet-root", runtime["FLEET_ROOT"],
                   "--bots-dir", runtime["FLEET_ROOT"] + "/runtime/bots", *tail)
    assert result.returncode == 2 and result.stdout == ""


@pytest.mark.parametrize("marked,expected", [(False, 3), (True, 2)])
def test_pulse_refuses_unmarked_entry_before_context_validation(runtime, marked, expected):
    runtime.pop("CLAUDLOBBY_CLI")
    if marked:
        runtime["CLAUDLOBBY_PRIVATE_PULSE_RUNTIME"] = "1"
    result = subprocess.run(["/bin/bash", "-u", "-c", '. "$1"; _claudlobby_require_pulse_runtime "$2" example',
                             "pulse", str(HELPER), runtime["CLAUDLOBBY_NATIVE_DIR"]], env=runtime,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == expected
    if not marked:
        assert "use claudlobby fleet pulse" in result.stderr


CREDENTIALS = ["--admitted-runtime", "--fleet", "example", "--bot", "worker"]


def credentials(runtime, *tail):
    return parse(runtime, "credentials", *CREDENTIALS, "--fleet-root", runtime["FLEET_ROOT"],
                 "--bots-dir", runtime["FLEET_ROOT"] + "/runtime/bots", *tail)


def test_credentials_take_each_declaration_literally_and_in_order(runtime):
    """#1213: the selected CLI passes the fleet's declarations, VAR=SURFACE each."""
    # A surface names a library file, so its name may hold any text but a control character.
    values = ["RAILWAY_PERSONAL_TOKEN=integration/railway", "GITHUB_PAT=mcp/github",
              "_X1=integration/dir/name.v2", "ACME_KEY=mcp/my_server", "ACME_KEY=mcp/a name",
              "ACME_PUSH_TOKEN=git_credentials/Acme-Org", "WORKER_TG_TOKEN=telegram/worker"]
    tail = [part for value in values for part in ("--declares", value)]
    result = credentials(runtime, *tail)
    assert result.returncode == 0, result.stderr
    assert [line for line in result.stdout.splitlines() if line.startswith("declares:")] == [
        f"declares:{value}" for value in values]
    # printf with no arguments prints its format once, so none reads as one empty line.
    none = credentials(runtime)
    assert none.returncode == 0, none.stderr
    assert [line for line in none.stdout.splitlines() if line.startswith("declares:")] == [
        "declares:"]


@pytest.mark.parametrize("value", [
    "GITHUB_PAT", "=mcp/github", "GITHUB_PAT=", "1PAT=mcp/github", "GITHUB-PAT=mcp/github",
    "GITHUB_PAT=../x", "GITHUB_PAT=skill/github", "GITHUB_PAT=mcp/", "GITHUB_PAT=$(id)",
    "GITHUB_PAT=mcp/github\nOTHER=mcp/x", "GITHUB_PAT=mcp/git\thub",
    "GITHUB_PAT=git_credentials/", "GITHUB_PAT=git-credentials/Acme-Org",
])
def test_credentials_refuse_a_declaration_of_any_other_shape(runtime, value):
    result = credentials(runtime, "--declares", value)
    assert result.returncode == 2 and result.stdout == ""


def test_only_the_credentials_scope_takes_declarations(runtime):
    result = parse(runtime, "reload", "--admitted-runtime", "--fleet", "example",
                   "--bots-dir", runtime["FLEET_ROOT"] + "/runtime/bots", "--bot", "worker",
                   "--declares", "GITHUB_PAT=mcp/github")
    assert result.returncode == 2 and "unsupported admitted runtime option" in result.stderr
