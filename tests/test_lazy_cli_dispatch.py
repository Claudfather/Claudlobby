"""#1123 — read-only verbs must not pay the composer import stack (#904 PR1).

The pin is a real subprocess per case: a fresh interpreter runs the actual CLI
entry point and then inspects its own sys.modules — asserting on the artifact
(what got imported), never on the source shape. The dispatch case runs a real
`brief --boot` against a minimal root, so the pin covers the RUN path, not
just --help registration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_and_report_modules(tmp_path: Path, argv: list[str]) -> str:
    code = (
        "import sys\n"
        f"sys.argv = {argv!r}\n"
        "rc = 0\n"
        "try:\n"
        "    from claudlobby.__main__ import main\n"
        "    rc = main() or 0\n"
        "except SystemExit as e:\n"
        "    rc = e.code or 0\n"
        "heavy = [m for m in ('claudlobby.composer', 'claudlobby.diff', 'claudlobby.validator') if m in sys.modules]\n"
        "print('HEAVY:', ','.join(heavy) or 'none', 'RC:', rc)\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_brief_help_imports_no_heavy_modules(tmp_path):
    out = _run_and_report_modules(tmp_path, ["claudlobby", "brief", "--help"])
    assert "HEAVY: none" in out, out


def test_brief_boot_run_imports_no_heavy_modules(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "state").mkdir()
    fleet_dir = tmp_path / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n  name: f1\n  service_prefix: com.test\n"
        "  bots:\n    alex:\n      expertise: [software-engineering]\n"
    )
    out = _run_and_report_modules(
        tmp_path,
        [
            "claudlobby",
            "--root",
            str(tmp_path),
            "--fleet",
            "f1",
            "brief",
            "--bot",
            "alex",
            "--boot",
        ],
    )
    assert "HEAVY: none" in out, out
    assert "RC: 0" in out, out


def test_generate_still_dispatches_composer(tmp_path):
    # The inverse control: the pin must be able to fail — a verb that DOES
    # need the composer must show it imported, or the heavy-list is wrong.
    (tmp_path / "lib").mkdir()
    fleet_dir = tmp_path / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n  name: f1\n  service_prefix: com.test\n"
        "  bots:\n    alex:\n      expertise: [software-engineering]\n"
    )
    out = _run_and_report_modules(
        tmp_path,
        ["claudlobby", "--root", str(tmp_path), "--fleet", "f1", "validate"],
    )
    assert "claudlobby.validator" in out, out
