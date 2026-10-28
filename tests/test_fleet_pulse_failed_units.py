"""fleet-pulse's failed-units leg (#2532): an owned unit in the failed state is
reported once an episode, a passing one is not, and a host unit is recorded once
a host and paged nowhere.

Each case drives the REAL sweep through the browser leg's harness (the repo's
claudlobby/_runtime_scripts/ linked file by file; plane-emit.sh and tg-post.sh
append what they are given to files). A stand-in CLI answers the sweep's
`host supervision failed` read with the test's own result, so the rows here are
the read's output, not native state: tests/test_failed_units.py covers the read,
and tests/test_supervisor_adapter.sh the native calls under it. The launchd rows
are a test double; no Mac ran this.
"""

from __future__ import annotations

import json

import pytest

from tests.test_fleet_pulse_browsers import Host, pytestmark  # noqa: F401 (pytestmark: its skips apply here)

SYSTEMD = "ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=4"


class UnitsHost(Host):
    """The browser leg's host with fleet F, its escalation chat, and a CLI that answers the read."""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.cli = tmp / "claudlobby"
        self.fleet("F", "com.t", ["b1", "b2"])
        self.reads([])

    def reads(self, units, *, refused=None):
        body = ({"ok": False, "error": {"code": "unavailable", "message": refused}} if refused
                else {"ok": True, "data": {"units": units}})
        self.cli.write_text("#!/bin/bash\ncase \" $* \" in\n    *\" host supervision failed \"*)\n"
                            f"        cat <<'JSON'\n{json.dumps(body)}\nJSON\n        exit {6 if refused else 0} ;;\n"
                            "esac\nexit 0\n")
        self.cli.chmod(0o755)

    def sweep(self, fleet, cli=True, bound=False):
        extra = {"CLAUDLOBBY_CLI": str(self.cli)} if cli else {}
        if bound:  # as `claudlobby fleet pulse` runs it for a selected release
            extra |= {"CLAUDLOBBY_RELEASE_ID": "r-test", "CLAUDLOBBY_PRIVATE_PULSE_RELEASE": "r-test"}
        return super().sweep(fleet, FLEET_PULSE_ESCALATION_CHAT_ID="-1001234567890",
                             FLEET_PULSE_ESCALATION_STATE_DIR=str(self.tmp / "sender"), **extra)

    def recorded(self, kind="unit_failed"):
        rows = []
        for line in self.capture.read_text().splitlines() if self.capture.exists() else []:
            for event in json.loads(line)["events"]:
                if event["payload"]["event"] == kind:
                    rows.append((event["fleet"], event["payload"]["data"]["data"]))
        return rows

    def pages(self):
        return self.tg.read_text().splitlines() if self.tg.exists() else []

    def keys(self, name):
        path = self.root / "state" / "pulse" / name
        return path.read_text().split() if path.exists() else []


@pytest.fixture
def host(tmp_path):
    h = UnitsHost(tmp_path)
    try:
        yield h
    finally:
        h.close()


def unit(name, *, scope="fleet", fleet="F", bot=None, surface="systemd", state="failed", detail=SYSTEMD,
         template=None):
    return {"unit": name, "scope": scope, "fleet": None if scope == "host" else fleet, "bot": bot,
            "surface": surface, "state": state, "detail": detail, "template": template}


def page(name, fleet="F"):
    return f"FLEET ALERT ({fleet}): {name} failed (systemd {SYSTEMD}). Its log: journalctl --user -u {name}"


def test_a_unit_forced_to_fail_is_recorded_and_paged_once(host):
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    host.sweep("F")
    assert host.recorded() == [("F", {"unit": "com.t.creds-check.service", "scope": "fleet",
                                      "surface": "systemd", "detail": SYSTEMD, "template": None})]
    assert host.pages() == [page("com.t.creds-check.service")]
    assert host.keys("F.units_failed") == host.keys("F.units_failed.paged") == ["com.t.creds-check.service"]
    assert f"Failed units: com.t.creds-check.service (fleet, systemd {SYSTEMD});" in host.summary("F")


def test_a_passing_unit_a_bot_unit_and_another_fleets_unit_are_not_reported(host):
    host.fleet("G", "com.u", ["g1", "g2"])
    # the read lists only failed and running units: a passing one is in neither
    host.reads([unit("com.t.b1.service", scope="bot", bot="b1"), unit("com.u.creds-check.service", fleet="G")])
    host.sweep("F")
    assert host.recorded() == [] and host.pages() == [] and host.keys("F.units_failed") == []
    assert "Failed units" not in host.summary("F")
    host.sweep("G")
    assert [data["unit"] for _, data in host.recorded()] == ["com.u.creds-check.service"]
    assert host.pages() == [page("com.u.creds-check.service", fleet="G")]


def test_a_failure_that_clears_and_recurs_is_a_new_episode(host):
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    host.reads([])
    host.sweep("F")
    assert host.keys("F.units_failed") == host.keys("F.units_failed.paged") == []
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    assert len(host.recorded()) == 2
    assert host.pages() == [page("com.t.creds-check.service")] * 2


def test_a_run_in_progress_holds_an_open_episode_and_opens_none(host):
    running = unit("com.t.creds-check.service", state="running", detail="ActiveState=activating")
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    host.reads([running])
    host.sweep("F")
    assert host.keys("F.units_failed") == ["com.t.creds-check.service"]
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    assert len(host.recorded()) == 1 and len(host.pages()) == 1
    host.reads([])
    host.sweep("F")
    host.reads([running])
    host.sweep("F")
    assert host.keys("F.units_failed") == [] and len(host.recorded()) == 1 and len(host.pages()) == 1


def test_a_host_unit_is_recorded_once_a_host_on_its_own_anchor_and_paged_nowhere(host):
    host.fleet("G", "com.u", ["g1", "g2"])
    host.reads([unit("claudlobby-disk-monitor.service", scope="host")])
    for fleet in ("G", "F", "G", "F"):
        host.sweep(fleet)
    assert host.recorded() == [("_host", {"unit": "claudlobby-disk-monitor.service", "scope": "host",
                                          "surface": "systemd", "detail": SYSTEMD, "template": None})]
    assert host.pages() == []
    assert host.keys("_host.units_failed") == ["claudlobby-disk-monitor.service"]
    assert host.keys("F.units_failed") == host.keys("G.units_failed") == []
    assert "Failed units: claudlobby-disk-monitor.service (host," in host.summary("F")
    assert "Failed units" not in host.summary("G")
    host.reads([])
    host.sweep("G")
    assert host.keys("_host.units_failed") == ["claudlobby-disk-monitor.service"]
    host.sweep("F")
    assert host.keys("_host.units_failed") == []


def test_a_page_that_did_not_go_out_is_tried_again_with_no_second_record(host):
    (host.lib / "tg-post.sh").write_text(
        '#!/bin/bash\n[ ! -e "$TG_CAPTURE.fail" ] || exit 3\nprintf "%s\\n" "$1" >> "$TG_CAPTURE"\n')
    (host.tmp / "tg.log.fail").write_text("")
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F")
    assert len(host.recorded()) == 1 and host.pages() == []
    assert host.keys("F.units_failed") == ["com.t.creds-check.service"]
    assert host.keys("F.units_failed.paged") == []
    assert [data["for_event"] for _, data in host.recorded("alert_delivery_failed")] == ["unit_failed"]
    (host.tmp / "tg.log.fail").unlink()
    host.sweep("F")
    host.sweep("F")
    assert len(host.recorded()) == 1
    assert host.pages() == [page("com.t.creds-check.service")]
    assert host.keys("F.units_failed.paged") == ["com.t.creds-check.service"]


def test_a_refused_read_touches_no_key_and_a_bound_sweep_pages_its_guard_once(host):
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F", bound=True)
    host.reads([], refused="failed units cannot be read: no user manager")
    result = host.sweep("F", bound=True)
    host.sweep("F", bound=True)
    assert "failed-units read refused (rc=6): failed units cannot be read: no user manager" in result.stderr
    assert host.keys("F.units_failed") == ["com.t.creds-check.service"]
    assert host.pages() == [page("com.t.creds-check.service"),
                            "FLEET ALERT: the failed-units read for F is refused - a failed job unit cannot "
                            "be seen until it reads again. (failed units cannot be read: no user manager)"]
    host.reads([unit("com.t.creds-check.service")])
    host.sweep("F", bound=True)
    assert len(host.recorded()) == 1 and len(host.pages()) == 2


def test_a_failure_template_instance_and_a_launchd_job_say_what_failed(host):
    host.reads([unit("com.t.job-failed@com.t.sweep.service.service", template="com.t.job-failed@.service"),
                unit("com.t.notify", surface="launchd", detail="last exit status 1")])
    host.sweep("F")
    assert host.pages() == [
        "FLEET ALERT (F): com.t.job-failed@com.t.sweep.service.service failed (systemd " + SYSTEMD
        + "), so the failure alert it sends did not go out. "
        "Its log: journalctl --user -u com.t.job-failed@com.t.sweep.service.service",
        "FLEET ALERT (F): com.t.notify failed (launchd last exit status 1)."]
    assert [data["template"] for _, data in host.recorded()] == ["com.t.job-failed@.service", None]


def test_an_unbound_sweep_says_why_it_read_nothing_and_pages_no_guard(host):
    host.reads([unit("com.t.creds-check.service")])
    result = host.sweep("F", cli=False)
    assert ("failed-units read refused (rc=127): claudlobby: CLAUDLOBBY_CLI must name the composed absolute"
            in result.stderr)
    host.reads([], refused="failed units cannot be read: no release is selected on this host")
    result = host.sweep("F")
    assert "failed-units read refused (rc=6)" in result.stderr
    assert host.recorded() == [] and host.pages() == [] and host.keys("F.units_failed") == []
