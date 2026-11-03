"""Check that a supported platform's shards ran its collected suite, each test once.

CI runs each supported platform's suite in TEST_SPLITS shards
(.github/workflows/test.yml). pytest-split hands each shard a share of the
collected suite by the times in .test_durations, and the shares add up to the
suite only while every shard collects the same tests in the same order. So each
shard lists the suite it collected, in the step and the environment it runs it
in, before it runs its share:

    .venv/bin/python tests/ci_shards.py ids collection.txt collected.txt

and the platform's gate job, which carries the platform's check name, reads
every shard's list and JUnit report:

    python3 tests/ci_shards.py check --lane NAME --splits N shards/

It fails unless there is one shard for each group from 1 to N; every shard
collected the same suite; the reports between them hold every collected test
exactly once, and nothing else but the modules skipped at collection, which
every shard reports; the skips, counted the way one run of the whole suite
counts them, are at most the lane's ceiling in SKIP_CEILINGS; and each test
named with --require-passed ran once and passed.

    python3 tests/ci_shards.py durations shards/ OUT

merges the times the shards measured into one file in .test_durations' format.
It gates nothing; documentation/test-suite.md says when to commit it.

Standard library only: the gate job runs it with the runner's own python3.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

# The most tests each platform's suite may skip, counted the way one run of the
# whole suite counts them: each test skipped in any shard, and each module
# skipped at collection once (every shard collects the suite, so every shard
# reports that module). Each shard's skips can sit under a ceiling that their
# sum breaks, so the gate counts across the shards, never per shard. A skip is
# a test that did not run, so each ceiling is the count the suite has now, not
# a margin above it. A change that adds a skip raises the ceiling here and says
# why in its pull request; when a change removes skips, the check prints a
# notice until the ceiling comes down to the count.
SKIP_CEILINGS = {
    "supported (ubuntu-latest, py3.10)": 17,
    "supported (macos-latest, py3.11)": 124,
}

# pytest's count line after a --collect-only listing: "9997/10002 tests
# collected (5 deselected) in 19.6s", or "12 tests collected in 0.1s".
_COUNT = re.compile(r"^(\d+)(?:/\d+)? tests? collected\b")
_NODE_ID = re.compile(r"^[^\s:]+\.py::\S")
_SHARD = re.compile(r"-shard-(\d+)$")
# _pytest/junitxml.py's bin_xml_escape: the characters an XML document cannot
# hold, which the report writes as #xNN.
_XML_ILLEGAL = re.compile(
    "[^\u0009\u000a\u000d -~\u0080-퟿-�\U00010000-\U0010ffff]"
)
# How a JUnit <skipped> says what it was: a test's skip and an xfail carry a
# type; a module skipped while it was collected is written with none.
_SKIP_TYPES = {"pytest.skip": "skipped", "pytest.xfail": "xfailed", None: "collection-skipped"}
_LISTED = 20  # names printed per problem before the rest are counted


class ShardError(Exception):
    """Shard output that does not show what the gate has to see."""


def collected_ids(text: str) -> list[str]:
    """The node ids `pytest --collect-only --verbosity=-1` listed, checked
    against the count pytest printed after them: a line the listing holds that
    is not a test, or a test it dropped, cannot pass unnoticed."""
    lines = text.splitlines()
    ids = []
    for line in lines:
        if not line:
            break
        ids.append(line)
    counts = [int(m.group(1)) for line in lines if (m := _COUNT.match(line))]
    if len(counts) != 1:
        raise ShardError(f"expected pytest's one 'N tests collected' line, found {len(counts)}")
    strays = [line for line in ids if not _NODE_ID.match(line)]
    if strays:
        raise ShardError(f"the listing holds lines that are not test ids: {strays[:3]}")
    if len(ids) != counts[0]:
        raise ShardError(f"read {len(ids)} test ids, pytest counted {counts[0]}")
    if len(set(ids)) != len(ids):
        raise ShardError("the listing names one test twice")
    return ids


def fingerprint(ids: list[str]) -> str:
    return hashlib.sha256("".join(f"{i}\n" for i in ids).encode()).hexdigest()


def _xml_escape(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        i = ord(m.group())
        return f"#x{i:02X}" if i <= 0xFF else f"#x{i:04X}"

    return _XML_ILLEGAL.sub(repl, text)


def junit_key(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's JUnit report gives the test with this
    node id: _pytest/junitxml.py's mangle_test_address, and the escape
    record_testreport applies to the name."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), _xml_escape(names[-1])


def read_report(path: Path) -> list[tuple[tuple[str, str], str, str]]:
    """Each test case of a JUnit report: its (classname, name), its outcome
    (passed, failed, skipped, xfailed, or collection-skipped for a module
    skipped while it was collected), and the message of a skip."""
    cases = []
    for case in ET.parse(path).iter("testcase"):
        key = (case.get("classname", ""), case.get("name", ""))
        skipped = case.find("skipped")
        message = ""
        if case.find("failure") is not None or case.find("error") is not None:
            outcome = "failed"
        elif skipped is not None:
            message = skipped.get("message", "")
            kind = skipped.get("type")
            outcome = _SKIP_TYPES.get(kind, f"skipped as {kind}")
        else:
            outcome = "passed"
        cases.append((key, outcome, message))
    return cases


def _shards(root: Path, splits: int) -> dict[int, Path]:
    """One artifact directory per group, 1 to splits."""
    found = defaultdict(list)
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        m = _SHARD.search(path.name)
        if not m:
            raise ShardError(f"{path.name}: not a shard's artifact (no -shard-N suffix)")
        found[int(m.group(1))].append(path)
    if sorted(found) != list(range(1, splits + 1)) or any(len(v) != 1 for v in found.values()):
        names = ", ".join(p.name for paths in found.values() for p in paths) or "none"
        raise ShardError(f"expected one artifact for each of shards 1 to {splits}, found {names}")
    return {group: paths[0] for group, paths in sorted(found.items())}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ShardError(f"{path.parent.name} uploaded no {path.name}") from None


def _listed(items) -> str:
    items = list(items)
    more = f"\n  (and {len(items) - _LISTED} more)" if len(items) > _LISTED else ""
    return "".join(f"\n  {item}" for item in items[:_LISTED]) + more


def _same_suite(lists: dict[int, list[str]]) -> list[str]:
    suite = lists[1]
    for group, ids in lists.items():
        if ids != suite:
            apart = next(
                (n for n, (a, b) in enumerate(zip(suite, ids)) if a != b), min(len(suite), len(ids))
            )
            raise ShardError(
                f"shard {group} collected a different suite from shard 1: {len(ids)} tests "
                f"(sha256 {fingerprint(ids)}) against {len(suite)} (sha256 {fingerprint(suite)}), "
                f"first apart at test {apart + 1}"
            )
    return suite


def check(root: Path, lane: str, splits: int, require_passed: list[str]) -> list[str]:
    """What the shards in root show, as lines for the job log; raises
    ShardError naming every problem found."""
    if lane not in SKIP_CEILINGS:
        raise ShardError(f"no skip ceiling for {lane!r}; SKIP_CEILINGS has {sorted(SKIP_CEILINGS)}")
    ceiling = SKIP_CEILINGS[lane]
    shards = _shards(root, splits)
    for group, path in shards.items():
        rc = _read(path / "pytest.rc").strip()
        if rc != "0":
            raise ShardError(f"shard {group}'s pytest exited {rc}")
    suite = _same_suite({g: _read(d / "collected.txt").splitlines() for g, d in shards.items()})
    reports = {g: read_report(d / "pytest-results.xml") for g, d in shards.items()}
    keys = {}
    for nodeid in suite:
        key = junit_key(nodeid)
        if key in keys:
            raise ShardError(f"two collected tests share one JUnit name: {keys[key]} and {nodeid}")
        keys[key] = nodeid

    problems = []
    ran = defaultdict(list)
    collection_skips = defaultdict(set)
    skipped = {}
    for group, cases in reports.items():
        for key, outcome, message in cases:
            if key in keys:
                ran[key].append(group)
                if outcome == "skipped":
                    skipped[keys[key]] = message
                elif outcome not in ("passed", "xfailed"):
                    problems.append(f"shard {group} reports {keys[key]} {outcome}")
            elif outcome == "collection-skipped":
                module = ".".join(filter(None, key))
                collection_skips[module].add(group)
                skipped[module] = message
            else:
                problems.append(f"shard {group} reports a test it never collected: {key}")
    missing = [nodeid for key, nodeid in keys.items() if not ran[key]]
    twice = [
        f"{keys[key]} (shards {', '.join(map(str, groups))})"
        for key, groups in ran.items()
        if len(groups) > 1
    ]
    partial = [f"{m} (shards {sorted(g)})" for m, g in collection_skips.items() if len(g) != splits]
    if missing:
        problems.append(f"{len(missing)} collected test(s) ran in no shard:{_listed(missing)}")
    if twice:
        problems.append(f"{len(twice)} test(s) ran in more than one shard:{_listed(twice)}")
    if partial:
        problems.append(f"module(s) skipped at collection in some shards only:{_listed(partial)}")
    if len(skipped) > ceiling:
        problems.append(
            f"{len(skipped)} tests skipped across the {splits} shards, over the ceiling of "
            f"{ceiling} for {lane} (SKIP_CEILINGS in tests/ci_shards.py). A skip is a test that "
            "did not run: if a new one is meant to skip on this platform, raise the ceiling in "
            "the same change and say why. The skips:"
            + "".join(f"\n  {nodeid}: {why}" for nodeid, why in sorted(skipped.items()))
        )
    for name in require_passed:
        hits = [(g, o) for g, cases in reports.items() for (k, o, _) in cases if k[1] == name]
        if len(hits) != 1 or hits[0][1] != "passed":
            problems.append(f"{name} must run once and pass; the shards report {hits or 'nothing'}")
    if problems:
        raise ShardError("\n".join(problems))

    lines = [f"{lane}: {splits} shards collected the same {len(suite)} tests"
             f" (sha256 {fingerprint(suite)})."]
    for group, cases in reports.items():
        mine = [outcome for key, outcome, _ in cases if key in keys]
        lines.append(f"  shard {group}: {len(mine)} ran, {mine.count('skipped')} skipped")
    lines.append("Every collected test ran in exactly one shard.")
    lines.append(f"Skipped: {len(skipped)} (ceiling {ceiling}).")
    if len(skipped) < ceiling:
        lines.append(
            f"::notice::{lane} skips {len(skipped)} tests, under its ceiling of {ceiling}: "
            f"lower its entry in SKIP_CEILINGS (tests/ci_shards.py) to {len(skipped)}."
        )
    lines.extend(f"Ran once and passed: {name}" for name in require_passed)
    return lines


def merge_durations(root: Path) -> dict[str, float]:
    """Every shard's durations file, as one: each shard stored the times of
    exactly the tests it ran (pytest-split's --store-durations
    --clean-durations), so the files hold disjoint tests.

    A node id holding an @ is left out: the content check, which a pull
    request must pass, reads a user@host in an added line as an address (a
    test's URL with user:password@ is one). A test left out still runs, at the
    average time."""
    merged = {}
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        merged.update(json.loads(_read(path / "durations.json")))
    # Four decimals keep the file small, and the floor keeps every time above
    # zero, so that a test timed at under 50 microseconds still weighs something.
    return {
        nodeid: max(round(secs, 4), 0.0001)
        for nodeid, secs in sorted(merged.items())
        if "@" not in nodeid
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ids_cmd = commands.add_parser("ids", help="list one shard's --collect-only node ids")
    ids_cmd.add_argument("collection", type=Path)
    ids_cmd.add_argument("out", type=Path)
    check_cmd = commands.add_parser("check", help="check every shard of one lane")
    check_cmd.add_argument("--lane", required=True)
    check_cmd.add_argument("--splits", required=True, type=int)
    check_cmd.add_argument("--require-passed", action="append", default=[], metavar="NAME")
    check_cmd.add_argument("shards", type=Path)
    durations_cmd = commands.add_parser("durations", help="merge the times the shards measured")
    durations_cmd.add_argument("shards", type=Path)
    durations_cmd.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "ids":
            ids = collected_ids(_read(args.collection))
            args.out.write_text("".join(f"{i}\n" for i in ids), encoding="utf-8")
            print(f"Collected {len(ids)} tests, sha256 {fingerprint(ids)}")
        elif args.command == "check":
            print("\n".join(check(args.shards, args.lane, args.splits, args.require_passed)))
        else:
            merged = merge_durations(args.shards)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(merged, sort_keys=True, indent=4) + "\n", "utf-8")
            print(f"Merged the times of {len(merged)} tests into {args.out}")
    except ShardError as exc:
        print(exc, file=sys.stderr)
        print(f"::error::{str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
