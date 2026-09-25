"""Hosted-macOS controls for native bridge evidence, using an owned git export.

Each mutant must fail the intended assertion in every selected case. A skip,
setup error, unrelated failure, or unexpectedly passing mutant fails this driver.
The checkout stays unchanged and each test retains its final process-group reap.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


SOURCE = Path(__file__).resolve().parents[2]
BRIDGE = "tests/test_bridge_state.py"
STARTUP = "test_native_bridge_startup_failure_reaps_tree"
LONG_PATH = "test_native_host_long_exec_path_reads_up"
CASES = [f"{STARTUP}[{case}]" for case in ("no-pidfile", "early-exit", "pre-yield")] + [LONG_PATH]


def _run(export: Path, output: Path, env: dict[str, str], label: str,
         cases: list[str], failure: str | None = None) -> dict:
    junit = output / f"{label}.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-ra", f"--junitxml={junit}",
         *[f"{BRIDGE}::{name}" for name in cases]],
        cwd=export, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=180,
    )
    (output / f"{label}.log").write_text(result.stdout)
    print(result.stdout, end="", flush=True)
    assert result.returncode == (1 if failure else 0), f"{label}: unexpected pytest exit {result.returncode}"
    actual = list(ET.parse(junit).iter("testcase"))
    assert len(actual) == len(cases), f"{label}: unexpected case count"
    for name in cases:
        found = [case for case in actual if case.attrib["name"] == name]
        assert len(found) == 1, f"{label}: missing or duplicate {name}"
        case = found[0]
        assert case.find("skipped") is None and case.find("error") is None, f"{label}: did not execute {name}"
        failures = case.findall("failure")
        if failure:
            assert len(failures) == 1 and failure in failures[0].attrib.get("message", ""), (
                f"{label}: {name} did not fail the intended assertion: {ET.tostring(case, encoding='unicode')}"
            )
        else:
            assert not failures, f"{label}: baseline failed: {name}"
    return {"pytest_exit": result.returncode, "cases": cases, "expected_failure": failure}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (platform.system() != "Darwin" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("CLAUDLOBBY_CI_NATIVE_SMOKE") != "1"):
        parser.error("controls require an opted-in GitHub-hosted macOS runner")
    node = shutil.which("node")
    assert node, "native controls require Node; absence is not a skip"
    subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=SOURCE, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = {"revision": revision, "controls": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="native-controls-", dir=output) as temporary:
            scratch = Path(temporary)
            export = scratch / "source"
            export.mkdir()
            archive = subprocess.run(["git", "archive", "--format=tar", revision], cwd=SOURCE,
                                     capture_output=True, check=True)
            subprocess.run(["/usr/bin/tar", "-xf", "-", "-C", str(export)], input=archive.stdout, check=True)
            for directory in ("home", "tmp"):
                (scratch / directory).mkdir()
            env = {
                "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
                "PATH": os.pathsep.join([str(Path(sys.executable).parent), str(Path(node).parent),
                                         "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
                "PYTHONPATH": str(export), "LANG": "en_US.UTF-8",
                "PLANE_EMIT_DISABLED": "1", "PLANE_EMIT_ENABLED": "0",
                "PLANE_SOCKET": str(scratch / "unbound.sock"),
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            }
            results["controls"]["baseline"] = _run(export, output, env, "baseline", CASES)
            mutations = [
                ("startup-cleanup", BRIDGE,
                 "    except BaseException:\n        _kill_tree(proc)\n        raise\n",
                 "    except BaseException:\n        raise\n",
                 [f"{STARTUP}[no-pidfile]", f"{STARTUP}[pre-yield]"],
                 "startup failure left the parent running"),
                ("wrong-comm", "lib/lib-common.sh", '    _exe="${args%% *}"\n', '    _exe="$comm"\n',
                 [LONG_PATH], "got 'no_bridge' for a healthy owned poller"),
            ]
            for label, relative, original, mutant, cases, failure in mutations:
                path = export / relative
                source = path.read_text()
                assert source.count(original) == 1, f"{label}: mutation seam changed"
                try:
                    path.write_text(source.replace(original, mutant))
                    results["controls"][label] = _run(export, output, env, label, cases, failure)
                finally:
                    path.write_text(source)
            results["controls"]["restored"] = _run(export, output, env, "restored", CASES)
    finally:
        (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
