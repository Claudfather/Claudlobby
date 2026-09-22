"""#1722 — manifest provenance: what was this fleet composed FROM?

`diff` answers "what would generate change now". Nothing answered "did my
INPUTS change since the runtime was built" — and in the outage this comes from,
a stopped rebase checked out another branch's tree, the manifest reverted on
disk, the next generate composed from the reverted file, and every surface read
healthy. These pin the record and the two rungs that read it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claudlobby.composer import (
    MANIFEST_PROVENANCE_SCHEMA,
    changed_manifest_inputs,
    compose_bot_conf,
    manifest_inputs,
    manifest_provenance,
    manifest_warnings,
    read_manifest_provenance,
    write_manifest_provenance,
)
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent
FLEET_YAML = "fleet:\n  name: demo\n"


def _git(args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def _fleet(**kw):
    return FleetConfig(name="demo", service_prefix="com.example.demo", **kw)


def _paths(tmp_path: Path, *, in_git: bool = False, manifest: str = FLEET_YAML):
    root = tmp_path / "install"
    fleet_dir = root / "local" / "demo"
    (fleet_dir / "runtime" / "bots" / "worker").mkdir(parents=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(manifest)
    if in_git:
        _git(["init", "-q", "-b", "main"], fleet_dir)
        _git(["config", "user.email", "t@example.invalid"], fleet_dir)
        _git(["config", "user.name", "T"], fleet_dir)
        _git(["add", "fleet.yaml"], fleet_dir)
        _git(["commit", "-qm", "seed"], fleet_dir)
    return Paths(root=root, fleet_dir=fleet_dir)


class TestTheRecord:
    def test_provenance_hashes_every_present_input(self, tmp_path):
        paths = _paths(tmp_path)
        prov = manifest_provenance(_fleet(), paths)
        assert prov["schema"] == MANIFEST_PROVENANCE_SCHEMA
        assert prov["files"]["fleet.yaml"]["present"] is True
        assert len(prov["files"]["fleet.yaml"]["sha256"]) == 64
        # projects.yaml does not exist here: ABSENT is a third state and is kept
        # as one — the outage began with inputs VANISHING, not being edited.
        assert prov["files"]["projects.yaml"]["present"] is False
        assert prov["files"]["projects.yaml"]["sha256"] is None

    def test_provenance_untracked_when_not_in_git(self, tmp_path):
        prov = manifest_provenance(_fleet(), _paths(tmp_path, in_git=False))
        assert prov["git"] == {"in_git": False}
        assert manifest_warnings(prov) == []   # not a git checkout is not a fault

    def test_provenance_records_branch_and_commit_in_git(self, tmp_path):
        prov = manifest_provenance(_fleet(), _paths(tmp_path, in_git=True))
        assert prov["git"]["in_git"] is True
        assert prov["git"]["branch"] == "main"
        assert prov["git"]["commit"]
        assert prov["git"]["interrupted"] is False
        assert prov["git"]["on_default_branch"] is True
        assert manifest_warnings(prov) == []

    def test_composed_json_marks_interrupted_checkout(self, tmp_path):
        """The condition that started the outage: the tree holds the OTHER
        branch's files while every ordinary read looks healthy."""
        paths = _paths(tmp_path, in_git=True)
        (paths.fleet_config_dir / ".git" / "rebase-merge").mkdir()
        prov = write_manifest_provenance(_fleet(), paths)
        assert prov["git"]["interrupted"] is True
        on_disk = json.loads((paths.runtime / "composed.json").read_text())
        assert on_disk["git"]["interrupted"] is True
        assert any("mid-operation" in w for w in manifest_warnings(prov))

    def test_an_off_default_branch_is_warned(self, tmp_path):
        paths = _paths(tmp_path, in_git=True)
        _git(["checkout", "-q", "-b", "side"], paths.fleet_config_dir)
        prov = manifest_provenance(_fleet(), paths)
        assert prov["git"]["on_default_branch"] is False
        assert any("not the checkout's default branch" in w
                   for w in manifest_warnings(prov))

    def test_a_vanished_REQUIRED_input_is_warned(self, tmp_path):
        paths = _paths(tmp_path)
        paths.fleet_yaml.unlink()
        assert any("absent from disk" in w
                   for w in manifest_warnings(manifest_provenance(_fleet(), paths)))

    def test_an_absent_OPTIONAL_input_is_not_warned(self, tmp_path):
        """projects.yaml is optional. Warning on every fleet that has none is a
        rung operators learn to skip — and its DISAPPEARANCE after a compose is
        covered by change detection instead, which is the outage's real shape."""
        paths = _paths(tmp_path)
        assert not paths.projects_yaml.exists()
        assert manifest_warnings(manifest_provenance(_fleet(), paths)) == []


class TestTheStampInBotConf:
    def _bot(self):
        return BotConfig(bot_id="worker", name="worker", expertise=[])

    def test_bot_conf_carries_only_the_manifest_hash(self, tmp_path):
        """Nothing VOLATILE may land here: diff compares bot.conf as exact text,
        so a timestamp or a commit id would read as permanent drift on every bot
        on every run. The hash changes only when the manifest does."""
        paths = _paths(tmp_path, in_git=True)
        conf = compose_bot_conf(self._bot(), _fleet(), paths)
        assert "export FLEET_MANIFEST_SHA256=" in conf
        for volatile in ("composed_at", "FLEET_MANIFEST_COMMIT",
                         "FLEET_MANIFEST_BRANCH", "FLEET_COMPOSED_AT"):
            assert volatile not in conf, volatile

    def test_the_stamp_is_stable_across_composes(self, tmp_path):
        paths = _paths(tmp_path, in_git=True)
        a = compose_bot_conf(self._bot(), _fleet(), paths)
        b = compose_bot_conf(self._bot(), _fleet(), paths)
        assert a == b, "an unchanged manifest must compose byte-identically"

    def test_the_stamp_moves_when_the_manifest_does(self, tmp_path):
        paths = _paths(tmp_path, in_git=True)
        before = compose_bot_conf(self._bot(), _fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# an edit\n")
        after = compose_bot_conf(self._bot(), _fleet(), paths)
        assert before != after, "a changed manifest MUST move the stamp"


class TestChangeDetection:
    def test_unchanged_inputs_report_nothing(self, tmp_path):
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        assert changed_manifest_inputs(_fleet(), paths) == []

    def test_an_edited_manifest_is_named(self, tmp_path):
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# edited under the fleet\n")
        assert changed_manifest_inputs(_fleet(), paths) == ["fleet.yaml"]

    def test_a_vanished_input_counts_as_changed(self, tmp_path):
        """The outage's own shape: the file did not change, it LEFT."""
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.unlink()
        assert "fleet.yaml" in changed_manifest_inputs(_fleet(), paths)

    def test_no_record_returns_empty_and_callers_check_separately(self, tmp_path):
        """Empty here means BOTH 'nothing moved' and 'no record', so every
        caller tests for the record itself — an unknown answer and a clean one
        must not be told apart by this return value alone."""
        paths = _paths(tmp_path)
        assert read_manifest_provenance(paths) is None
        assert changed_manifest_inputs(_fleet(), paths) == []


class TestInputSet:
    def test_the_mission_file_is_an_input_when_configured(self, tmp_path):
        paths = _paths(tmp_path)
        assert "mission_file" not in manifest_inputs(_fleet(), paths)
        f = _fleet(mission="m", mission_file="missions/fleet.md")
        assert manifest_inputs(f, paths)["mission_file"].name == "fleet.md"


class TestTheDoctorRung:
    def _run(self, fleet, paths):
        from claudlobby.doctor import DoctorReport, check_manifest_provenance

        report = DoctorReport()
        check_manifest_provenance(fleet, paths, report)
        return report.checks[-1]

    def test_passes_when_inputs_are_unchanged(self, tmp_path):
        paths = _paths(tmp_path, in_git=True)
        write_manifest_provenance(_fleet(), paths)
        c = self._run(_fleet(), paths)
        assert c.status == "pass" and "unchanged since compose" in c.detail

    def test_warns_on_changed_manifest(self, tmp_path):
        paths = _paths(tmp_path, in_git=True)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# changed under the fleet\n")
        c = self._run(_fleet(), paths)
        assert c.status == "warn"
        assert "manifest changed since the running fleet was composed" in c.detail
        assert "fleet.yaml" in c.detail
        # the remedy names the RESTART, because bot.conf is read once at startup
        assert "restart" in c.detail

    def test_warns_on_interrupted_compose_even_when_nothing_changed_since(self, tmp_path):
        """A fleet composed FROM a wedged checkout is not made sound by the
        manifest sitting still afterwards."""
        paths = _paths(tmp_path, in_git=True)
        (paths.fleet_config_dir / ".git" / "rebase-merge").mkdir()
        write_manifest_provenance(_fleet(), paths)
        c = self._run(_fleet(), paths)
        assert c.status == "warn" and "at compose time" in c.detail
        assert "mid-operation" in c.detail

    def test_warns_when_there_is_no_record_at_all(self, tmp_path):
        c = self._run(_fleet(), _paths(tmp_path))
        assert c.status == "warn" and "without provenance" in c.detail

    def test_a_newer_schema_is_reported_unread_not_interpreted(self, tmp_path):
        """'unchanged' read off a misunderstood record is the false clear the
        record exists to prevent."""
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        f = paths.runtime / "composed.json"
        d = json.loads(f.read_text())
        d["schema"] = MANIFEST_PROVENANCE_SCHEMA + 1
        f.write_text(json.dumps(d))
        c = self._run(_fleet(), paths)
        assert c.status == "warn" and "not interpreting it" in c.detail.lower()

    def test_the_rung_never_FAILS(self, tmp_path):
        """warn-level by design: it reports a sibling checkout's state, which
        claudlobby does not own (check_claudron's precedent)."""
        paths = _paths(tmp_path, in_git=True)
        (paths.fleet_config_dir / ".git" / "rebase-merge").mkdir()
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text("fleet:\n  name: demo\n# and changed too\n")
        assert self._run(_fleet(), paths).status != "fail"


class TestTheDiffHeader:
    def _header(self, fleet, paths):
        from claudlobby.diff import manifest_header

        return manifest_header(fleet, paths)

    def test_unchanged_says_so_with_the_compose_age(self, tmp_path):
        paths = _paths(tmp_path)
        prov = write_manifest_provenance(_fleet(), paths)
        h = self._header(_fleet(), paths)
        assert h.startswith("manifest: unchanged since compose")
        assert prov["composed_at"] in h

    def test_changed_says_the_INPUTS_moved(self, tmp_path):
        """The distinction the header exists for: a diff body alone cannot say
        whether the runtime drifted or the manifest did."""
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# moved\n")
        h = self._header(_fleet(), paths)
        assert "manifest: CHANGED" in h and "fleet.yaml" in h
        assert "inputs moved" in h

    def test_no_record_is_said_rather_than_assumed_clean(self, tmp_path):
        h = self._header(_fleet(), _paths(tmp_path))
        assert "NO PROVENANCE RECORDED" in h

    def test_a_newer_schema_is_not_interpreted(self, tmp_path):
        paths = _paths(tmp_path)
        write_manifest_provenance(_fleet(), paths)
        f = paths.runtime / "composed.json"
        d = json.loads(f.read_text())
        d["schema"] = MANIFEST_PROVENANCE_SCHEMA + 99
        f.write_text(json.dumps(d))
        assert "not interpreting" in self._header(_fleet(), paths).lower()


class TestTheRungStaysQuietWhereItHasNothingToSay:
    def _checks(self, fleet, paths):
        from claudlobby.doctor import DoctorReport, check_manifest_provenance

        report = DoctorReport()
        check_manifest_provenance(fleet, paths, report)
        return report.checks

    def test_a_fleet_that_was_never_composed_gets_NO_rung(self, tmp_path):
        """"Regenerate to record what this runtime was composed from" is
        nonsense addressed to a runtime that does not exist, and doctor already
        has rungs for "you have not generated yet". check_claudron's precedent:
        silent where there is nothing to diagnose."""
        root = tmp_path / "install"
        fleet_dir = root / "local" / "demo"
        fleet_dir.mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)
        paths = Paths(root=root, fleet_dir=fleet_dir)
        assert self._checks(_fleet(), paths) == []

    def test_a_COMPOSED_fleet_without_a_record_still_warns(self, tmp_path):
        """The positive control: the silence above must be scoped to 'nothing
        composed', not swallow the case the rung exists for."""
        paths = _paths(tmp_path)           # _paths() creates runtime/bots/worker
        assert self._checks(_fleet(), paths)[-1].status == "warn"


class TestDirtyIsLoadBearing:
    """vera, #1726 review: `dirty` was computed, persisted and read by nothing —
    a recorded fact nothing consumes and no test pins. It is now the read-time
    discriminator that survives the compose-time snapshot's bound."""

    def _attr(self, tmp_path, *, in_git=True, edit=False, commit=False):
        from claudlobby.composer import manifest_change_attribution

        paths = _paths(tmp_path, in_git=in_git)
        write_manifest_provenance(_fleet(), paths)
        if edit:
            paths.fleet_yaml.write_text(FLEET_YAML + "# a later change\n")
        if commit:
            _git(["add", "fleet.yaml"], paths.fleet_config_dir)
            _git(["commit", "-qm", "committed the change"], paths.fleet_config_dir)
        return manifest_change_attribution(_fleet(), paths)

    def test_an_UNCOMMITTED_edit_is_named_as_one(self, tmp_path):
        out = self._attr(tmp_path, edit=True)
        assert "uncommitted" in out and "working tree" in out

    def test_a_COMMITTED_change_says_it_arrived_through_git(self, tmp_path):
        """The case the compose-time snapshot cannot attribute once the git
        anomaly is repaired — this is what still answers."""
        out = self._attr(tmp_path, edit=True, commit=True)
        assert "arrived through git" in out
        assert "uncommitted" in out, "it must still NAME what it excluded"

    def test_it_does_not_claim_to_separate_a_commit_from_a_checkout(self, tmp_path):
        """The bound, pinned. A reader handed a confident word cannot go and
        look; one told what the answer cannot distinguish can."""
        out = self._attr(tmp_path, edit=True, commit=True)
        assert "a commit, a checkout or a branch switch" in out

    def test_not_a_git_checkout_is_NOT_rendered_as_a_fault(self, tmp_path):
        assert self._attr(tmp_path, in_git=False, edit=True) is None

    def test_the_doctor_rung_carries_the_attribution(self, tmp_path):
        from claudlobby.doctor import DoctorReport, check_manifest_provenance

        paths = _paths(tmp_path, in_git=True)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# changed\n")
        report = DoctorReport()
        check_manifest_provenance(_fleet(), paths, report)
        c = report.checks[-1]
        assert c.status == "warn" and "uncommitted" in c.detail

    def test_the_diff_header_carries_the_attribution(self, tmp_path):
        from claudlobby.diff import manifest_header

        paths = _paths(tmp_path, in_git=True)
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# changed\n")
        h = manifest_header(_fleet(), paths)
        assert "manifest: CHANGED" in h and "uncommitted" in h


class TestTheBoundIsWrittenDownWhereAReaderMeetsIt:
    """vera: 'a recorded fact that silently stops discriminating is worse than
    no record, because a reader will trust it.' The bound must be stated in the
    code AND in the operator-facing doc, not only in a review thread."""

    def test_the_code_states_the_snapshot_bound(self):
        from claudlobby.composer import manifest_provenance

        doc = manifest_provenance.__doc__ or ""
        assert "SNAPSHOT" in doc and "not an audit trail" in doc
        assert "repair-then-generate" in doc, "the uncovered ORDER must be named"

    def test_the_lifecycle_doc_states_it_too(self):
        text = (REPO / "documentation" / "fleet-update-lifecycle.md").read_text()
        assert "repair" in text.lower() and "snapshot" in text.lower(), (
            "the operator-facing doc must carry the bound, not just the code")
