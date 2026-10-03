#!/usr/bin/env python3
"""A stand-in for `gh api`, for the rollout check's tests (#2111).

It answers the three reads the checker makes from a JSON state file
($FAKE_GH_STATE): the PR body, the PR's changed files, and the caller file on the
default branch. Each call's arguments are appended to $FAKE_GH_LOG, one call per
line, so a test can say which reads were made and with what. It prints what each
read's --jq would, and refuses (exit 2) a read whose --jq or pagination is not
the checker's (#2116 review): it cannot evaluate them, so without the pin a
checker that read the title, or only the first page of files, would pass here.
"""

import json
import os
import re
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_GH_STATE"], encoding="utf-8") as fh:
    state = json.load(fh)
fail = state.get("fail") or {}

if not args or args[0] != "api":
    print(f"fake gh: unexpected call {args}", file=sys.stderr)
    sys.exit(2)
endpoint = next((a for a in args if a.startswith("repos/")), "")

if "/contents/" in endpoint:
    if fail.get("contents"):
        print(f"gh: Server Error (HTTP {fail['contents']})", file=sys.stderr)
        sys.exit(1)
    if state.get("base_caller") is None:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(state["base_caller"])
elif re.search(r"/pulls/\d+/files(\?|$)", endpoint):
    if "--paginate" not in args or ".[] | .filename, (.previous_filename // empty)" not in args:
        print(f"fake gh: the file read is not every page of filenames and previous names: {args}",
              file=sys.stderr)
        sys.exit(2)
    if fail.get("files"):
        print("gh: Server Error (HTTP 502)", file=sys.stderr)
        sys.exit(1)
    for item in state.get("files", []):
        print(item["filename"])
        if item.get("previous_filename"):
            print(item["previous_filename"])
elif re.search(r"/pulls/\d+$", endpoint):
    if '.body // ""' not in args:
        print(f"fake gh: the body read is not the PR body: {args}", file=sys.stderr)
        sys.exit(2)
    if fail.get("body"):
        print("gh: Server Error (HTTP 502)", file=sys.stderr)
        sys.exit(1)
    print(state.get("body") or "")
else:
    print(f"fake gh: unexpected endpoint {endpoint!r}", file=sys.stderr)
    sys.exit(2)
