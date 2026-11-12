"""The scheduled pull names a local-commit/upstream blocker without pulling."""

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts" / "git-pull-all.sh"


def test_local_only_checkout_is_reported_as_blocked(tmp_path):
    projects = tmp_path / "projects"
    repo = projects / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "work.txt").write_text("local\n")
    subprocess.run(["git", "-C", str(repo), "add", "work.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "local"], check=True)
    env = {**os.environ, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "CLAUDLOBBY_ROOT": str(tmp_path), "PLANE_EMIT_DISABLED": "1"}
    result = subprocess.run(["bash", str(SCRIPT), str(projects), "--status-nul"],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"git-pull-all-v1\0repo\0skipped_blocked\0"
    assert "no upstream" in (tmp_path / "git-pull.log").read_text()
