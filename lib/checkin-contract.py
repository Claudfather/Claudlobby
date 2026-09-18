#!/usr/bin/env python3
# lib/checkin-contract.py
"""The check-in decision record contract (manager check-in spec §7), schema 1.

Stdlib only (the dispatch-overdue.py precedent): checkin-record.sh pipes the
manager's decision JSON through `normalize` before anything reaches the plane,
so a malformed decision is refused AT THE DOOR with every reason named, never
landed as a row no reader can join.

    python3 checkin-contract.py --checkin-id ck_<32hex> < decision.json
    exit 0 ok (normalized JSON on stdout) / 2 contract violation (reasons on stderr)

The DOOR mints the id (lib-common's plane_mint_id, the one mint every door
uses); this module validates its shape and never mints. Schema 1 is the record
ONE hand-equipped manager can produce this chunk: actions dispatch | ask |
nothing. Later chunks ADD (propose, sprint, focus fields) as schema 2. Every
count in `inputs_seen` and `delta` is int | null -- null means "could not
measure" and is never collapsed to 0, because an unchanged delta is the skill's
primary argument for silence -- and a MISSING count is a defect, not a 0.
`inputs_seen.considered` is the losers list: a selector can only be judged
against what it did NOT pick (lib/sprint-selection-record.py, #974), so on
`dispatch` it must be non-empty, and on `nothing` whenever `issues_seen` is a
positive count (the nothing rows are the population an inert verdict is
diagnosed from); `issues_seen` beside `issues_considered` is
that module's second rule -- the raw count (capped at READ 4's per-repo limit,
so 0 vs non-zero is what it discriminates) beside the filtered one, so a broken
FILTER and an empty backlog do not produce the same row; a broken QUERY is
separated by `inputs_seen.unavailable`, the module's third rule (provenance), so
both list keys must be PRESENT, empty allowed, never defaulted. `raise.decided`
true requires action ask (--raised counts on it); with `mission` unavailable,
`issues_considered` must be null. `prev_checkin_id` is required
(null = READ 0 answered "none") so a skipped read cannot pose as a first check-in.
"""
from __future__ import annotations

import json
import re
import sys

SCHEMA = 1
ACTIONS = ("dispatch", "ask", "nothing")
TEXT_MAX = 600
LIST_MAX = 10
ITEM_MAX = 200
INPUTS_COUNTS = ("open_tasks", "stalls", "unacked", "issues_seen", "issues_considered", "knowledge_hits")
INPUTS_LISTS = ("considered", "unavailable")
DELTA_COUNTS = ("tasks_opened", "tasks_completed", "stalls_appeared", "stalls_cleared",
                "issues_new", "messages_new", "held_pending")
ID_RE = re.compile(r"^ck_[0-9a-f]{32}$")     # the one spelling: plane_mint_id ck
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")   # a projects.yaml key


class ContractError(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _str_list(v) -> bool:
    return (isinstance(v, list) and len(v) <= LIST_MAX
            and all(isinstance(s, str) and len(s) <= ITEM_MAX for s in v))


def _text(v) -> bool:
    return isinstance(v, str) and v.strip() != "" and len(v) <= TEXT_MAX


def normalize(obj, *, checkin_id: str | None = None) -> dict:
    """Return the schema-1 record, or raise ContractError listing EVERY defect."""
    if not isinstance(obj, dict):
        raise ContractError(["decision must be a JSON object"])
    bad: list[str] = []
    out: dict = {"schema": SCHEMA}

    cid = checkin_id or obj.get("checkin_id")
    if not cid:
        bad.append("checkin_id required (the door mints it: plane_mint_id ck)")
    elif not ID_RE.match(str(cid)):
        bad.append("checkin_id must be ck_<32 hex>")
    out["checkin_id"] = cid
    if "prev_checkin_id" not in obj:
        bad.append("prev_checkin_id required (null = READ 0 answered: no previous check-in)")
    prev = obj.get("prev_checkin_id")
    if prev is not None and not ID_RE.match(str(prev)):
        bad.append("prev_checkin_id must be ck_<32 hex> or null")
    out["prev_checkin_id"] = prev

    seen = obj.get("inputs_seen")
    if not isinstance(seen, dict):
        bad.append("inputs_seen must be an object")
        seen = {}
    out["inputs_seen"] = {}
    for k in INPUTS_COUNTS:
        if k not in seen:
            bad.append(f"inputs_seen.{k} required (a non-negative integer, or null = could not measure)")
            out["inputs_seen"][k] = None
            continue
        v = seen[k]
        if v is not None and not _count(v):
            bad.append(f"inputs_seen.{k} must be a non-negative integer or null (null = could not measure)")
        out["inputs_seen"][k] = v
    for k in INPUTS_LISTS:
        if k not in seen:
            bad.append(f"inputs_seen.{k} required (a list, empty allowed; a missing list is a defect, not an empty one)")
            out["inputs_seen"][k] = []
            continue
        v = seen[k]
        if not _str_list(v):
            bad.append(f"inputs_seen.{k} must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
        out["inputs_seen"][k] = v

    delta = obj.get("delta")
    if not isinstance(delta, dict):
        bad.append("delta must be an object")
        delta = {}
    out["delta"] = {}
    for k in DELTA_COUNTS:
        if k not in delta:
            bad.append(f"delta.{k} required (a non-negative integer, or null = could not measure)")
            out["delta"][k] = None
            continue
        v = delta[k]
        if v is not None and not _count(v):
            bad.append(f"delta.{k} must be a non-negative integer or null (null = could not measure)")
        out["delta"][k] = v

    action = obj.get("action")
    if action not in ACTIONS:
        bad.append(f"action must be one of {', '.join(ACTIONS)}")
    out["action"] = action

    pk = obj.get("project_key")
    if pk is not None and not (isinstance(pk, str) and SLUG_RE.match(pk)):
        bad.append("project_key must be a projects.yaml slug or null")
    out["project_key"] = pk
    if action == "dispatch" and pk is None:
        bad.append("action dispatch must name project_key")
    considered_v = out["inputs_seen"]["considered"]
    considered_is_list = isinstance(considered_v, list)
    if action == "dispatch" and considered_is_list and not considered_v:
        bad.append("action dispatch needs inputs_seen.considered non-empty (a selector is judged by what it did NOT pick)")
    seen_n = out["inputs_seen"].get("issues_seen")
    if action == "nothing" and isinstance(seen_n, int) and seen_n > 0 and considered_is_list and not considered_v:
        bad.append("action nothing with issues_seen > 0 needs inputs_seen.considered non-empty (what was there, and why it was passed over)")

    rationale = obj.get("rationale")
    if not _text(rationale):
        bad.append(f"rationale must be a non-empty string, rationale must be <= {TEXT_MAX} characters")
    out["rationale"] = rationale

    raise_ = obj.get("raise")
    if not isinstance(raise_, dict):
        bad.append("raise must be an object")
        raise_ = {}
    decided = raise_.get("decided", False)
    if not isinstance(decided, bool):
        bad.append("raise.decided must be true or false")
    reason = raise_.get("reason")
    if not _text(reason):
        bad.append(f"raise.reason must be a non-empty string (why it surfaced, or why not), raise.reason must be <= {TEXT_MAX} characters")
    held = raise_.get("held", [])
    if not _str_list(held):
        bad.append(f"raise.held must be a list of <= {LIST_MAX} strings of <= {ITEM_MAX} chars")
    out["raise"] = {"decided": decided, "reason": reason, "held": held}
    if action == "ask" and decided is not True:
        bad.append("action ask requires raise.decided = true (an ask IS a surfacing)")
    if decided is True and action != "ask":
        bad.append("raise.decided true requires action ask (an ask IS the surfacing; --raised counts on it)")
    unav = out["inputs_seen"]["unavailable"]
    unav = unav if isinstance(unav, list) else []
    if "mission" in unav and out["inputs_seen"]["issues_considered"] is not None:
        bad.append("issues_considered must be null when mission is unavailable (a count filtered by a mission nobody read is fabricated)")
    if "checkins" in unav and out.get("prev_checkin_id") is not None:
        bad.append("prev_checkin_id must be null when checkins is unavailable (a previous id nobody read is fabricated)")

    if bad:
        raise ContractError(bad)
    return out


def main(argv: list[str]) -> int:
    checkin_id = None
    if len(argv) == 3 and argv[1] == "--checkin-id":
        checkin_id = argv[2]
    elif len(argv) != 1:
        print("usage: checkin-contract.py [--checkin-id ck_<32hex>] < decision.json", file=sys.stderr)
        return 2
    try:
        obj = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"checkin-contract: not JSON: {exc}", file=sys.stderr)
        return 2
    try:
        out = normalize(obj, checkin_id=checkin_id)
    except ContractError as exc:
        for r in exc.reasons:
            print(f"checkin-contract: {r}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
