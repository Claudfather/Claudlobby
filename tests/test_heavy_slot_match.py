"""What the heavy-job slot gates (#1686): the matcher and the rewrite.

The PreToolUse hook puts a wrapper in front of each heavy command in a Bash
tool call, and in front of nothing else. These tables are the spec:

* GATED — the command as the hook must rewrite it, with the wrapper written
  here as ``W``. The rest of the command is byte-identical, so every other part
  of it (and every rule a permission layer applies to it) is untouched.
* NOT_GATED — heavy words in places that are not a heavy command: an argument,
  a quoted string, a heredoc body, a comment, a targeted test run, a dev
  server. Getting one of these wrong corrupts a command, which is worse than
  missing a gate, so the matcher reads the command the way the shell does.
* UNSURE — constructs the matcher does not parse. It must refuse to rewrite
  them (the hook then lets the call through untouched and counts it), never
  guess where a command starts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hs():
    spec = importlib.util.spec_from_file_location(
        "heavy_slot", REPO / "lib" / "heavy-slot.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


GATED = [
    # pytest, when it runs a suite
    ("pytest", "W pytest"),
    ("pytest -q -x", "W pytest -q -x"),
    ("python3 -m pytest", "W python3 -m pytest"),
    ("python3 -X dev -m pytest", "W python3 -X dev -m pytest"),
    ("./.venv/bin/python -m pytest -q", "W ./.venv/bin/python -m pytest -q"),
    (
        "./.venv/bin/pytest -m 'not quarantine and not harness'",
        "W ./.venv/bin/pytest -m 'not quarantine and not harness'",
    ),
    ("pytest tests/", "W pytest tests/"),
    ("pytest -k digest", "W pytest -k digest"),
    (
        "pytest --deselect tests/test_a.py::test_b",
        "W pytest --deselect tests/test_a.py::test_b",
    ),
    # where the command sits in a Bash tool call
    (
        "cd /x && ./.venv/bin/pytest -q > /tmp/log 2>&1; echo rc=$?",
        "cd /x && W ./.venv/bin/pytest -q > /tmp/log 2>&1; echo rc=$?",
    ),
    ("FOO=1 BAR='a b' pytest", "FOO=1 BAR='a b' W pytest"),
    ("pytest -q 2>&1 | tail -5", "W pytest -q 2>&1 | tail -5"),
    ("nohup pytest > log 2>&1 &", "W nohup pytest > log 2>&1 &"),
    ("time pytest", "time W pytest"),
    ("! pytest -q", "! W pytest -q"),
    ("exec pytest", "exec W pytest"),
    ("command pytest", "command W pytest"),
    ("(cd app && npm ci)", "(cd app && W npm ci)"),
    ("{ npm ci; }", "{ W npm ci; }"),
    ("if true; then npm ci; fi", "if true; then W npm ci; fi"),
    ("while false; do npm ci; done", "while false; do W npm ci; done"),
    (
        "for d in a b; do (cd $d && npm ci); done",
        "for d in a b; do (cd $d && W npm ci); done",
    ),
    ("npm ci && npm run build", "W npm ci && W npm run build"),
    ("cd x\nnpm install", "cd x\nW npm install"),
    ("out=$(npm ci 2>&1)", "out=$(W npm ci 2>&1)"),
    # seen through runners
    ("timeout 900 pytest -q", "W timeout 900 pytest -q"),
    (
        'env -i HOME="$HOME" PATH="$PWD/.venv/bin:/usr/bin" ./.venv/bin/pytest -q',
        'W env -i HOME="$HOME" PATH="$PWD/.venv/bin:/usr/bin" ./.venv/bin/pytest -q',
    ),
    ("uv run pytest", "W uv run pytest"),
    ("nice -n 10 npm ci", "W nice -n 10 npm ci"),
    ("xvfb-run -a npx playwright test", "W xvfb-run -a npx playwright test"),
    # bash -c: rewritten INSIDE the string, so the wrapper never wraps a shell
    ("bash -c 'npm ci'", "bash -c 'W npm ci'"),
    ('sh -c "cd app && npm ci"', 'sh -c "cd app && W npm ci"'),
    # package installs
    ("npm ci", "W npm ci"),
    ("npm i -D vitest", "W npm i -D vitest"),
    ("npm --prefix app ci", "W npm --prefix app ci"),
    ("pnpm install --frozen-lockfile", "W pnpm install --frozen-lockfile"),
    ("yarn", "W yarn"),
    ("yarn install", "W yarn install"),
    # the scripts that are these jobs in product repos (#1686 plan: `build`
    # is `next build` and `test` is vitest in nearly every package.json)
    ("npm test", "W npm test"),
    ("npm test -- -t 'renders'", "W npm test -- -t 'renders'"),
    ("npm run test:ci", "W npm run test:ci"),
    ("npm run build", "W npm run build"),
    ("npm run build:prod", "W npm run build:prod"),
    ("pnpm test", "W pnpm test"),
    ("pnpm build", "W pnpm build"),
    ("yarn build", "W yarn build"),
    # vitest, next build, Playwright, through npx / pnpm / yarn
    ("npx vitest run", "W npx vitest run"),
    ("npx --yes vitest", "W npx --yes vitest"),
    ("npm exec -- vitest run", "W npm exec -- vitest run"),
    ("pnpm exec vitest run", "W pnpm exec vitest run"),
    ("pnpm dlx vitest run", "W pnpm dlx vitest run"),
    ("yarn vitest run", "W yarn vitest run"),
    ("next build", "W next build"),
    ("npx next build", "W npx next build"),
    ("yarn next build", "W yarn next build"),
    ("npx playwright test", "W npx playwright test"),
    ("npx playwright install chromium", "W npx playwright install chromium"),
    (
        "npx -p @playwright/test playwright test",
        "W npx -p @playwright/test playwright test",
    ),
    ("python -m playwright install", "W python -m playwright install"),
    # Chromium, invoked directly
    (
        "chromium --headless --screenshot=out.png https://example.com",
        "W chromium --headless --screenshot=out.png https://example.com",
    ),
    (
        "google-chrome --headless --print-to-pdf=x.pdf page.html",
        "W google-chrome --headless --print-to-pdf=x.pdf page.html",
    ),
]


@pytest.mark.parametrize("command,want", GATED, ids=[c for c, _ in GATED])
def test_a_heavy_command_gets_the_wrapper(hs, command, want):
    assert hs.gate(command, "W") == want


NOT_GATED = [
    # a targeted test run names its files; the slot is for suites
    "pytest tests/test_x.py",
    "pytest tests/test_x.py::TestA::test_b -q",
    "pytest -k foo tests/test_a.py tests/test_b.py",
    "python -m pytest tests/test_sh_suites.py -k transcript_digest",
    "npx vitest run src/a.test.ts",
    "npx vitest run src/__tests__/api/x.test.ts src/y.test.ts",
    "vitest run shopify",
    "vitest related src/a.ts",
    "npm test -- src/a.test.ts",
    # informational
    "pytest --version",
    "pytest -h",
    "npm --version",
    "npm -v",
    "yarn --version",
    "next --version",
    "chromium --version",
    "echo $(pytest --version)",
    # dev servers and watchers, by agreement not jobs
    "npm run dev",
    "npm start",
    "next dev",
    "yarn dev",
    "npm run test:watch",
    "vitest --watch",
    "vitest watch",
    # neither heavy nor a test/build script
    "npm run lint",
    "npm view vitest version",
    "npm ls",
    "playwright show-report",
    "npx playwright codegen",
    "npx tsc --noEmit",
    "pip install pytest",
    "pytest-watch",
    "sudo apt install chromium",
    # heavy words that are not the command
    "grep -rn pytest .",
    "rg vitest src",
    "echo 'run npm ci'",
    'echo "npm ci"',
    'git commit -m "fix: npm ci under pytest"',
    "ls pytest/",
    "cat pytest.ini",
    "which pytest",
    "command -v pytest",
    "type pytest",
    "man pytest",
    "vim package.json",
    "printf 'npm ci\\n' > x.sh",
    "# pytest -q",
    "ls # npm ci",
    # heredoc bodies are data, even inside a quoted command substitution
    "cat > f <<'EOF'\nnpm ci\npytest\nEOF",
    "cat > f <<-EOF\n\tnpm ci\n\tEOF\nls",
    "git commit -m \"$(cat <<'EOF'\nnpm ci && pytest (all\nEOF\n)\"",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['pytest'])\nEOF",
    "cat <<< 'npm ci'",
]


@pytest.mark.parametrize("command", NOT_GATED)
def test_a_command_that_is_not_a_heavy_job_is_left_alone(hs, command):
    assert hs.gate(command, "W") is None


UNSURE = [
    "eval 'npm ci'",
    "case $x in a) npm ci;; esac",
    "f() { npm ci; }; f",
    "coproc npm ci",
    "x=`npm ci`",
    'bash -c "npm ci $X"',
    "echo 'unterminated npm ci",
    "cat <<EOF\nnpm ci",
]


@pytest.mark.parametrize("command", UNSURE)
def test_a_construct_the_matcher_does_not_parse_is_never_rewritten(hs, command):
    with pytest.raises(hs.Unsure):
        hs.gate(command, "W")


def test_the_rewrite_only_inserts_the_wrapper(hs):
    # Every rewrite is the original with the wrapper text inserted: nothing
    # else may move, or the hook would be editing commands it does not own.
    for command, want in GATED:
        got = hs.gate(command, "W")
        assert got.replace("W ", "", got.count("W ")) == command.replace(
            "W ", "", command.count("W ")
        )


class TestTheWrappersOwnCheck:
    """The wrapper refuses to run what is not a heavy job, so its path can
    never become a way to run anything else. It checks the TOOL, not whether
    the run is targeted: the hook decides with the command as written, the
    wrapper sees it after the shell expands it, and the two must never
    disagree in the direction of refusing a job the hook sent."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["pytest"],
            ["pytest", "tests/test_x.py"],
            ["timeout", "900", "pytest"],
            ["env", "-i", "A=1", "./.venv/bin/pytest"],
            ["npm", "ci"],
            ["npx", "vitest", "run", "src/a.test.ts"],
            ["pnpm", "exec", "next", "build"],
            ["yarn"],
            ["chromium", "--headless"],
            ["python3", "-m", "pytest"],
        ],
    )
    def test_a_heavy_tool_is_accepted(self, hs, argv):
        assert hs.heavy_family(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["ls"],
            ["rm", "-rf", "x"],
            ["bash", "-c", "npm ci"],
            ["sh", "-c", "pytest"],
            ["env", "rm", "-rf", "x"],
            ["timeout", "5", "bash", "-c", "pytest; rm -rf x"],
            ["npx", "some-package"],
            ["npm", "run", "deploy"],
            ["python3", "script.py"],
            [],
        ],
    )
    def test_anything_else_is_refused(self, hs, argv):
        assert not hs.heavy_family(argv)
