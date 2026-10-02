"""The credential-echo guard (#2090): refuse a CLI form that prints an
env-held credential into the transcript, unless the variable is removed in
the same command.

`neonctl --help` printed a real NEON_API_KEY into a session transcript: the
CLI shows the variable as the default of `--api-key`. The matrix below is the
evidence table itself (fixtures/credential_echo/registry-rows.json, the
canary probe kit's table A as data): every row that echoes is refused as
written, every `unset` row passes once its variables are removed in the same
command, and the controls, which never echo, pass untouched.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.conftest import constructed_env, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "claudlobby/_runtime_scripts" / "credential-echo-guard.sh"
TABLE = json.loads((REPO / "tests/fixtures/credential_echo/registry-rows.json").read_text())


def _run(command: str, env: dict | None = None, tool: str = "Bash") -> subprocess.CompletedProcess:
    # The keys of a live PreToolUse payload (as in test_heavy_slot_guard), identifiers faked.
    payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": "/tmp",
               "permission_mode": "auto", "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command}, "tool_use_id": "toolu_x", "prompt_id": "p"}
    return subprocess.run(["bash", str(GUARD)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env or constructed_env(), timeout=60)


def _decision(p: subprocess.CompletedProcess):
    if not p.stdout.strip():
        return None
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return out.get("permissionDecision"), out.get("permissionDecisionReason", "")


def _as_written(row: dict) -> str:
    assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in row.get("env", {}).items())
    command = shlex.join(row["argv"])
    return f"{assignments} {command}" if assignments else command


GUARDED = [r for r in TABLE["rows"] if r["action"] != "optional"]
UNSET = [r for r in TABLE["rows"] if r["action"] == "unset"]
UNTOUCHED = TABLE["controls"] + [r for r in TABLE["rows"] if r["action"] == "optional"]


@pytest.mark.parametrize("row", GUARDED, ids=lambda r: r["id"])
def test_every_echoing_row_is_refused_as_written(row):
    verdict = _decision(_run(_as_written(row)))
    assert verdict is not None and verdict[0] == "deny", (row["id"], verdict)


@pytest.mark.parametrize("neutralise", ["env -u", "env -i", "empty assignment", "unset first"])
@pytest.mark.parametrize("row", UNSET, ids=lambda r: r["id"])
def test_an_unset_row_passes_once_its_variables_are_removed(row, neutralise):
    command, names = shlex.join(row["argv"]), row["vars"]
    if neutralise == "env -u":
        command = "env " + " ".join(f"-u {n}" for n in names) + " " + command
    elif neutralise == "env -i":
        command = 'env -i PATH="$PATH" HOME="$HOME" ' + command
    elif neutralise == "empty assignment":
        command = " ".join(f"{n}=" for n in names) + " " + command
    else:
        command = "unset " + " ".join(names) + "; " + command
    assert _decision(_run(command)) is None, command


@pytest.mark.parametrize("row", UNTOUCHED, ids=lambda r: r["id"])
def test_a_form_that_does_not_echo_is_untouched(row):
    assert _decision(_run(_as_written(row))) is None, row["id"]


REFUSED = [
    "ls && neonctl --help",
    "cd /tmp;neonctl --help",
    "(neonctl --help)",
    "timeout 10 neonctl --help",
    "/usr/local/bin/neonctl --help",
    "ls\nneonctl --help",
    "ls && \\\nneonctl --help",
    "bash -c 'neonctl --help'",
    "env -u NEON_API_KEY true && neonctl --help",  # env scopes the other command
    "unset NEON_API_KEY | neonctl --help",  # a pipeline element is its own subshell
    "NEON_API_KEY=CANARY_x7q2 neonctl --help",  # set, not removed
    "neonctl",
    "neon projects",
    "neonctl -o json",
    "neonctl --bogus projects list",
    "env -u NEON_API_KEY DEBUG='*' neonctl me",  # the trace prints the stored login too
    "DEBUG=neonctl* neon projects list",
    "python3 -m pip config list",
    "pip3 config debug",
    "env -u GH_TOKEN gh auth token",  # falls back to the stored login's token
    "gh auth token | cat",
    "gh auth token 2>/dev/null",
    "gh auth token > /dev/stdout",
    "gh auth token >&2",
    "echo $(gh auth token)",
    "cat <<EOF\nhi\nEOF\nneonctl --help",  # a command after a heredoc is judged
    "for i in 1; do neonctl --help; done",
    "if true; then neonctl --help; fi",
    "while true; do neonctl --help; break; done",
    "if true; then :; else neonctl --help; fi",
    "! neonctl --help",
    'echo "$(neonctl --help)"',  # a substitution runs inside double quotes
    'echo "`neonctl --help`"',
    "bash <<'EOF'\nneonctl --help\nEOF",  # a heredoc fed to a shell is commands
    "cat <<'EOF' | bash\nneonctl --help\nEOF",
    "cat <<EOF\n$(neonctl --help)\nEOF",  # an unquoted heredoc runs its substitutions
    "echo 'neonctl --help' | sh",
    "bash <<< 'neonctl --help'",
    "bash -ec 'neonctl --help'",
    "ne'on'ctl --help",  # a name split by quoting runs the same CLI
    '"n"eonctl --help',
    "pip --tim 5 config list",  # optparse takes any unambiguous prefix
    "pip config --editor vi list",  # an option's value is not the action
    "pip --log-file x config debug",  # a hidden option takes a value too
    "env --ch /tmp neonctl --help",  # getopt_long takes a prefix of --chdir
    "env -S 'neonctl --help'",  # env splits the string into the command
    "env -u NEON_API_KEY -S 'DEBUG=1 neonctl me'",
    "setsid -f neonctl --help",
    "nice --adj 5 neonctl --help",
    "timeout -k 5 10 neonctl --help",
    "bunx neonctl --help",
    "pnpm dlx neonctl --help",
    "npm exec -- neonctl --help",
    "npx -c 'neonctl --help'",
    "$'\\x6eeonctl' --help",  # an ANSI-C string decodes as bash decodes it
    "$'neon\\143tl' --help",
    "gh $'\\x61uth' token",
    '$"neonctl" --help',  # a translated string reads as a double-quoted one
    "false && unset NEON_API_KEY; neonctl --help",  # a removal that never ran
    "cd /nowhere && unset NEON_API_KEY; neonctl --help",
    "(false) && unset NEON_API_KEY; neonctl --help",
    "if false; then unset NEON_API_KEY; fi; neonctl --help",
    "for i in; do unset NEON_API_KEY; done; neonctl --help",
    "f() { unset NEON_API_KEY; }; neonctl --help",  # a function body runs when called
    "unset -f NEON_API_KEY; neonctl --help",  # removes a function, not the variable
    "echo | unset NEON_API_KEY; neonctl --help",  # a pipeline's last element is a subshell
]

ALLOWED = [
    'echo "neonctl --help"',
    "# neonctl --help",
    "grep -n neonctl notes.md",
    "neonctl projects list",
    "neonctl -o json projects list",
    "neon branches list --project-id p-1",
    "unset NEON_API_KEY && neonctl --help",
    "env --unset=NEON_API_KEY neonctl --help",
    "env -u NEON_API_KEY bash -c 'neonctl --help'",
    "NEON_API_KEY= neon projects --help",
    "gh auth token > token.txt",
    "gh auth token >> token.txt",
    "gh auth token &> token.txt",
    "git status",
    "git commit -F - <<'EOF'\nmentions neonctl --help here\nEOF",  # a heredoc body is data
    "command -v neonctl",  # a lookup runs nothing
    "command -V neon",
    "for i in neonctl neon; do echo $i; done",
    "cat <<'EOF'\n$(neonctl --help)\nEOF",  # a quoted heredoc is literal
    "neonctl projects nosuchverb",  # an unknown command prints only an error
    "pip --timeout 5 install requests",
    "pip --tim 5 config get global.index-url",
    "env -S 'neonctl projects list'",
    "env -u NEON_API_KEY -S 'neonctl --help'",
    "env --uns=NEON_API_KEY neonctl --help",  # a prefix of --unset still removes it
    "setsid -f neonctl projects list",
    "pnpm dlx neonctl projects list",
    "bunx neonctl@2 projects list",
    "echo $'neonctl --help'",
    "unset -v NEON_API_KEY; neonctl --help",
    "{ unset NEON_API_KEY; neonctl --help; }",
    "cd /tmp; unset NEON_API_KEY && neonctl --help",
]

# ---- probes added in review of #2091 (vera's, pasted as given) -----------------------------------------
PROBE_REFUSED = [
    "if true; then neonctl --help; fi",
    "for i in 1; do neonctl --help; done",
    "while true; do neonctl --help; break; done",
    "if neonctl --help; then :; fi",
    "! neonctl --help",
    'echo "$(neonctl --help)"',
    'x="$(neonctl --help)"; echo "$x"',
    'echo "`neonctl --help`"',
    'printf "%s" "$(pip config list)"',
    'echo "$(gh auth token)"',
    "cat <<EOF\n$(neonctl --help)\nEOF",
    "bash <<'EOF'\nneonctl --help\nEOF",
    "sh <<EOF\npip config list\nEOF",
    "bash -s <<'EOF'\ngh auth token\nEOF",
    "bash -ec 'neonctl --help'",
    "bash -lc 'pip config list'",
    "sh -xc 'neonctl --help'",
    'echo "$(neonctl --help 2>&1 | sed "s/a/b/")"',
    "ne''onctl --help",
    'n"e"onctl --help',
    "pip con''fig list",
    "gh au''th token",
    "pip --timeout 5 config list",
    "pip --cache-dir /tmp/c config list",
    "env -- neonctl --help",
    "env -u OTHER -- neonctl --help",
    "command -p neonctl --help",
    "time -p neonctl --help",
]
PROBE_ALLOWED = [
    "command -v neonctl",
    "command -v neon",
    "command -V neonctl",
    "neonctl nosuchcmd",
    "neonctl help",
    "cat <<EOF\nneonctl --help\nEOF",
    "cat <<'EOF'\n$(neonctl --help)\nEOF",
    "bash <<'EOF'\necho neonctl --help\nEOF",
    "env -u NEON_API_KEY -- neonctl --help",
    "env -i -- neonctl --help",
]


@pytest.mark.parametrize("command", REFUSED)
def test_the_command_forms_around_a_row_are_refused(command):
    verdict = _decision(_run(command))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)


@pytest.mark.parametrize("command", ALLOWED)
def test_the_command_forms_around_a_row_are_allowed(command):
    assert _decision(_run(command)) is None, command


@pytest.mark.parametrize("command", PROBE_REFUSED)
def test_probe_a_form_the_review_got_past_the_guard_is_refused(command):
    verdict = _decision(_run(command))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)


@pytest.mark.parametrize("command", PROBE_ALLOWED)
def test_probe_ordinary_work_the_review_saw_refused_is_allowed(command):
    assert _decision(_run(command)) is None, command


# #2097: a double-quoted substitution the decider could not delimit made the
# whole line unreadable, and an unreadable line is allowed, so an echoing form
# beside it went unjudged. The usual shape is a heredoc post whose prose holds an
# apostrophe or an unmatched parenthesis. Each prose below names config, auth or
# neon, so the prefilter hands it to the decider.
POST_APOSTROPHE = "gh pr comment 1 --body \"$(cat <<'EOF'\nIt's a note about the config\nEOF\n)\""
POST_PAREN = "gh pr comment 1 --body \"$(cat <<'EOF'\nsee (the auth notes\nEOF\n)\""

QUOTED_SUBST_REFUSED = [
    POST_APOSTROPHE + " && gh auth token",
    "gh auth token; " + POST_APOSTROPHE,
    POST_PAREN + " && neonctl --help",
    "gh pr comment 1 --body \"$(neonctl --help; cat <<'EOF'\nIt's a note\nEOF\n)\"",  # the help lands in the post
    "echo \"$(neonctl --help # it's a comment\n)\"",
    'echo "$(case x in x) neonctl --help;; esac)"',  # a case pattern's parenthesis
    'echo "$(case x in (x) pip config list;; esac)"',  # an opening parenthesis already balanced it
    "cat <<EOF\n$(echo it's)\nEOF\ngh auth token",  # bash still runs the line after the heredoc
    "echo \"$(echo it's)\" && gh auth token",  # cannot be delimited: the rest is still judged
    "cat <<EOF\nuse `x for the config\nEOF\ngh auth token",  # a lone backtick in a heredoc body
]
QUOTED_SUBST_ALLOWED = [
    POST_APOSTROPHE,
    POST_PAREN,
    "gh pr comment 1 --body \"$(cat <<'EOF'\nDon't run neonctl --help here\nEOF\n)\"",
    'echo "$(case x in x) echo config;; esac)"',
]


@pytest.mark.parametrize("command", QUOTED_SUBST_REFUSED)
def test_a_quoted_substitution_never_hides_the_rest_of_its_line(command):
    verdict = _decision(_run(command))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)


@pytest.mark.parametrize("command", QUOTED_SUBST_ALLOWED)
def test_a_heredoc_post_whose_prose_has_quotes_or_parentheses_is_allowed(command):
    assert _decision(_run(command)) is None, command


def test_the_gh_token_refusal_leads_with_letting_gh_read_it():
    # A token sent to a file stays readable on disk by every bot on the uid.
    verdict = _decision(_run("gh auth token"))
    assert verdict is not None and verdict[0] == "deny"
    assert verdict[1].startswith("Let gh read the token itself"), verdict[1]
    assert verdict[1].index("Let gh") < verdict[1].index("> FILE")


def test_the_guard_events_are_registered_as_notices():
    # Unregistered, they would land on the plane with no severity.
    assert SYSTEM_EVENT_SEVERITY.get("credential_echo_refused") == "notice"
    assert SYSTEM_EVENT_SEVERITY.get("credential_echo_unparsed") == "notice"


def test_the_refusal_names_the_safe_form_and_never_a_value():
    verdict = _decision(_run("NEON_API_KEY=CANARY_x7q2_rs01 neonctl --help"))
    assert verdict is not None and verdict[0] == "deny"
    assert "env -u NEON_API_KEY" in verdict[1]
    assert "CANARY_x7q2_rs01" not in verdict[1]


def test_another_tool_is_untouched():
    assert _decision(_run("neonctl --help", tool="Read")) is None


def test_a_malformed_payload_fails_open():
    p = subprocess.run(["bash", str(GUARD)], input="Bash neonctl {not json", capture_output=True,
                       text=True, env=constructed_env(), timeout=60)
    assert p.returncode == 0 and _decision(p) is None


def test_a_refusal_is_recorded_with_its_row_and_never_the_command(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    env = constructed_env(HOME=tmp_path / "home", FLEET_NAME="testfleet", BOT_ID="tbot",
                          BOT_DIR=bot, **scratch_plane_env(root, initialize=True))
    with _serving(root, scratch_plane_env) as socket:
        p = _run("NEON_API_KEY=CANARY_x7q2_ev01 neonctl --help",
                 env={**env, "PLANE_SOCKET": str(socket)})
    assert _decision(p) is not None and _decision(p)[0] == "deny", p.stderr
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    refused = [r for r in rows if r["type"] == "credential_echo_refused"]
    assert len(refused) == 1, (rows, p.stderr)
    assert refused[0]["source"] == "credential-echo-guard", refused[0]
    assert refused[0]["data"] == {"row": "neon-help", "cli": "neonctl"}, refused[0]
    assert "CANARY_x7q2_ev01" not in json.dumps(rows)
