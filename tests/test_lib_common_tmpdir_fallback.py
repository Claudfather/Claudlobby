"""#1682: lib-common.sh's `_LC_TMPDIR` fallback template, and its failure mode.

The fallback (`mktemp -d -t 'lib-common'`) had no trailing X's, which GNU
coreutils mktemp rejects outright ("too few X's in template") while BSD/macOS
tolerates -- so the branch was unreachable in normal conditions (the primary
`mktemp -d` almost always succeeds) and, on the one platform where it could
actually run, could never succeed. Three things pinned here:

1. The fixed template succeeds on GNU coreutils (this host). BSD/macOS is not
   independently verified -- no such host is available here; the fixed
   template is documented as accepted by both, and adding X's is strictly a
   widening of what the old (X-less) template already required.
2. When the primary genuinely fails but a WORKING fallback path exists, the
   fallback actually rescues it. Real OS conditions (a full/read-only /tmp)
   cannot isolate this branch: measured directly (not assumed) that GNU
   coreutils' bare `mktemp -d` and `mktemp -d -t template` both consult the
   same TMPDIR identically, so a TMPDIR-based failure takes out both forms
   together, never one without the other. Reaching the branch where ONLY the
   primary fails needs a stubbed mktemp that discriminates on argv shape.
3. When BOTH attempts genuinely fail, the failure names `_LC_TMPDIR` /
   lib-common.sh rather than surfacing as a bare, context-free mktemp error
   with nothing connecting it back to its real cause -- the actual defect
   #1682 reports: the template bug was silent at the SOURCE, and the symptom
   that reached an operator (`lib/env-tiers.sh` exiting 1) carried no
   indication of what had actually failed or why.
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
LIB_COMMON = REPO / "lib" / "lib-common.sh"
ENV_TIERS = REPO / "lib" / "env-tiers.sh"


def _source_probe(tmp_path: Path, env: dict) -> subprocess.CompletedProcess:
    """Source the real lib-common.sh under a real caller's usual `set -e`
    posture and report what `_LC_TMPDIR` ended up holding and whether it was
    a real, existing directory -- checked FROM WITHIN bash, before the
    script exits and lib-common.sh's own EXIT trap (`_lc_cleanup`) removes
    it, since a caller can't observe the directory afterward."""
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        f'. "{LIB_COMMON}"\n'
        'if [ -d "$_LC_TMPDIR" ]; then _exists=DIR_EXISTS; else _exists=DIR_MISSING; fi\n'
        'printf \'SOURCED _LC_TMPDIR=[%s] %s\\n\' "$_LC_TMPDIR" "$_exists"\n'
    )
    return subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env)


def _parse(stdout: str) -> tuple[str, str]:
    """('SOURCED _LC_TMPDIR=[/tmp/x] DIR_EXISTS\\n') -> ('/tmp/x', 'DIR_EXISTS')"""
    line = stdout.strip()
    path = line.split("[", 1)[1].split("]", 1)[0]
    marker = line.rsplit(" ", 1)[1]
    return path, marker


class TestTheFixedTemplateWorksOnGnu:
    def test_the_new_template_succeeds_where_the_old_one_failed(self, tmp_path: Path):
        """Direct regression pin on the literal defect: the OLD template
        (no X's) fails on GNU with 'too few X's in template'; confirm that
        first (so this test would have failed before the fix), then confirm
        the mechanism that replaced it -- sourcing lib-common.sh with a
        genuinely working primary -- produces a real, existing directory."""
        old = subprocess.run(
            ["mktemp", "-d", "-t", "lib-common"], capture_output=True, text=True
        )
        assert old.returncode != 0
        assert "too few X" in old.stderr

        r = _source_probe(tmp_path, constructed_env())
        assert r.returncode == 0, r.stdout + r.stderr
        tmpdir, exists = _parse(r.stdout)
        assert tmpdir, r.stdout
        assert exists == "DIR_EXISTS", r.stdout


class TestThePrimaryFailingFallsThroughToTheFallback:
    """Real OS conditions can't isolate this branch (measured: bare `-d` and
    `-t template` both honor TMPDIR identically, so a TMPDIR-based failure
    always takes both out together). A stub that discriminates on argv shape
    is the only way to reach "primary fails, fallback alone succeeds" at all."""

    @pytest.fixture
    def stub_bin(self, tmp_path: Path) -> Path:
        real_mktemp = shutil.which("mktemp")
        assert real_mktemp, "no real mktemp on PATH to delegate to"
        stub_dir = tmp_path / "stubbin"
        stub_dir.mkdir()
        stub = stub_dir / "mktemp"
        stub.write_text(
            "#!/bin/bash\n"
            "# Forces the bare/no-template primary to fail; delegates any\n"
            "# call carrying -t (the fallback shape) to the real mktemp.\n"
            'case " $* " in\n'
            "  *' -t '*) exec " + real_mktemp + ' "$@" ;;\n'
            "  *) echo 'mktemp: STUB forcing primary failure' >&2; exit 1 ;;\n"
            "esac\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return stub_dir

    def test_fallback_rescues_when_reachable(self, tmp_path: Path, stub_bin: Path):
        """The primary's own `2>/dev/null` (lib-common.sh's, unchanged by
        this fix) swallows the stub's stderr on that attempt -- correctly,
        since a working fallback means nothing worth surfacing happened.
        What proves the primary genuinely failed and the FALLBACK (not the
        primary) produced the result is the directory name itself: the
        fallback's template (`lib-common.XXXXXXXXXX`) is distinct from the
        primary's own default (`tmp.XXXXXXXXXX`)."""
        env = constructed_env(PATH=f"{stub_bin}:{os.environ['PATH']}")
        r = _source_probe(tmp_path, env)
        assert r.returncode == 0, r.stdout + r.stderr
        tmpdir, exists = _parse(r.stdout)
        assert tmpdir, r.stdout
        assert exists == "DIR_EXISTS", r.stdout
        assert Path(tmpdir).name.startswith("lib-common."), (
            f"expected the FALLBACK's template to have produced this dir, got {tmpdir!r}"
        )


class TestBothAttemptsFailingNamesTheHelper:
    """The actual reported defect: before the fix, this path surfaced as a
    bare, context-free mktemp error -- nothing connecting it back to
    lib-common.sh or `_LC_TMPDIR`. Exercised through the real downstream
    door the issue names, `lib/env-tiers.sh`, not just lib-common.sh alone."""

    @pytest.fixture
    def readonly_tmpdir(self, tmp_path: Path) -> Path:
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        yield ro
        ro.chmod(0o755)  # restore so pytest's own cleanup can remove it

    def test_env_tiers_names_lc_tmpdir_not_a_bare_exit(
        self, tmp_path: Path, readonly_tmpdir: Path
    ):
        env = constructed_env(
            TMPDIR=str(readonly_tmpdir), CLAUDLOBBY_ROOT=str(tmp_path)
        )
        r = subprocess.run(
            ["bash", str(ENV_TIERS)], capture_output=True, text=True, env=env
        )
        assert r.returncode == 1
        assert "_LC_TMPDIR" in r.stderr, r.stderr
        assert "lib-common.sh" in r.stderr, r.stderr

    def test_lib_common_alone_shows_the_same_diagnostic(
        self, tmp_path: Path, readonly_tmpdir: Path
    ):
        env = constructed_env(TMPDIR=str(readonly_tmpdir))
        r = _source_probe(tmp_path, env)
        assert r.returncode == 1
        assert "_LC_TMPDIR" in r.stderr, r.stderr
        assert "SOURCED" not in r.stdout, r.stdout
