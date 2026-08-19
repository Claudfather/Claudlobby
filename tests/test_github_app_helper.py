"""App-auth P1 (#1271): lib/git-credential-github-app + mint + setup.

Lane-A wrappers (model: tests/test_creds_check_telegram.py): the real scripts
run under subprocess with the network stubbed on a private PATH. Real openssl
signs a real throwaway RSA key — only curl is faked, so the JWT the stub
captures is the JWT production would send.

The pins that carry the plan's contracts:
- D1/D10 (helper-direct): the mint CLI never shells to `git credential`.
- D9: mint failure = empty stdout + nonzero + stderr reason.
- D11: hard helper failure prints quit=1 so git stops the helper chain.
- D12: the App JWT iat is backdated (Pi boot-clock window).
- House secrets rule: neither token nor JWT nor key material rides argv, and
  the auth_mint_failed event never contains a token.
"""

import base64
import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import _write_exec, constructed_env

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "lib" / "git-credential-github-app"
MINT = REPO_ROOT / "lib" / "mint-github-token.sh"
SETUP = REPO_ROOT / "lib" / "setup-github-app.sh"

CTX = "protocol=https\nhost=github.com\n\n"
STUB_TOKEN = "ghs_STUBTOKEN1234567890"


@pytest.fixture(scope="session")
def rsa_key(tmp_path_factory):
    """A real throwaway RSA key — openssl signs for real; only curl is faked."""
    key = tmp_path_factory.mktemp("appkey") / "private-key.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )
    return key


def _curl_stub(bindir: Path) -> None:
    """Fake curl: honors -o/-w/--config enough for the helper and setup.

    Logs argv (so tests can prove no secret rides the command line) and the
    --config file content (so tests can decode the JWT production would send).
    GITHUB_APP_STUB_MODE picks the response: ok | http401 | curlfail.
    """
    _write_exec(
        bindir / "curl",
        """#!/bin/bash
printf '%s\\n' "$*" >> "$STUB_DIR/argv.log"
out_file="" cfg_file="" url="" prev=""
for a in "$@"; do
  case "$prev" in
    -o) out_file="$a";;
    --config) cfg_file="$a";;
  esac
  case "$a" in
    https://*) url="$a";;
  esac
  prev="$a"
done
[ -n "$cfg_file" ] && cat "$cfg_file" >> "$STUB_DIR/cfg.log"
mode="${GITHUB_APP_STUB_MODE:-ok}"
if [ "$mode" = curlfail ]; then
  printf 'stub: connection refused\\n' >&2
  exit 7
fi
case "$url" in
  *access_tokens*)
    if [ "$mode" = http401 ]; then
      body='{"message":"A JSON web token could not be decoded"}'
      code=401
    else
      body='{"token":"__TOKEN__"}'
      code=201
    fi
    if [ -n "$out_file" ]; then
      printf '%s' "$body" > "$out_file"
      printf '%s' "$code"
    else
      printf '%s' "$body"
      [ "$code" = 401 ] && exit 22
    fi
    ;;
  *users/*)
    printf '{"id": 4242}'
    ;;
esac
""".replace("__TOKEN__", STUB_TOKEN),
    )


@pytest.fixture
def app_env(tmp_path, rsa_key):
    """Scratch root + curl stub + a fully-configured env (config via env vars)."""
    stub = tmp_path / "stub-bin"
    stub.mkdir()
    _curl_stub(stub)
    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    import os

    env = constructed_env(
        PATH=f"{stub}:{os.environ['PATH']}",
        STUB_DIR=str(stub),
        HOME=str(home),
        CLAUDLOBBY_ROOT=str(root),
        GITHUB_APP_ID="999001",
        GITHUB_APP_INSTALLATION_ID="555002",
        GITHUB_APP_PRIVATE_KEY_PATH=str(rsa_key),
    )
    return {"env": env, "stub": stub, "root": root, "home": home}


def _run(script, env, stdin=CTX, args=("get",)):
    return subprocess.run(
        ["bash", str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def _events(root: Path) -> str:
    return "".join(
        p.read_text() for p in (root / "state" / "events").glob("fleet-*.jsonl")
    )


class TestHelperGet:
    def test_happy_path_answers_the_credential_protocol(self, app_env):
        r = _run(HELPER, app_env["env"])
        assert r.returncode == 0, r.stderr
        assert "username=x-access-token" in r.stdout
        assert f"password={STUB_TOKEN}" in r.stdout
        assert "quit=" not in r.stdout

    def test_neither_token_nor_jwt_nor_key_rides_argv(self, app_env):
        _run(HELPER, app_env["env"])
        argv = (app_env["stub"] / "argv.log").read_text()
        assert "ghs_" not in argv, "token leaked onto curl argv"
        assert "eyJ" not in argv, "JWT leaked onto curl argv"
        assert "Authorization" not in argv, "auth header must ride --config"

    def test_jwt_iat_is_backdated(self, app_env):
        before = int(time.time())
        _run(HELPER, app_env["env"])
        cfg = (app_env["stub"] / "cfg.log").read_text()
        jwt = cfg.split("Bearer ")[1].split('"')[0]
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        assert payload["iat"] <= before - 50, "iat must be backdated ~60s (D12)"
        assert payload["exp"] > before, "exp must be in the future"
        assert payload["iss"] == "999001"

    def test_foreign_host_gets_silence_not_quit(self, app_env):
        r = _run(HELPER, app_env["env"], stdin="protocol=https\nhost=gitlab.com\n\n")
        assert r.returncode == 0
        assert r.stdout == ""

    def test_http_401_is_loud_quit_plus_event(self, app_env):
        env = dict(app_env["env"], GITHUB_APP_STUB_MODE="http401")
        r = _run(HELPER, env)
        assert r.returncode != 0
        assert "quit=1" in r.stdout, "hard failure must stop the helper chain (D11)"
        assert "password=" not in r.stdout
        assert "401" in r.stderr
        events = _events(app_env["root"])
        assert '"type":"auth_mint_failed"' in events
        assert "ghs_" not in events, "event must never carry a token"

    def test_missing_config_is_loud(self, app_env):
        env = {
            k: v
            for k, v in app_env["env"].items()
            if not k.startswith("GITHUB_APP_")
        }
        r = _run(HELPER, env)
        assert r.returncode != 0
        assert "quit=1" in r.stdout
        assert "GITHUB_APP_ID" in r.stderr

    def test_config_file_fallback_and_env_precedence(self, app_env, rsa_key):
        conf = app_env["home"] / "github-app.conf"
        conf.write_text(
            'GITHUB_APP_ID="999001"\n'
            'GITHUB_APP_INSTALLATION_ID="111999"\n'
            f'GITHUB_APP_PRIVATE_KEY_PATH="{rsa_key}"\n'
        )
        base = {
            k: v
            for k, v in app_env["env"].items()
            if not k.startswith("GITHUB_APP_")
        }
        base["CLAUDLOBBY_GITHUB_APP_CONF"] = str(conf)

        # Fallback: no env vars — the config file alone serves the mint, and
        # the stub sees the CONFIG installation id in the exchange URL.
        r = _run(HELPER, base)
        assert r.returncode == 0, r.stderr
        assert f"password={STUB_TOKEN}" in r.stdout
        assert "111999" in (app_env["stub"] / "argv.log").read_text()

        # Precedence: an env var beats the same key in the config file.
        (app_env["stub"] / "argv.log").write_text("")
        env2 = dict(base, GITHUB_APP_INSTALLATION_ID="555002")
        r2 = _run(HELPER, env2)
        assert r2.returncode == 0, r2.stderr
        argv = (app_env["stub"] / "argv.log").read_text()
        assert "555002" in argv and "111999" not in argv

    def test_store_and_erase_are_silent_noops(self, app_env):
        for action in ("store", "erase"):
            r = _run(HELPER, app_env["env"], args=(action,))
            assert (r.returncode, r.stdout) == (0, "")

    def test_missing_openssl_fails_cleanly(self, app_env, sysbin_excluding):
        mirror = sysbin_excluding("openssl")
        env = dict(app_env["env"], PATH=f"{app_env['stub']}:{mirror}")
        r = _run(HELPER, env)
        assert r.returncode != 0
        assert "quit=1" in r.stdout
        assert "openssl" in r.stderr


class TestMintCli:
    def test_prints_bare_token(self, app_env):
        r = _run(MINT, app_env["env"], args=())
        assert r.returncode == 0, r.stderr
        assert r.stdout == STUB_TOKEN

    def test_failure_is_empty_stdout_nonzero_with_stderr(self, app_env):
        env = dict(app_env["env"], GITHUB_APP_STUB_MODE="http401")
        r = _run(MINT, env, args=())
        assert r.returncode != 0
        assert r.stdout == "", "D9: failure must leave stdout empty"
        assert "mint-github-token" in r.stderr

    def test_never_shells_to_git_credential(self, app_env):
        # D1/D10 pin: a booby-trapped `git` on PATH proves the mint path never
        # consults git at all — helper-direct is the program invariant.
        trap = app_env["stub"] / "git"
        _write_exec(trap, "#!/bin/bash\ntouch \"$STUB_DIR/git-was-called\"\nexit 1\n")
        r = _run(MINT, app_env["env"], args=())
        assert r.returncode == 0, r.stderr
        assert not (app_env["stub"] / "git-was-called").exists()


class TestSetupScript:
    def test_happy_path_writes_config_and_prints_wiring(self, app_env, rsa_key, tmp_path):
        conf = tmp_path / "conf" / "github-app.conf"
        r = _run(
            SETUP,
            app_env["env"],
            stdin="",
            args=(
                "--app-id", "999001",
                "--installation-id", "555002",
                "--private-key", str(rsa_key),
                "--slug", "test-fleet-bot",
                "--config-path", str(conf),
            ),
        )
        assert r.returncode == 0, r.stderr + r.stdout
        assert conf.exists()
        assert stat.S_IMODE(conf.stat().st_mode) == 0o600
        assert "bot_user_id: 4242" in r.stdout
        assert "GITHUB_APP_ID=999001" in r.stdout
        assert "4242+test-fleet-bot[bot]@users.noreply.github.com" in r.stdout
        # Redesign pin: setup never touches git config (the composer owns it).
        assert "git config" not in conf.read_text()

    def test_corrupt_key_gets_the_diagnostic(self, app_env, tmp_path, rsa_key):
        # A truncated PEM fails `openssl rsa -check` on every build (CRLF
        # tolerance varies by openssl version — measured: OpenSSL 3.6 accepts
        # CRLF, so the fork's CRLF rejection belonged to its PyJWT path).
        # The diagnostic block itself still names CRLF as a common cause.
        bad = tmp_path / "truncated.pem"
        bad.write_bytes(rsa_key.read_bytes()[: len(rsa_key.read_bytes()) // 2])
        r = _run(
            SETUP,
            app_env["env"],
            stdin="",
            args=(
                "--app-id", "999001",
                "--installation-id", "555002",
                "--private-key", str(bad),
                "--slug", "test-fleet-bot",
                "--no-write-config",
            ),
        )
        assert r.returncode != 0
        assert "CRLF" in r.stderr

    def test_jwt_401_prints_the_troubleshooting_tree(self, app_env, rsa_key):
        env = dict(app_env["env"], GITHUB_APP_STUB_MODE="http401")
        r = _run(
            SETUP,
            env,
            stdin="",
            args=(
                "--app-id", "999001",
                "--installation-id", "555002",
                "--private-key", str(rsa_key),
                "--slug", "test-fleet-bot",
                "--no-write-config",
            ),
        )
        assert r.returncode != 0
        assert "Most likely causes" in r.stderr
        assert "fingerprint" in r.stderr
