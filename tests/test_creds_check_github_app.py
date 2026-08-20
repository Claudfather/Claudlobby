"""App-auth P4 (#1274): the GitHub App fallback in creds-check.sh.

check_github_pat gains an App branch BEFORE the skip: a fleet that mints
installation tokens at use time is probed, not skipped. Isolated by copying
creds-check.sh + lib-common.sh + a STUB mint CLI into a temp lib — full
control of the probe's HTTP code, no key/network (the real mint→helper→
openssl path is proven by scenario 12 and the P1 battery).
"""

import json
import shutil
import subprocess
from pathlib import Path

from tests.conftest import _write_exec, constructed_env

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fleet(tmp_path, *, app_env=None, mint="ok", probe_code="200", pat=None):
    """Build a minimal one-fleet root. app_env: the GITHUB_APP_* dict to write
    into the fleet .env (None = none declared). mint: ok|fail. probe_code: the
    HTTP code the curl stub returns for /installation/repositories."""
    root = tmp_path / "root"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (root / "state").mkdir()
    for helper in ("creds-check.sh", "lib-common.sh"):
        shutil.copy(REPO_ROOT / "lib" / helper, lib / helper)

    # Stub mint CLI: prints a token, or fails per `mint`.
    if mint == "ok":
        _write_exec(lib / "mint-github-token.sh", "#!/bin/bash\nprintf 'ghs_STUBINSTALL'\n")
    else:
        _write_exec(lib / "mint-github-token.sh", "#!/bin/bash\nexit 1\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # curl stub: -o /dev/null -w %{http_code} → print the code. Answers the
    # installation probe with probe_code; the PAT /user path returns 200 so a
    # PAT test can also run through here.
    _write_exec(
        bindir / "curl",
        f"""#!/bin/bash
out="" ; prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  case "$a" in https://*) url="$a";; esac
  prev="$a"
done
case "$url" in
  *installation/repositories*) printf '%s' "{probe_code}" ;;
  *api.github.com/user*)       printf '200' ;;
  *) printf '000' ;;
esac
""",
    )
    # jq stub (creds-check needs it for state writes).
    real_jq = shutil.which("jq")
    if real_jq:
        (bindir / "jq").symlink_to(real_jq)

    fleet_dir = root / "local" / "f"
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: f\n  bots:\n    b1:\n      expertise: [x]\n")
    env_lines = []
    if pat:
        env_lines.append(f'GITHUB_PAT="{pat}"')
    for k, v in (app_env or {}).items():
        env_lines.append(f'{k}="{v}"')
    (fleet_dir / ".env").write_text("\n".join(env_lines) + "\n")

    state = root / "state" / "creds-check-state.json"
    (tmp_path / "home").mkdir()
    env = constructed_env(
        PATH=f"{bindir}:/usr/bin:/bin",
        HOME=str(tmp_path / "home"),
        CLAUDLOBBY_ROOT=str(root),
        CLAUDLOBBY_CREDS_LOG=str(root / "creds-check.log"),
        CLAUDLOBBY_CREDS_STATE=str(state),
    )
    return {"lib": lib, "state": state, "env": env}


def _run(f):
    subprocess.run(
        ["bash", str(f["lib"] / "creds-check.sh"), "f"],
        env=f["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(f["state"].read_text())


APP = {
    "GITHUB_APP_ID": "999001",
    "GITHUB_APP_INSTALLATION_ID": "555002",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/tmp/nonexistent-key.pem",
}


def test_app_configured_healthy_records_ok(tmp_path):
    state = _run(_fleet(tmp_path, app_env=APP, probe_code="200"))
    row = state["github_pat"]
    assert row["status"] == "ok"
    assert "installation token" in row["detail"].lower()


def test_app_mint_failure_records_fail(tmp_path):
    state = _run(_fleet(tmp_path, app_env=APP, mint="fail"))
    row = state["github_pat"]
    assert row["status"] == "fail"
    assert "mint failed" in row["detail"]


def test_app_401_is_distinguished_from_403(tmp_path):
    row401 = _run(_fleet(tmp_path / "a", app_env=APP, probe_code="401"))["github_pat"]
    assert row401["status"] == "fail" and "401" in row401["detail"]
    row403 = _run(_fleet(tmp_path / "b", app_env=APP, probe_code="403"))["github_pat"]
    assert row403["status"] == "fail" and "403" in row403["detail"]
    assert "scope" in row403["detail"]


def test_no_pat_and_no_app_config_still_skips(tmp_path):
    # #1213 composition: an App-less, PAT-less fleet keeps skipping — P4 does
    # not change that half; it only adds the App branch. Detail updated.
    state = _run(_fleet(tmp_path, app_env=None))
    row = state["github_pat"]
    assert row["status"] == "skip"
    assert "no GITHUB_APP_" in row["detail"]


def test_partial_app_config_does_not_attempt_a_mint(tmp_path):
    # The gate needs ALL THREE vars; a fleet with only one is not App-mode and
    # must not attempt a mint (it would fail spuriously).
    state = _run(_fleet(tmp_path, app_env={"GITHUB_APP_ID": "999001"}))
    assert state["github_pat"]["status"] == "skip"


def test_static_pat_still_takes_precedence(tmp_path):
    # A fleet with both a PAT and App config uses the PAT path (unchanged) —
    # the App branch is only reached when no static token exists.
    state = _run(_fleet(tmp_path, pat="ghp_realtoken", app_env=APP))
    row = state["github_pat"]
    assert row["status"] == "ok"
    assert row["detail"] == "HTTP 200"  # the /user path, not the App path


def test_app_token_never_rides_curl_argv(tmp_path):
    # The house invariant: the minted token goes through auth_curl_cfg, never
    # the command line. Re-stub curl to log argv, assert the token is absent.
    f = _fleet(tmp_path, app_env=APP, probe_code="200")
    bindir = Path(f["env"]["PATH"].split(":")[0])
    _write_exec(
        bindir / "curl",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$STUB_ARGV"\n'
        'for a in "$@"; do case "$a" in https://*) url="$a";; esac; done\n'
        'case "$url" in *installation/repositories*) printf "200";; *) printf "000";; esac\n',
    )
    f["env"]["STUB_ARGV"] = str(bindir / "argv.log")
    _run(f)
    argv = (bindir / "argv.log").read_text()
    assert "ghs_STUBINSTALL" not in argv
    assert "Authorization" not in argv
