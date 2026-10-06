#!/usr/bin/env python3
"""A stand-in for `gh`, for the merge guardrails' rollout rungs (#2116 review).

It answers the reads those rungs make, from a JSON state file ($FAKE_GH_STATE),
and refuses (exit 2) a read whose arguments are not the ones the rung is pinned
to, so a rung that drops `--paginate` or reads the rollup another way fails its
test instead of passing on the stand-in's goodwill. Each call's arguments are
appended to $FAKE_GH_LOG, one call per line. state["fail"] names reads that
fail like a throttled or broken API: "repo", "contents", "rollup", "files",
"holds", "closes". state["contents"] is "present" or "absent" (a 404).
"""

import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as fh:
    state = json.load(fh)
fail = set(state.get("fail") or [])

ROLLUP_RUNS = '[.statusCheckRollup[] | select(.name == "rollout-check / Rollout check")] '
NEWEST = 'sort_by(.startedAt) | last | .conclusion // "ABSENT"'
# A run that has not completed (queued or in progress) reads PENDING, whatever
# its startedAt: a queued run's can be null or a zero time, which sorts it first.
ROLLUP_JQ = ROLLUP_RUNS + '| if any(.status != "COMPLETED") then "PENDING" else (' + NEWEST + ') end'
FILES_JQ = ".[] | .filename, (.previous_filename // empty)"


def refuse(why):
    print(f"fake gh: {why}: {args}", file=sys.stderr)
    sys.exit(2)


def broken():
    print("gh: Server Error (HTTP 502)", file=sys.stderr)
    sys.exit(1)


def jq_arg():
    return args[args.index("--jq") + 1] if "--jq" in args else None


if args[:1] == ["api"]:
    endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
    if "/contents/.github/workflows/rollout-check.yml?ref=" in endpoint:
        if "--silent" not in args:
            refuse("the adoption read prints nothing")
        if not endpoint.endswith("?ref=" + state.get("default", "main")):
            refuse("the adoption read is not on the default branch")
        if "contents" in fail:
            broken()
        if state.get("contents") != "present":
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            sys.exit(1)
    elif endpoint.endswith("/files"):
        if "--paginate" not in args or jq_arg() != FILES_JQ:
            refuse("the file read is not every page of filenames and previous names")
        if "files" in fail:
            broken()
        for item in state.get("files", []):
            print(item["filename"])
            if item.get("previous_filename"):
                print(item["previous_filename"])
    elif endpoint.count("/") == 2:
        if jq_arg() != ".default_branch":
            refuse("the repo read is not the default branch")
        if "repo" in fail:
            broken()
        print(state.get("default", "main"))
    else:
        refuse("unexpected endpoint")
elif args[:2] == ["pr", "view"]:
    fields = args[args.index("--json") + 1] if "--json" in args else ""
    if fields == "statusCheckRollup":
        if jq_arg() != ROLLUP_JQ:
            refuse("the rollup read is not the newest run's conclusion")
        if "rollup" in fail:
            broken()
        runs = [r for r in state.get("rollup", []) if r["name"] == "rollout-check / Rollout check"]
        if any(r.get("status") != "COMPLETED" for r in runs):
            print("PENDING")
        else:
            # As jq sorts: a null startedAt (or none) before any string, stably.
            runs.sort(key=lambda r: (r.get("startedAt") is not None, r.get("startedAt") or ""))
            conclusion = runs[-1].get("conclusion") if runs else None
            print("ABSENT" if conclusion is None or conclusion is False else conclusion)
    elif fields == "closingIssuesReferences":
        if jq_arg() != ".closingIssuesReferences[].number":
            refuse("the closes read is not the closing issue numbers")
        if "closes" in fail:
            broken()
        for number in state.get("closes", []):
            print(number)
    else:
        refuse("unexpected pr view")
elif args[:2] == ["issue", "list"]:
    if args[args.index("--label") + 1:][:1] != ["rollout-hold"] or "open" not in args:
        refuse("the hold listing is not the open rollout-hold issues")
    if "holds" in fail:
        broken()
    for number in state.get("holds", []):
        print(number)
else:
    refuse("unexpected call")
