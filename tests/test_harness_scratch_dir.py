"""A harness instrument that cannot make its temporary directory removes nothing.

On 2026-11-21 a test run lost its whole checkout. The disk was full, so the
`mktemp -d` in harness/rehearse-held-push.sh failed and printed nothing. The
script ran without `set -e`, and its next line, `ROOT="$(cd "$ROOT" && pwd -P)"`,
turned the empty value into the directory the script was started in: `cd ""`
succeeds in bash without moving. Its EXIT trap then ran `rm -rf "$ROOT"`.
pytest starts these instruments from the checkout, so the checkout was the
directory removed. Started from a home directory, it would have removed that.

harness/scratch-dir.sh is the one place a harness script makes and removes such
a directory now. What is pinned here:

1. Both instruments that had the defect stop at once, with nothing removed,
   when mktemp fails. The directory they were started in is a throwaway one
   here, so the test is safe to run against the code before the fix, where it
   fails because that directory is gone.
2. scratch_dir stops the script unless mktemp really made a directory that is
   not the current one.
3. scratch_remove refuses a path that is empty, relative, the filesystem root,
   or that is or holds the home directory, the current directory, the
   directory the script was started in or the checkout the helper is in. Those
   refusals run against a stand-in `rm` that only records its arguments, so a
   broken guard cannot remove anything from this test either. The root and the
   real checkout are asked of scratch_refusal, which only prints.
4. The other scripts whose unchecked `mktemp -d` fed a removal, a `pkill -f` or
   an export root stop there too. An empty value pointed them at the filesystem
   root: `rm -rf /log`, `pkill -f "/bin/claude"`, an export into `/root`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "harness" / "scratch-dir.sh"
INSTRUMENTS = ("rehearse-held-push.sh", "rehearse-debounce-recipient.sh")
# (script, arguments, a uname that says Linux, which mktemp calls fail). The last is
# None for every call, with nothing else on PATH but dirname: the code before the
# fix went on with an empty variable, and without rm, pkill, mkdir or tar it can do
# nothing with one. ab-comms-eval.sh keeps the whole PATH and loses only its own two
# directories: it sources lib-common.sh first, which makes one of its own, and it ran
# under set -e, so the code before the fix stopped as well, without saying why.
ALSO_STOP = [
    ("harness/rehearse-env-cascade.sh", (), False, None),
    ("harness/rehearse-plane-durability.sh", (), True, None),
    ("harness/ab-comms-eval.sh", ("--dry-run",), False, "ab-comms-eval"),
    ("tests/test_alert_recipient.sh", (), False, None),
    ("tests/test_err_trap_errtrace.sh", (), False, None),
    ("library/skills/gitguardian/test.sh", (), False, None),
]
FULL_DISK = "mktemp: mkdtemp failed on /full/tmp.XXXXXXXXXX: No space left on device"


def _stub(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(exist_ok=True)
    stub = directory / name
    stub.write_text("#!/bin/bash\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


def _path(*stub_dirs: Path) -> str:
    return os.pathsep.join([*(str(d) for d in stub_dirs), os.environ["PATH"]])


@pytest.fixture
def failing_mktemp(tmp_path: Path) -> Path:
    """mktemp as a full disk leaves it: the reason on stderr, nothing on stdout, rc 1.
    With MKTEMP_FAILS_FOR set, only a call naming that text fails; the rest are real."""
    return _stub(
        tmp_path / "mktemp-fails",
        "mktemp",
        'if [ -n "${MKTEMP_FAILS_FOR:-}" ]; then\n'
        '    case "$*" in\n'
        '        *"$MKTEMP_FAILS_FOR"*) ;;\n'
        f'        *) exec {shutil.which("mktemp")} "$@" ;;\n'
        "    esac\n"
        "fi\n"
        f"echo '{FULL_DISK}' >&2\nexit 1\n",
    )


@pytest.fixture
def recording_rm(tmp_path: Path) -> Path:
    """An rm that removes nothing and writes down what it was asked to remove."""
    return _stub(tmp_path / "rm-records", "rm", 'printf \'%s\\n\' "$@" >> "$RM_LOG"\n')


def _probe(
    tmp_path: Path, body: str, *, cwd: Path, helper: Path = HELPER, **env
) -> subprocess.CompletedProcess:
    """Source the helper as the instruments do (set -u, no -e) and run `body`."""
    probe = tmp_path / "probe.sh"
    probe.write_text(f'set -uo pipefail\n. "{helper}"\n{body}\n')
    return subprocess.run(
        ["bash", str(probe)],
        cwd=cwd,
        env=constructed_env(**env),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="the instruments exit before making a directory when tmux is absent",
)
@pytest.mark.parametrize("name", INSTRUMENTS)
def test_an_instrument_that_cannot_make_its_root_removes_nothing(
    name, tmp_path, failing_mktemp
):
    started_in = tmp_path / "started-in"
    started_in.mkdir()
    (started_in / "kept.txt").write_text("kept\n")
    result = subprocess.run(
        ["bash", str(REPO / "harness" / name)],
        cwd=started_in,
        env=constructed_env(PATH=_path(failing_mktemp)),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (started_in / "kept.txt").is_file(), (
        f"{name} removed the directory it was started in\n{result.stdout}{result.stderr}"
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "No space left on device" in result.stderr
    assert "Nothing was removed" in result.stderr
    assert sorted(p.name for p in started_in.iterdir()) == ["kept.txt"], (
        "the instrument went on to work in the directory it was started in"
    )


@pytest.mark.parametrize(
    "script, arguments, says_linux, fails_for", ALSO_STOP, ids=[c[0] for c in ALSO_STOP]
)
def test_a_script_that_cannot_make_its_temporary_directory_stops_there(
    script, arguments, says_linux, fails_for, tmp_path, failing_mktemp
):
    stand_ins = failing_mktemp
    (stand_ins / "dirname").symlink_to(shutil.which("dirname"))
    if says_linux:
        _stub(stand_ins, "uname", "echo Linux\n")
    started_in, under_test = tmp_path / "started-in", tmp_path / "checkout-under-test"
    started_in.mkdir()
    under_test.mkdir()
    (started_in / "kept.txt").write_text("kept\n")
    env = (
        constructed_env(PATH=_path(stand_ins), MKTEMP_FAILS_FOR=fails_for)
        if fails_for
        else constructed_env(PATH=stand_ins, CLAUDLOBBY_ROOT=under_test)
    )
    result = subprocess.run(
        [shutil.which("bash"), str(REPO / script), *arguments],
        cwd=started_in,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "No space left on device" in result.stderr
    assert "Nothing was removed" in result.stderr
    assert sorted(p.name for p in started_in.iterdir()) == ["kept.txt"]
    assert not any(under_test.iterdir())


class TestScratchDir:
    def test_it_names_a_new_physical_directory(self, tmp_path):
        here = tmp_path / "here"
        here.mkdir()
        result = _probe(
            tmp_path,
            "scratch_dir MADE\nprintf '%s\\n' \"$MADE\"\n"
            '(cd "$MADE" && pwd -P)\nrmdir "$MADE"',
            cwd=here,
        )
        assert result.returncode == 0, result.stderr
        made, physical = result.stdout.splitlines()
        assert made == physical and made.startswith("/")
        assert Path(made) != here.resolve() and not Path(made).exists()

    def test_it_passes_a_template_to_mktemp(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        result = _probe(
            tmp_path,
            f'scratch_dir MADE "{base}/named.XXXXXX"\nprintf \'%s\\n\' "$MADE"',
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        made = Path(result.stdout.strip())
        assert (
            made.parent == base.resolve()
            and made.name.startswith("named.")
            and made.is_dir()
        )

    def test_a_failed_mktemp_stops_the_script(self, tmp_path, failing_mktemp):
        result = _probe(
            tmp_path,
            "scratch_dir MADE\necho REACHED",
            cwd=tmp_path,
            PATH=_path(failing_mktemp),
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "REACHED" not in result.stdout
        assert (
            "No space left on device" in result.stderr
            and "Nothing was removed" in result.stderr
        )

    @pytest.mark.parametrize(
        "printed, why",
        [
            ("", "mktemp -d made no directory"),
            ("/nonexistent/claudlobby-scratch-dir-test", "mktemp -d made no directory"),
            (".", "the current directory"),
        ],
        ids=["nothing", "a-missing-path", "the-current-directory"],
    )
    def test_a_mktemp_that_exits_zero_without_a_new_directory_stops_the_script(
        self, tmp_path, printed, why
    ):
        """Exit status alone is not trusted: the value is what gets removed later."""
        lying = _stub(tmp_path / "mktemp-lies", "mktemp", f"echo '{printed}'\nexit 0\n")
        here = tmp_path / "here"
        here.mkdir()
        result = _probe(
            tmp_path, "scratch_dir MADE\necho REACHED", cwd=here, PATH=_path(lying)
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "REACHED" not in result.stdout
        assert why in result.stderr and "Nothing was removed" in result.stderr


class TestScratchRemove:
    @pytest.fixture
    def places(self, tmp_path):
        """A current directory with a child, a home, and a link to the current directory."""
        here, home = tmp_path / "here", tmp_path / "home"
        (here / "child").mkdir(parents=True)
        home.mkdir()
        (tmp_path / "link-to-here").symlink_to(here)
        return here, home

    @pytest.mark.parametrize(
        "target, why",
        [
            ("", "the path is empty"),
            ("child", "the path is relative"),
            (".", "the path is relative"),
            ("{here}", "the current directory"),
            ("{here}/", "the current directory"),
            ("{tmp}/link-to-here", "the current directory"),
            ("{tmp}", "the current directory"),
            ("{home}", "the home directory"),
        ],
        ids=[
            "empty",
            "relative",
            "dot",
            "current",
            "current-with-a-slash",
            "link-to-current",
            "holds-current-and-home",
            "home",
        ],
    )
    def test_it_refuses(self, tmp_path, places, recording_rm, target, why):
        here, home = places
        target = target.format(here=here, home=home, tmp=tmp_path)
        log = tmp_path / "rm.log"
        result = _probe(
            tmp_path,
            'scratch_remove "$TARGET"\necho "rc=$?"',
            cwd=here,
            PATH=_path(recording_rm),
            HOME=home,
            RM_LOG=log,
            TARGET=target,
        )
        assert "rc=1" in result.stdout, result.stdout + result.stderr
        assert "refusing to remove" in result.stderr and why in result.stderr
        assert not log.exists(), f"rm was run: {log.read_text()}"

    def test_it_refuses_the_directory_the_script_was_started_in(
        self, tmp_path, places, recording_rm
    ):
        """The current directory is read when the helper is sourced, so a later cd hides nothing."""
        here, home = places
        log = tmp_path / "rm.log"
        result = _probe(
            tmp_path,
            f'cd "{here}/child"\nscratch_remove "{here}"\necho "rc=$?"',
            cwd=here,
            PATH=_path(recording_rm),
            HOME=home,
            RM_LOG=log,
        )
        assert "rc=1" in result.stdout, result.stdout + result.stderr
        assert (
            "the current directory" in result.stderr
            or "was started in" in result.stderr
        )
        assert not log.exists()
        result = _probe(
            tmp_path,
            f'cd "{home}"\nscratch_remove "{here}"\necho "rc=$?"',
            cwd=here,
            PATH=_path(recording_rm),
            HOME=tmp_path / "absent",
            RM_LOG=log,
        )
        assert "rc=1" in result.stdout, result.stdout + result.stderr
        assert "was started in" in result.stderr
        assert not log.exists()

    def test_the_filesystem_root_is_refused(self, tmp_path):
        """Asked of scratch_refusal, which only prints: nothing here may reach rm."""
        result = _probe(tmp_path, "scratch_refusal /\nscratch_refusal //", cwd=tmp_path)
        assert result.stdout.splitlines() == ["it is the filesystem root"] * 2, (
            result.stdout
        )

    def test_it_removes_a_directory_scratch_dir_made_and_passes_over_an_absent_one(
        self, tmp_path, places
    ):
        here, home = places
        result = _probe(
            tmp_path,
            f'scratch_dir MADE "{tmp_path}/made.XXXXXX"\ntouch "$MADE/file"\n'
            f'scratch_remove "$MADE" "{tmp_path}/absent"\necho "rc=$?"\n'
            '[ -e "$MADE" ] && echo STILL_THERE',
            cwd=here,
            HOME=home,
        )
        assert "rc=0" in result.stdout and "STILL_THERE" not in result.stdout, (
            result.stdout + result.stderr
        )
        assert result.stderr == ""
        assert here.is_dir() and home.is_dir()

    def test_a_refusal_does_not_keep_the_other_paths(self, tmp_path, places):
        here, home = places
        mine = tmp_path / "mine"
        mine.mkdir()
        result = _probe(
            tmp_path, f'scratch_remove "" "{mine}"\necho "rc=$?"', cwd=here, HOME=home
        )
        assert "rc=1" in result.stdout and not mine.exists(), (
            result.stdout + result.stderr
        )
        assert here.is_dir() and home.is_dir()

    def test_it_refuses_the_checkout_its_own_file_is_in(self, tmp_path, recording_rm):
        """Against a copy of the helper in a stand-in checkout: this test never
        hands the real checkout to scratch_remove."""
        checkout = tmp_path / "holder" / "checkout"
        (checkout / "harness").mkdir(parents=True)
        copy = checkout / "harness" / "scratch-dir.sh"
        shutil.copy(HELPER, copy)
        elsewhere, log = tmp_path / "elsewhere", tmp_path / "rm.log"
        elsewhere.mkdir()
        for target in (checkout, tmp_path / "holder", checkout / "harness" / ".."):
            result = _probe(
                tmp_path,
                f'scratch_remove "{target}"\necho "rc=$?"',
                cwd=elsewhere,
                helper=copy,
                PATH=_path(recording_rm),
                HOME=tmp_path / "absent",
                RM_LOG=log,
            )
            assert "rc=1" in result.stdout, result.stdout + result.stderr
            assert "the checkout this helper is in" in result.stderr
            assert not log.exists(), f"rm was run: {log.read_text()}"
        result = _probe(
            tmp_path,
            f'scratch_remove "{checkout}/harness"\necho "rc=$?"',
            cwd=elsewhere,
            helper=copy,
            PATH=_path(recording_rm),
            HOME=tmp_path / "absent",
            RM_LOG=log,
        )
        assert "rc=0" in result.stdout and log.exists(), (
            "a directory inside the checkout is not the checkout"
        )

    def test_the_real_checkout_is_refused(self, tmp_path):
        """Asked of scratch_refusal, which only prints. The reason is pinned by
        the test above; here a test directory inside the checkout may come first."""
        result = _probe(
            tmp_path,
            f'scratch_refusal "{REPO}"\nscratch_refusal "{REPO.parent}"',
            cwd=tmp_path,
            HOME=tmp_path / "absent",
        )
        lines = result.stdout.splitlines()
        assert len(lines) == 2 and all(
            line.startswith("it is or holds") for line in lines
        ), result.stdout + result.stderr
