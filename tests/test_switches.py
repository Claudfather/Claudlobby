"""The switch registry and the surfaces that render it (chunk N).

The defaults flip is only half a change. The other half — and the half these
tests are mostly about — is that whatever stays off has to be VISIBLE, with the
one line that flips it, at the three places an operator actually looks:
`claudlobby doctor`, `claudlobby plane doctor`, and the end of a setup run.

So the assertions here are deliberately about the SURFACE, not just the data:
a registry nobody renders is the same opacity in a tidier shape.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import switches as sw
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent

_FLEET = """\
    fleet:
      name: sw-fleet
      service_prefix: com.sw
      bots:
        kev:
          expertise: [software-engineering]
"""


def _root(tmp_path: Path, env: str | None = None) -> Path:
    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    # The REAL resolver, not a stub: the whole reason env_tiers shells out is
    # that a Python copy of the cascade drifts from the runtime (#1226), and a
    # test that stubs it certifies the copy instead of the contract.
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    (root / "fleet.yaml").write_text(dedent(_FLEET))
    if env is not None:
        (root / ".env").write_text(env)
    return root


def _resolve(tmp_path: Path, env: str | None = None):
    root = _root(tmp_path, env)
    fleet, _md = load_fleet(root / "fleet.yaml")
    return sw.resolve(Paths(root=root, fleet_dir=root), fleet)


def _state(rows, key):
    return next(r for r in rows if r.switch.key == key)


# ---------------------------------------------------------------------------
# the rule, as shipped
# ---------------------------------------------------------------------------


def test_exactly_the_four_categories_ship_off():
    """The rule has four reasons and no fifth. Every opt-in must NAME which one
    keeps it off — an unexplained off switch is how a default quietly becomes
    a habit, which is the state this chunk found the estate in."""
    opt_in = {s.key for s in sw.SWITCHES if s.polarity == sw.OPT_IN}
    assert opt_in == {"update-siblings", "session-digest", "code-audit-sweep",
                      "weekly-worker-restart"}
    for s in sw.SWITCHES:
        if s.polarity == sw.OPT_IN:
            assert s.why_opt_in, f"{s.key} ships off with no stated reason"
        else:
            assert not s.why_opt_in


def test_every_switch_carries_both_directions():
    """A knob you can only turn one way is not a knob. Every row states the arm
    AND the disarm line, because the reader who needs the table is equally
    likely to be turning something off."""
    for s in sw.SWITCHES:
        assert s.arm and s.disarm and s.what, s.key
        assert s.env or s.job or s.scope == sw.FLEET_JOB, s.key


def test_the_target_workflow_doors_are_the_reaction_chain():
    """Scoped narrowly on purpose: status's header names these and nothing
    else. A header that listed every off switch would be scrolled past, which
    is the same failure as printing nothing."""
    assert {s.key for s in sw.SWITCHES if s.target_workflow} == {
        "task-recheck", "plane-expire", "plane-recording"}


def test_a_fresh_fleet_has_everything_but_the_opt_ins_on(tmp_path):
    rows = _resolve(tmp_path)
    on = {r.switch.key for r in rows if r.on}
    off = {r.switch.key for r in rows if not r.on}
    assert off == {s.key for s in sw.SWITCHES if s.polarity == sw.OPT_IN}
    assert {"task-recheck", "plane-expire", "plane-prune", "plane-daemon",
            "plane-view", "plane-host-probe", "registry-scan",
            "spindown-receipt", "plane-recording"} <= on


def test_the_shipped_tier_agrees_with_the_registry():
    """The registry and system.yaml are two spellings of one decision, and the
    estate's recurring defect is two spellings drifting (#892/#1143). Pinned
    together so a future `enroll: false` in the yaml with no registry row — or
    the reverse — fails here rather than on a host."""
    from claudlobby.config import load_host_jobs

    host = load_host_jobs()
    for s in sw.SWITCHES:
        if not s.job or s.scope not in (sw.HOST_JOB, sw.HOST_SERVICE):
            continue
        cfg = host[s.job]
        if s.scope == sw.HOST_SERVICE:
            enrolled = cfg.get("enroll") is True
        else:
            enrolled = cfg.get("enroll", True) is not False
        assert enrolled is s.default_on, (
            f"{s.job}: system.yaml says enrolled={enrolled} while the registry"
            f" says default_on={s.default_on}")


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_a_fleet_env_that_turns_the_recheck_off_is_visible(tmp_path):
    """The pin the ruling asks for by name: a fleet with a `.env` turning
    task-recheck off SHOWS it, and names the tier that did."""
    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    st = _state(rows, "task-recheck")
    assert st.on is False
    assert st.label == "off"
    assert "root .env" in st.source or ".env" in st.source
    assert sw.target_workflow_off(rows) == [st]
    assert "task-recheck" in sw.summary_line(rows)


def test_an_empty_assignment_is_not_an_off_switch(tmp_path):
    """`export FLAG=` wins at its tier (#1213) and is not a "0". The bash gate
    reads it the same way (`${FLAG:-1}` is empty, which is not 0), so the table
    and the door agree — which is the only thing that makes the table worth
    reading."""
    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=\n")
    assert _state(rows, "task-recheck").on is True


def test_the_silencer_reads_inverted(tmp_path):
    rows = _resolve(tmp_path, "PLANE_EMIT_DISABLED=1\n")
    assert _state(rows, "plane-recording").on is False
    assert _state(rows, "plane-recording") in sw.target_workflow_off(rows)


def test_an_opt_in_nobody_armed_reads_as_the_DEFAULT_not_as_config(tmp_path):
    """Attribution answers "who decided this". An opt-in sitting off because
    nobody armed it was never turned off by anything — saying "fleet.yaml" there
    sends a reader to edit a file that has nothing to say."""
    rows = _resolve(tmp_path)
    assert _state(rows, "code-audit-sweep").source == "default"
    assert _state(rows, "weekly-worker-restart").source == "default"


def test_an_armed_opt_in_names_what_armed_it(tmp_path):
    rows = _resolve(tmp_path, "SESSION_DIGEST_ENABLED=1\n")
    st = _state(rows, "session-digest")
    assert st.on is True and ".env" in st.source


def test_an_unreachable_resolver_says_so_rather_than_guessing(tmp_path,
                                                             monkeypatch):
    """Unreachable is not empty (source_state's rule). A table that quietly
    printed the shipped defaults as if it had measured them would be a worse
    lie than the dormancy it replaced."""
    import claudlobby.env_tiers as et

    def boom(*a, **k):
        raise et.ResolverUnavailable("no resolver")

    monkeypatch.setattr(et, "read_tiers", boom)
    rows = _resolve(tmp_path)
    st = _state(rows, "task-recheck")
    assert st.unknown and st.label == "unknown"
    assert "resolver unreachable" in st.detail
    assert "resolver unreachable" in sw.summary_line(rows)
    # ...and an UNKNOWN state never reaches status's header: naming a door as
    # off when we could not read it is the fabrication this rule exists to stop.
    assert st not in sw.target_workflow_off(rows)


# ---------------------------------------------------------------------------
# the surfaces
# ---------------------------------------------------------------------------


def test_the_table_leads_with_the_opt_ins(tmp_path):
    """Ordering is the surface. Three off switches buried under nine on ones
    have been named without being shown."""
    rows = _resolve(tmp_path)
    text = sw.format_table(rows)
    assert text.index("opt-in — ships OFF") < text.index("on by default")
    for s in sw.SWITCHES:
        if s.polarity == sw.OPT_IN:
            assert s.key in text and s.arm in text
            assert s.why_opt_in in text


def _cli(root: Path, *argv):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root), *argv],
        capture_output=True, text=True, timeout=180, cwd=str(REPO))


def test_doctor_switches_prints_the_table_alone(tmp_path):
    r = _cli(_root(tmp_path, "TASK_RECHECK_ENABLED=0\n"), "doctor", "--switches")
    assert r.returncode == 0, r.stderr
    assert "=== switches ===" in r.stdout
    assert "task-recheck" in r.stdout and "off" in r.stdout
    # A row shows the line the reader needs FROM WHERE IT IS: an off door
    # prints how to get it back, an on door prints how to stop it. Printing
    # both would double the table and halve the chance either is read.
    assert "arm: unset TASK_RECHECK_ENABLED" in r.stdout
    assert "turn off: PLANE_EXPIRE_ENABLED=0" in r.stdout
    assert "update-siblings" in r.stdout          # the opt-ins are always listed
    assert "session-digest" in r.stdout
    assert "code-audit-sweep" in r.stdout
    # ...and ONLY the table: the shell setup doors call this, and a health
    # command's service probes / credential curls have no business in a
    # setup summary.
    assert "npx" not in r.stdout and "=== claudlobby doctor ===" not in r.stdout


def test_the_doctor_rung_exists_and_never_fails(tmp_path):
    """Reported, never flagged. A fleet that turned the re-check off did so on
    purpose; warning about it would train operators to ignore the rung, and a
    surface people ignore is not a surface."""
    from claudlobby.doctor import DoctorReport, check_switches

    root = _root(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    fleet, _md = load_fleet(root / "fleet.yaml")
    report = DoctorReport()
    check_switches(fleet, Paths(root=root, fleet_dir=root), report)
    rung = next(c for c in report.checks if c.name == "switches")
    assert rung.status == "pass"
    assert "task-recheck" in rung.detail
    assert report.switch_rows


def test_status_header_names_a_disabled_reaction(tmp_path):
    from claudlobby.status import switches_off_note

    rows = _resolve(tmp_path, "TASK_RECHECK_ENABLED=0\n")
    note = switches_off_note("artemis-data", rows)
    assert "task-recheck off on artemis-data" in note
    assert "doctor --switches" in note
    assert switches_off_note("artemis-data", _resolve(tmp_path / "b")) == ""


@pytest.mark.parametrize("door", ["lib/setup-fleet", "lib/setup-system"])
def test_both_setup_doors_end_by_printing_the_table(door):
    """One renderer, called by both — never a bash copy. A second table in
    shell is how the printed truth and the actual truth drift."""
    body = (REPO / door).read_text()
    assert "doctor --switches" in body


# ---------------------------------------------------------------------------
# the composed side
# ---------------------------------------------------------------------------


def test_host_timer_dormancy_is_ENFORCED_now(tmp_path):
    """#1385, closed as a consequence of the flip. `enroll: false` on a host
    TIMER had no code effect: compose_host_timers read `enroll` only in the
    service branch and setup-system enrolled every composed unit it found — so
    update-siblings, the one host job that MUTATES OPERATOR SOURCE, was
    enrolled on every host that ran the installer.

    Survivable while most doors shipped dormant; not survivable under a rule
    that ships doors on, because "stays opt-in" becomes the only thing between
    a root pull and the four categories."""
    from claudlobby.composer import compose_host_timers

    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    manifest = [ln for ln in (out / "DORMANT").read_text().splitlines()
                if ln and not ln.startswith("#")]
    assert manifest == ["claudlobby-update-siblings"]
    # Composed-but-dormant: the units still exist, only enrollment is withheld.
    assert (out / "claudlobby-update-siblings.timer").is_file()
    # ...and setup-system reads that manifest through the SHARED predicate the
    # fleet door already uses, rather than a second copy that can disagree.
    setup = (REPO / "lib" / "setup-system").read_text()
    assert 'unit_is_dormant "$host_timers_dir"' in setup


def test_the_plane_services_compose_by_default(tmp_path):
    from claudlobby.composer import compose_host_timers

    root = tmp_path / "h"
    root.mkdir(parents=True)
    out = compose_host_timers(Paths(root=root))
    names = {p.name for p in out.iterdir()}
    for job in ("plane-daemon", "plane-view"):
        assert f"claudlobby-{job}.service" in names
        assert f"claudlobby-{job}.plist" in names
        assert f"claudlobby-{job}.timer" not in names   # resident, not timed
    for job in ("plane-expire", "plane-prune", "plane-host-probe"):
        assert f"claudlobby-{job}.timer" in names


def test_the_composer_arming_tables_are_derived_not_listed():
    """The registry is the one place a knob's spelling lives. Four copies of
    "what knobs exist" is how the estate ended up with a `.env` full of flags
    nothing read."""
    from claudlobby.composer import FLEET_JOB_ARMING, HOST_JOB_ARMING

    assert FLEET_JOB_ARMING == sw.jobs_with_env(sw.FLEET_JOB)
    assert HOST_JOB_ARMING == sw.jobs_with_env(sw.HOST_JOB, sw.HOST_SERVICE)
    assert set(HOST_JOB_ARMING) == {"plane-expire", "plane-prune"}


def test_the_validator_namespaces_come_from_the_registry():
    """Derived, so a door deleted tomorrow warns without anyone touching the
    validator — and a fleet's own MYTOOL_ENABLED never does."""
    assert sw.namespaces() == {"TASK", "PLANE", "SESSION", "SPINDOWN"}
    assert "PLANE_SHADOW_ENABLED" not in sw.env_names()
    assert "PLANE_SHADOW_ENABLED" in sw.RETIRED


def test_doctor_switches_works_with_NO_fleet_at_all(tmp_path):
    """lib/setup-system runs once per HOST, and a host with overlay fleets has
    no root `fleet.yaml` — so requiring one here would have made the host door
    print nothing, which is the exact silence this chunk exists to remove. The
    host rows are still true without a fleet; the fleet-scoped ones fall back
    to their shipped defaults."""
    root = tmp_path / "hostonly"
    root.mkdir()
    (root / "lib").symlink_to(REPO / "lib")
    r = _cli(root, "doctor", "--switches")
    assert r.returncode == 0, r.stderr
    assert "plane-daemon" in r.stdout and "update-siblings" in r.stdout
