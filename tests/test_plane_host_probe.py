"""Host facet probe (chunk 3) — the cause=probe emitter for host.* metrics.

Drives the REAL lib/plane-host-probe.sh (real lib-common, real shim, real
cold-CLI ingest into a scratch db; the facet tools stubbed on PATH so the
values are deterministic). Load-bearing laws: subject_kind=host keyed by
hostname (joins the Host card); Pi-only facets are ABSENT on a non-Pi host,
never a fabricated 0; the job_ran proof-of-run always lands; dormant
without arming; every path exits 0.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from claudlobby.plane.db import db_path

REPO = Path(__file__).resolve().parent.parent
CLI = Path(sys.executable).parent / "claudlobby"


def _rig(tmp_path, *, pi=False, armed=True):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    stub = tmp_path / "bin"
    stub.mkdir()
    # deterministic facet tools on PATH ahead of the real ones
    (stub / "uptime").write_text(
        '#!/bin/bash\necho " 12:00  up 3 days,  load average: 0.50, 0.40, 0.30"\n')
    (stub / "df").write_text(
        '#!/bin/bash\n'
        'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
        'echo "/dev/disk1 1000000000 500000000 471859200 51% /"\n')  # ~450G
    (stub / "hostname").write_text('#!/bin/bash\necho probe-host\n')
    # macOS boottime shape — the string HOLDS `usec = ...`, the SEV-1 trap
    (stub / "sysctl").write_text(
        '#!/bin/bash\ncase "$*" in *kern.boottime*)'
        ' echo "{ sec = 1786878506, usec = 60460 } Sun Aug 16 2026" ;;'
        ' *) exit 1 ;; esac\n')
    # no uptime -s on macOS (errors), no /proc/stat — force the sysctl path
    (stub / "uptime").write_text(
        '#!/bin/bash\ncase "$*" in *-s*) exit 1 ;;'
        ' *) echo " 12:00  up 3 days,  load averages: 0.50 0.40 0.30" ;;'
        ' esac\n')
    if pi:
        (stub / "vcgencmd").write_text(
            '#!/bin/bash\necho "throttled=0x50005"\n')  # uv NOW + occurred
    for f in stub.iterdir():
        f.chmod(0o755)
    env = {
        "CLAUDLOBBY_ROOT": str(root),
        "HOME": str(tmp_path),
        "PLANE_EMIT_CLI": str(CLI),
        "PLANE_SOCKET": str(tmp_path / "no.sock"),
        "FLEET_NAME": "_host",
        "PATH": f"{stub}:/usr/bin:/bin",
    }
    if armed:
        env["PLANE_EMIT_ENABLED"] = "1"
    return root, env


def _run(root, env):
    return subprocess.run(
        ["bash", str(REPO / "lib" / "plane-host-probe.sh")],
        capture_output=True, text=True, env=env, timeout=120)


def _samples(root):
    db = db_path(root)
    if not db.is_file():
        return {}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["metric"]: r for r in conn.execute(
            "SELECT metric, subject_kind, subject_uid, value FROM"
            " metric_samples")}
    finally:
        conn.close()


def test_probe_emits_the_portable_facets(tmp_path):
    root, env = _rig(tmp_path)
    r = _run(root, env)
    assert r.returncode == 0, r.stderr
    s = _samples(root)
    assert json.loads(s["host.load"]["value"]) == {
        "one": 0.5, "five": 0.4, "fifteen": 0.3}
    assert int(s["host.disk_free_gb"]["value"]) == 450
    assert "host.mem_available_mb" in s        # real reader, value host-dependent
    assert s["host.job_ran"]["value"] in ("1", 1)
    assert s["host.load"]["subject_kind"] == "host"
    assert s["host.load"]["subject_uid"].startswith("host_")


def test_pi_facets_present_only_on_a_pi(tmp_path):
    root, env = _rig(tmp_path, pi=True)
    assert _run(root, env).returncode == 0
    s = _samples(root)
    assert s["host.thermal_flags"]["value"].strip('"') == "0x50005"
    assert json.loads(s["host.undervoltage"]["value"]) is True   # bit0 set


def test_no_pi_facets_are_fabricated_off_a_pi(tmp_path):
    root, env = _rig(tmp_path, pi=False)   # no vcgencmd on PATH
    assert _run(root, env).returncode == 0
    s = _samples(root)
    assert "host.thermal_flags" not in s   # absent, never a fabricated 0
    assert "host.undervoltage" not in s


def test_dormant_without_arming(tmp_path):
    root, env = _rig(tmp_path, armed=False)
    r = _run(root, env)
    assert r.returncode == 0
    assert not db_path(root).is_file()


def test_probe_job_composes_dormant_and_arms_on_the_emit_flag(tmp_path,
                                                              monkeypatch):
    from claudlobby.composer import compose_host_timers
    from claudlobby.paths import Paths
    from claudlobby.env_tiers import Resolution
    import claudlobby.env_tiers as et

    root = tmp_path / "r"
    (root / "claudlobby").mkdir(parents=True)
    (root / "claudlobby" / "system.yaml").write_text(
        "host:\n  jobs:\n"
        "    plane-host-probe:\n      enroll: false\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/plane-host-probe.sh\"\n"
        "      interval: 60\n      type: oneshot\n")
    monkeypatch.setattr(et, "read_tiers",
                        lambda paths, bot_name=None, fleet_name=None: [])
    monkeypatch.setattr(et, "cascade", lambda tiers: {
        "PLANE_EMIT_ENABLED": Resolution(
            name="PLANE_EMIT_ENABLED", value="1", tier="host", path=None)})
    out = compose_host_timers(Paths(root=root))
    svc = (out / "claudlobby-plane-host-probe.service").read_text()
    assert "Environment=PLANE_EMIT_ENABLED=1" in svc


def test_launcher_parses_and_references():
    body = (REPO / "lib" / "plane-host-probe.sh").read_text()
    assert "plane_armed plane-host-probe" in body
    r = subprocess.run(["bash", "-n", str(REPO / "lib" / "plane-host-probe.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_boot_time_is_the_real_instant_not_1970_and_utc(tmp_path):
    """r-gauntlet SEV-1 (proven live on macOS): the greedy sed matched
    `usec` inside kern.boottime, recording a 1970 boot every minute on
    every Mac. The first-digit-run extraction gets the real sec; UTC+Z."""
    root, env = _rig(tmp_path)
    assert _run(root, env).returncode == 0
    bt = _samples(root)["host.boot_time"]["value"].strip('"')
    assert bt == "2026-08-16T11:08:26Z"      # real instant, not 1970, +Z


def test_comma_decimal_locale_load_is_dropped_not_corrupted(tmp_path):
    """r-gauntlet SEV-2: a comma-decimal locale (0,52) would split a
    decimal comma into a bogus separator and record silently-wrong
    numbers. Each token is validated as a bare decimal; a comma-decimal
    line is DROPPED, never mis-recorded."""
    root, env = _rig(tmp_path)
    import os
    # override uptime to emit the comma-decimal shape (LC_ALL=C in the
    # script normalizes real locales, but a pre-formatted comma line still
    # must fail the per-token decimal check rather than corrupt)
    (tmp_path / "bin" / "uptime").write_text(
        '#!/bin/bash\ncase "$*" in *-s*) exit 1 ;;'
        ' *) echo " up  load average: 0,52, 0,58, 0,59" ;; esac\n')
    (tmp_path / "bin" / "uptime").chmod(0o755)
    assert _run(root, env).returncode == 0
    s = _samples(root)
    assert "host.load" not in s              # dropped, not {"one":0,...}
    assert "host.job_ran" in s               # the rest of the batch survives


def test_empty_hostname_never_poisons_the_whole_batch(tmp_path):
    """r-gauntlet SEV-3: an empty subject fails min_length=1 and the CLI
    rejects the ENTIRE batch — job_ran included, the edge it exists for.
    A fallback host keeps the batch valid."""
    root, env = _rig(tmp_path)
    (tmp_path / "bin" / "hostname").write_text('#!/bin/bash\nexit 1\n')
    (tmp_path / "bin" / "hostname").chmod(0o755)
    (tmp_path / "bin" / "uname").write_text('#!/bin/bash\necho\n')  # empty -n
    (tmp_path / "bin" / "uname").chmod(0o755)
    assert _run(root, env).returncode == 0
    s = _samples(root)
    assert s["host.job_ran"]["subject_uid"].startswith("host_")  # batch landed
