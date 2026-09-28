"""Explicit CI rehearsal of the real offline release assembler, not a fixture.

Build/download inputs first; this script installs only from those wheels into a
new disposable root. It never selects a release, enrolls units or opens a plane.
The ordinary assembly unit tests simulate pip; this covers that missing boundary.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--disposable-root", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    root = args.disposable_root.resolve()
    if root.exists() or root.is_relative_to(source) or source.is_relative_to(root):
        raise SystemExit("rehearsal needs a new disposable root outside the checkout")
    root.mkdir(parents=True, mode=0o700)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
           "PLANE_EMIT_DISABLED": "1", "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "XDG_DATA_HOME"):
        directory = root / key.lower()
        directory.mkdir()
        env[key] = str(directory)
    os.environ.clear()
    os.environ.update(env)
    results = {"steps": []}

    def run(name, command):
        start = time.monotonic()
        with (root / f"{name}.log").open("w") as log, \
                (root / f"{name}.stderr.log").open("w") as errors:
            process = subprocess.Popen([str(arg) for arg in command], cwd=root,
                                       env=env, stdout=log, stderr=errors,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError(f"{name} timed out; see private rehearsal log")
        results["steps"].append({"name": name, "exit": code,
                                 "seconds": time.monotonic() - start})
        if code:
            raise RuntimeError(f"{name} failed ({code}); see private rehearsal log")
        return (root / f"{name}.log").read_text()

    try:
        wheel, wheelhouse = args.wheel.resolve(strict=True), args.wheelhouse.resolve(strict=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source,
                                           env=env, text=True, timeout=10).strip()
        pins = []
        for dependency in sorted(wheelhouse.glob("*.whl")):
            with zipfile.ZipFile(dependency) as archive:
                names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                assert len(names) == 1
                metadata = BytesParser().parsebytes(archive.read(names[0]))
            if metadata["Name"].lower() == "claudlobby":
                assert dependency.read_bytes() == wheel.read_bytes()
                continue
            pins.append(f"{metadata['Name']}=={metadata['Version']} "
                        f"--hash=sha256:{hashlib.sha256(dependency.read_bytes()).hexdigest()}")
        lock = root / "dependency.lock"
        lock.write_text("\n".join(pins) + "\n")
        # This driver is the checked-out assembler. All subsequent CLI calls
        # use the sealed wheel entrypoint with an unrelated working directory.
        sys.path.insert(0, str(source))
        from claudlobby.release_install import assemble_release
        from claudlobby.releases import read_release

        data = root / "host data with spaces"
        data.mkdir()
        release = assemble_release(data, wheel, lock, wheelhouse, Path(sys.executable))
        assert release.inputs.source_revision == revision
        results.update(release_id=release.release_id, source_revision=release.inputs.source_revision,
                       wheel_sha256=release.inputs.wheel_sha256)
        assert assemble_release(data, wheel, lock, wheelhouse, Path(sys.executable)) == release
        assert read_release(data, release.release_id) == release
        run("help", [release.cli_path, "--help"])
        run("native-parse", ["/bin/bash", "-n", release.native_path / "keepalive.sh"])
        (data / "fleet.yaml").write_text("""fleet:
  name: assembly-smoke
  manager: manager
  service_prefix: com.assembly-smoke
  system_defaults: false
  defaults:
    channels: []
  bots:
    manager:
      expertise: [orchestration]
    worker:
      expertise: [software-engineering]
""")
        try:
            output = json.loads(run("config-plan", [release.cli_path, "--root", data,
                                    "config", "plan", "--release", release.release_id, "--json"]))
        except RuntimeError:
            # Only this generated, secret-free fixture may expose the cause.
            # Public commands intentionally redact authored configuration data.
            run("config-plan-cause", [release.directory / release.paths.interpreter,
                "-I", "-B", "-c",
                "from pathlib import Path; from types import SimpleNamespace; "
                "from claudlobby.commands.releases import _config_plan; "
                "import sys; _config_plan(SimpleNamespace(release=sys.argv[2], "
                "fleet_path=[]), Path(sys.argv[1]))", data, release.release_id])
            raise
        assert output["ok"] and output["data"]["release_id"] == release.release_id
        results["plan_id"] = output["data"]["plan_id"]
        assert not (data / "runtime").exists()
        assert not (data / "state/selected-release.json").exists()
        assert not (data / "state/plane/plane.db").exists()
        assert read_release(data, release.release_id) == release
        results["passed"] = True
    finally:
        (root / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
