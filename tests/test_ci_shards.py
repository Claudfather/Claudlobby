"""tests/ci_shards.py: the check that a supported platform's CI shards ran its
collected suite, each test once.

Driven by real pytest output. A probe suite is listed and run in shards by
pytest itself, through a stand-in for pytest-split that does what pytest-split
does after collection and ``-m``: keeps one group's share and deselects the
rest. So the listings and JUnit reports the check reads are the ones pytest
writes, tricky node ids and a module skipped at collection included.
pytest-split itself runs in CI only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from tests import ci_shards

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "pyproject.toml"
DURATIONS = REPO / ".test_durations"
SPLITS = 3
EXPRESSION = "not quarantine and not harness"

# pytest-split's mechanism with a plain round-robin for its arithmetic. PROBE_ALSO
# and PROBE_DROP break the split on purpose: a test run twice, a test run nowhere.
SPLITTER = '''
import os

import pytest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if "PROBE_GROUP" not in os.environ:
        return
    splits, group = int(os.environ["PROBE_SPLITS"]), int(os.environ["PROBE_GROUP"])
    also = set(os.environ.get("PROBE_ALSO", "").split("\\n")) - {""}
    drop = set(os.environ.get("PROBE_DROP", "").split("\\n")) - {""}
    mine = [
        item for n, item in enumerate(items)
        if (n % splits == group - 1 or item.nodeid in also) and item.nodeid not in drop
    ]
    config.hook.pytest_deselected(items=[item for item in items if item not in mine])
    items[:] = mine
'''

PROBE = {
    "tests/__init__.py": "",
    "tests/test_a.py": '''
import pytest


def test_plain():
    pass


@pytest.mark.parametrize("v", ["a b", "x::y", "p.q/r", "[n]", "uni-\\u00e9", "ctl\\x07"])
def test_params(v):
    pass


class TestK:
    def test_m(self):
        pass

    @pytest.mark.skip(reason="skip one")
    def test_s(self):
        pass

    @pytest.mark.xfail(reason="expected to fail")
    def test_x(self):
        assert False


@pytest.mark.skip(reason="skip two")
def test_s2():
    pass


@pytest.mark.skip(reason="skip three")
def test_s3():
    pass


@pytest.mark.harness
def test_for_the_harness_job():
    pass
''',
    "tests/sub/__init__.py": "",
    "tests/sub/test_b.py": "def test_deep():\n    pass\n",
    "tests/test_skipmod.py": '''
import pytest

pytest.skip("skipped at collection", allow_module_level=True)


def test_never_collected():
    pass
''',
}

PARAMS = ["a b", "x::y", "p.q/r", "[n]", "uni-\\xe9", "ctl\\x07"]
COLLECTED = [
    "tests/sub/test_b.py::test_deep",
    "tests/test_a.py::test_plain",
    *(f"tests/test_a.py::test_params[{v}]" for v in PARAMS),
    "tests/test_a.py::TestK::test_m",
    "tests/test_a.py::TestK::test_s",
    "tests/test_a.py::TestK::test_x",
    "tests/test_a.py::test_s2",
    "tests/test_a.py::test_s3",
]
SKIPS = 4  # three tests and the module skipped at collection


def _probe(root: Path) -> Path:
    probe = root / "probe"
    for rel, text in PROBE.items():
        (probe / rel).parent.mkdir(parents=True, exist_ok=True)
        (probe / rel).write_text(text)
    (probe / "conftest.py").write_text(SPLITTER)
    return probe


def _pytest(probe: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    child = {k: v for k, v in os.environ.items() if not k.startswith("PROBE_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(CONFIG), "--rootdir", str(probe),
         "-p", "no:cacheprovider", *args, "tests"],
        cwd=probe, capture_output=True, text=True, env={**child, **env},
    )


def _run_shard(probe: Path, out: Path, group: int, **env: str) -> Path:
    """What one CI shard does: list the suite, then run its share."""
    shard = out / f"pytest-probe-shard-{group}"
    shard.mkdir(parents=True)
    listing = _pytest(probe, "--collect-only", "--verbosity=-1", "-m", EXPRESSION)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    (shard / "collection.txt").write_text(listing.stdout)
    assert ci_shards.main(["ids", str(shard / "collection.txt"), str(shard / "collected.txt")]) == 0
    run = _pytest(
        probe, "-q", "-m", EXPRESSION, f"--junitxml={shard / 'pytest-results.xml'}",
        PROBE_SPLITS=str(SPLITS), PROBE_GROUP=str(group), **env,
    )
    (shard / "pytest.rc").write_text(f"{run.returncode}\n")
    return shard


@pytest.fixture(scope="module")
def shards(tmp_path_factory) -> Path:
    """The probe suite, listed and run in SPLITS shards."""
    root = tmp_path_factory.mktemp("shards")
    probe = _probe(root)
    for group in range(1, SPLITS + 1):
        _run_shard(probe, root / "out", group)
    return root


@pytest.fixture
def copy(shards, tmp_path) -> Path:
    """A copy of the shards' output to break."""
    out = tmp_path / "out"
    shutil.copytree(shards / "out", out)
    return out


@pytest.fixture
def ceiling(monkeypatch):
    def set_ceiling(value: int) -> None:
        monkeypatch.setitem(ci_shards.SKIP_CEILINGS, "probe", value)

    set_ceiling(SKIPS)
    return set_ceiling


def _check(out: Path, *require: str) -> list[str]:
    return ci_shards.check(out, "probe", SPLITS, list(require))


def _refused(out: Path, *require: str) -> str:
    with pytest.raises(ci_shards.ShardError) as refusal:
        _check(out, *require)
    return str(refusal.value)


def _skips_in(report: Path) -> int:
    """The skips one shard's report holds: tests and modules, not xfails."""
    kinds = [case.find("skipped") for case in ET.parse(report).iter("testcase")]
    return sum(1 for kind in kinds if kind is not None and kind.get("type") != "pytest.xfail")


# --- the listing each shard makes of its suite ------------------------------------------------

def test_the_listing_is_the_collected_suite_in_order(shards):
    for group in range(1, SPLITS + 1):
        listed = (shards / "out" / f"pytest-probe-shard-{group}" / "collected.txt").read_text()
        assert listed.splitlines() == COLLECTED


def test_every_shard_ran_only_its_share(shards):
    """The premise of every check below: the stand-in split the suite."""
    for group in range(1, SPLITS + 1):
        rc = (shards / "out" / f"pytest-probe-shard-{group}" / "pytest.rc").read_text()
        assert rc.strip() == "0"
    names = [
        len(list(ET.parse(shards / "out" / f"pytest-probe-shard-{g}" / "pytest-results.xml")
                 .iter("testcase")))
        for g in range(1, SPLITS + 1)
    ]
    # each share, plus the module every shard reports skipped at collection
    assert sum(names) == len(COLLECTED) + SPLITS, names


@pytest.mark.parametrize("damage", ["drop-a-line", "a-stray-line", "no-count", "two-counts",
                                    "a-repeated-id"])
def test_a_listing_that_disagrees_with_pytests_count_is_refused(shards, damage):
    text = (shards / "out" / "pytest-probe-shard-1" / "collection.txt").read_text()
    lines = text.splitlines()
    count = next(n for n, line in enumerate(lines) if " collected" in line)
    if damage == "drop-a-line":
        del lines[3]
    elif damage == "a-stray-line":
        lines.insert(2, "some output that is not a test")
    elif damage == "no-count":
        del lines[count]
    elif damage == "two-counts":
        lines.append(lines[count])
    else:
        lines[3] = lines[2]
    with pytest.raises(ci_shards.ShardError):
        ci_shards.collected_ids("\n".join(lines) + "\n")


# --- the gate's check -------------------------------------------------------------------------

def test_shards_that_ran_the_suite_each_test_once_pass(shards, ceiling):
    lines = _check(shards / "out")
    assert lines[0].startswith(f"probe: {SPLITS} shards collected the same {len(COLLECTED)} tests")
    assert "Every collected test ran in exactly one shard." in lines
    assert f"Skipped: {SKIPS} (ceiling {SKIPS})." in lines


def test_the_ceiling_counts_skips_across_all_shards(shards, ceiling):
    """The defect a per-session ceiling has: every shard sits under the
    ceiling while their sum breaks it."""
    reports = [shards / "out" / f"pytest-probe-shard-{g}" / "pytest-results.xml"
               for g in range(1, SPLITS + 1)]
    per_shard = [_skips_in(report) for report in reports]
    ceiling(SKIPS - 1)
    assert max(per_shard) <= SKIPS - 1, per_shard  # a per-shard check would pass
    refusal = _refused(shards / "out")
    over = f"{SKIPS} tests skipped across the {SPLITS} shards, over the ceiling of {SKIPS - 1}"
    assert over in refusal, refusal
    for skipped in ("tests/test_a.py::TestK::test_s", "tests/test_a.py::test_s2",
                    "tests.test_skipmod"):
        assert skipped in refusal, refusal


def test_a_module_skipped_at_collection_counts_once(shards, ceiling):
    """Every shard collects the suite, so every shard reports the module."""
    for group in range(1, SPLITS + 1):
        report = shards / "out" / f"pytest-probe-shard-{group}" / "pytest-results.xml"
        names = [case.get("name") for case in ET.parse(report).iter("testcase")]
        assert "tests.test_skipmod" in names, (group, names)
    assert f"Skipped: {SKIPS} (ceiling {SKIPS})." in _check(shards / "out")


def test_a_count_under_the_ceiling_passes_with_a_notice(shards, ceiling):
    ceiling(SKIPS + 2)
    notice = [line for line in _check(shards / "out") if line.startswith("::notice::")]
    assert notice and f"to {SKIPS}." in notice[0], notice


def test_a_test_two_shards_ran_is_refused(shards, copy, ceiling):
    probe = _probe(shards / "rerun-twice")
    shutil.rmtree(copy / "pytest-probe-shard-1")
    _run_shard(probe, copy, 1, PROBE_ALSO="tests/test_a.py::TestK::test_m")
    refusal = _refused(copy)
    assert "ran in more than one shard" in refusal and "TestK::test_m (shards 1, " in refusal


def test_a_collected_test_no_shard_ran_is_refused(shards, copy, ceiling):
    probe = _probe(shards / "rerun-drop")
    gone = "tests/test_a.py::test_params[x::y]"
    owner = COLLECTED.index(gone) % SPLITS + 1  # the stand-in's round-robin
    shutil.rmtree(copy / f"pytest-probe-shard-{owner}")
    _run_shard(probe, copy, owner, PROBE_DROP=gone)
    refusal = _refused(copy)
    assert "1 collected test(s) ran in no shard" in refusal and gone in refusal


@pytest.mark.parametrize("damage", ["reordered", "one-fewer"])
def test_shards_that_collected_different_suites_are_refused(copy, ceiling, damage):
    listing = copy / "pytest-probe-shard-2" / "collected.txt"
    ids = listing.read_text().splitlines()
    ids = [ids[1], ids[0], *ids[2:]] if damage == "reordered" else ids[:-1]
    listing.write_text("".join(f"{i}\n" for i in ids))
    assert "shard 2 collected a different suite from shard 1" in _refused(copy)


def test_a_reported_test_no_shard_collected_is_refused(copy, ceiling):
    report = copy / "pytest-probe-shard-3" / "pytest-results.xml"
    tree = ET.parse(report)
    ET.SubElement(tree.getroot().find("testsuite"), "testcase",
                  classname="tests.test_a", name="test_from_elsewhere")
    tree.write(report)
    assert "a test it never collected" in _refused(copy)


@pytest.mark.parametrize("damage", ["missing", "extra", "unnamed"])
def test_one_artifact_per_shard_or_refused(copy, ceiling, damage):
    if damage == "missing":
        shutil.rmtree(copy / "pytest-probe-shard-3")
    elif damage == "extra":
        shutil.copytree(copy / "pytest-probe-shard-3", copy / "pytest-probe-shard-4")
    else:
        (copy / "pytest-probe-logs").mkdir()
    with pytest.raises(ci_shards.ShardError):
        _check(copy)


def test_a_shard_whose_pytest_failed_is_refused(copy, ceiling):
    (copy / "pytest-probe-shard-2" / "pytest.rc").write_text("1\n")
    assert "shard 2's pytest exited 1" in _refused(copy)


def test_a_shard_that_uploaded_no_listing_is_refused(copy, ceiling):
    (copy / "pytest-probe-shard-1" / "collected.txt").unlink()
    assert "uploaded no collected.txt" in _refused(copy)


def test_a_lane_with_no_ceiling_is_refused(shards):
    with pytest.raises(ci_shards.ShardError, match="no skip ceiling"):
        ci_shards.check(shards / "out", "a lane nobody named", SPLITS, [])


def test_required_evidence_passes_once_it_ran_once_and_passed(shards, ceiling):
    assert "Ran once and passed: test_params[a b]" in _check(shards / "out", "test_params[a b]")


@pytest.mark.parametrize("name", ["test_s", "test_x", "test_not_in_the_suite"],
                         ids=["skipped", "xfailed", "absent"])
def test_required_evidence_that_did_not_pass_is_refused(shards, ceiling, name):
    assert f"{name} must run once and pass" in _refused(shards / "out", name)


def test_required_evidence_that_ran_twice_is_refused(shards, copy, ceiling):
    probe = _probe(shards / "rerun-evidence")
    shutil.rmtree(copy / "pytest-probe-shard-1")
    _run_shard(probe, copy, 1, PROBE_ALSO="tests/sub/test_b.py::test_deep")
    assert "test_deep must run once and pass" in _refused(copy, "test_deep")


def test_the_cli_exits_1_and_annotates_the_refusal(copy, ceiling, capsys):
    (copy / "pytest-probe-shard-2" / "pytest.rc").write_text("2\n")
    assert ci_shards.main(["check", "--lane", "probe", "--splits", str(SPLITS), str(copy)]) == 1
    assert "::error::shard 2's pytest exited 2" in capsys.readouterr().err


def test_junit_key_is_pytests_own_for_every_probe_id(shards):
    written = set()
    for group in range(1, SPLITS + 1):
        report = shards / "out" / f"pytest-probe-shard-{group}" / "pytest-results.xml"
        written |= {(c.get("classname"), c.get("name")) for c in ET.parse(report).iter("testcase")}
    assert {ci_shards.junit_key(nodeid) for nodeid in COLLECTED} <= written


# --- the measured times -----------------------------------------------------------------------

def test_the_merged_durations_are_every_shards_times(tmp_path):
    shard_times = (
        (1, {"tests/t.py::b": 1.23456789, "tests/t.py::c[https://u:p@host.test]": 0.5}),
        (2, {"tests/t.py::a": 0.00001}),
    )
    for group, times in shard_times:
        (tmp_path / f"d-shard-{group}").mkdir()
        (tmp_path / f"d-shard-{group}" / "durations.json").write_text(json.dumps(times))
    out = tmp_path / ".test_durations"
    assert ci_shards.main(["durations", str(tmp_path), str(out)]) == 0
    text = out.read_text()
    assert json.loads(text) == {"tests/t.py::a": 0.0001, "tests/t.py::b": 1.2346}
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=4) + "\n"


def test_the_committed_durations_are_pytest_split_input():
    durations = json.loads(DURATIONS.read_text())
    assert isinstance(durations, dict) and durations
    assert list(durations) == sorted(durations)
    bad = [
        nodeid for nodeid, seconds in durations.items()
        if not ci_shards._NODE_ID.match(nodeid) or "@" in nodeid
        or not isinstance(seconds, float) or seconds <= 0
    ]
    assert not bad, bad[:5]
