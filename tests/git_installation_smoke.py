"""Real prepare-only Git installation proof in private disposable directories.

Uses the checkout's exact commit as a trusted local remote. No fleet is authored
or applied, and no native manager command, bot, grant or Plane writer is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-root", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    root = args.disposable_root.absolute()
    if (root != root.resolve() or root.exists() or root.is_relative_to(source)
            or source.is_relative_to(root)):
        raise SystemExit("smoke needs a new unredirected disposable root outside the checkout")
    root.mkdir(mode=0o700, parents=True)
    interpreter = Path(sys.executable).resolve()
    env = {"PATH": f"{interpreter.parent}:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
           "PLANE_EMIT_DISABLED": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        directory = root / key.lower()
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    # Seed the private pip cache from the lane's existing downloads, when present.
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pip"
    if cache.is_dir() and not cache.is_symlink():
        shutil.copytree(cache, Path(env["XDG_CACHE_HOME"]) / "pip")
    results = {"steps": []}

    def run(name, command, *, cwd=root):
        start = time.monotonic()
        with (root / f"{name}.log").open("w") as log, (root / f"{name}.stderr.log").open("w") as errors:
            process = subprocess.Popen([str(item) for item in command], cwd=cwd, env=env,
                                       stdout=log, stderr=errors, start_new_session=True)
            try:
                code = process.wait(timeout=600)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
        results["steps"].append({"name": name, "exit": code, "seconds": time.monotonic() - start})
        if code:
            raise RuntimeError(f"{name} failed ({code}); see private smoke logs")
        return (root / f"{name}.log").read_text()

    try:
        revision = run("source-revision", ["git", "--no-optional-locks", "-C", source,
                                          "rev-parse", "--verify", "HEAD^{commit}"]).strip()
        installation, data = root / "installation", root / "persistent data"
        command = [interpreter, "-B", "-m", "claudlobby.bootstrap", "--installation", installation,
                   "--root", data, "--remote", source, "--ref", revision,
                   "--interpreter", interpreter, "--json"]
        prepared = json.loads(run("prepare", command, cwd=source))
        assert prepared["ok"] and prepared["prepared"] and prepared["activated"] is False
        assert prepared["source_revision"] == revision
        cli = installation / "bin/claudlobby"
        assert prepared["cli"] == str(cli)
        entries = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (cli, installation / "bin/claudlobby-update")}
        run("help", [cli, "--help"])
        status = json.loads(run("status", [cli, "--json", "host", "update", "status"]))
        assert status == {"read_only": True, "update": None}
        assert json.loads(run("retry", command, cwd=source)) == prepared
        assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (cli, installation / "bin/claudlobby-update")} == entries
        for relative in ("state/config.json", "state/update.json", "state/selected-release.json",
                         "state/activations", "state/plane", "runtime", "local"):
            assert not os.path.lexists(data / relative), relative
        for directory in (Path(env["XDG_CONFIG_HOME"]) / "systemd/user",
                          Path(env["HOME"]) / ".config/systemd/user",
                          Path(env["HOME"]) / "Library/LaunchAgents"):
            assert not os.path.lexists(directory), str(directory)
        results.update(passed=True, source_revision=revision, prepared=True, activated=False)
    finally:
        (root / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
