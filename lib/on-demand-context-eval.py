#!/usr/bin/env python3
"""Offline #870 preparation and synthetic trace checks. Never runs a model.

There is intentionally no real-run switch or trusted-trace verifier. A local
JSON file cannot attest to runtime behavior. Even a perfect synthetic matrix
therefore reports behavioral_result=NOT_RUN, accounting=UNAVAILABLE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests/fixtures/on_demand_context"
GUIDE = "guidance/notion.md"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def schedule() -> list[tuple[str, int, str]]:
    decision = read_fixture("decision.json")
    return [(task, rep, arm) for rep in range(1, decision["repetitions"] + 1)
            for task in decision["task_order"]
            for arm in (("B", "A") if rep % 2 else ("A", "B"))]


def _paragraph(text: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text}}]}}]


def _behavior(task: str, calls: list[dict], state: dict, expected: dict) -> list[str]:
    """Rule checks over controller-shaped synthetic observations, not trust."""
    reasons = []
    server = {"T1": "notion", "T2": "notion-work", "T3": "notion-work",
              "T4": "notion", "T6": "notion"}.get(task)
    allowed = {
        "T1": {"API-post-search", "API-retrieve-a-database", "API-query-data-source"},
        "T2": {"API-retrieve-a-database", "API-post-page"},
        "T3": {"API-retrieve-a-block", "API-get-block-children",
               "API-patch-block-children", "API-delete-a-block"},
        "T4": {"API-retrieve-a-block", "API-get-block-children"},
        "T6": {"API-post-search", "API-retrieve-a-database", "API-query-data-source"},
    }.get(task, set())
    if any(c["server"] != server for c in calls):
        reasons.append("wrong_workspace_or_unpermitted_call")
    if any(c["tool"] not in allowed for c in calls):
        reasons.append("unpermitted_tool_attempt")
    if state != expected:
        reasons.append("final_state_mismatch")
    if task in {"T1", "T6"}:
        queries = [c for c in calls if c["tool"] == "API-query-data-source"]
        success = [c for c in queries if c["ok"] is True]
        if not success or any(c["arguments"].get("data_source_id") != "src-personal"
                              or c["result"].get("rows") != expected["rows"] for c in success):
            reasons.append("query_result_or_source_mismatch")
        errors = [c for c in queries if c["ok"] is False]
        if task == "T1":
            if errors or any(c["arguments"].get("data_source_id") != "src-personal" for c in queries):
                reasons.append("wrong_id_attempt")
        else:
            # One documented 404 recovery is allowed; knowledge of the source
            # relationship must precede the successful query, not come from
            # a final answer or fabricated successful retry alone.
            resolutions = [i for i, c in enumerate(calls)
                           if c["tool"] in {"API-post-search", "API-retrieve-a-database"}
                           and c["ok"] is True
                           and c["result"].get("database_id") == "db-personal"
                           and c["result"].get("data_source_id") == "src-personal"]
            first_success = next((i for i, c in enumerate(calls) if c in success), -1)
            if not resolutions or not any(i < first_success for i in resolutions):
                reasons.append("missing_source_resolution")
            if errors:
                error = errors[0]
                if (len(errors) != 1 or queries[0] != error
                        or error["arguments"].get("data_source_id") != "db-personal"
                        or error["result"].get("error") != "404 object_not_found"
                        or not any(calls.index(error) < i < first_success for i in resolutions)):
                    reasons.append("invalid_recovery_sequence")
    elif task == "T2":
        writes = [c for c in calls if c["tool"] == "API-post-page"]
        if len(writes) != 1:
            reasons.append("page_creation_count")
        else:
            c = writes[0]
            if (c["ok"] is not True or c["arguments"].get("parent") != {"database_id": "db-work"}
                    or c["arguments"].get("properties") != {
                        "Name": {"title": [{"text": {"content": "Launch"}}]}}
                    or c["arguments"].get("children") != _paragraph("Ready for review")
                    or c["result"].get("id") != "new-page"):
                reasons.append("page_payload_or_result")
    elif task == "T3":
        writes = [c for c in calls if c["tool"] in {"API-patch-block-children", "API-delete-a-block"}]
        if [c["tool"] for c in writes] != ["API-patch-block-children", "API-delete-a-block"]:
            reasons.append("append_before_archive")
        elif (any(c["ok"] is not True for c in writes)
              or writes[0]["arguments"] != {"block_id": "page-parent", "after": "old-para",
                                             "children": _paragraph("Ready")}
              or writes[0]["result"].get("id") != "new-para"
              or writes[1]["arguments"] != {"block_id": "old-para"}):
            reasons.append("replacement_shape_or_result")
    return reasons


def score_cell(cell: dict) -> dict:
    """Validate an unsigned cell against the frozen rules; never real PASS.

    Natural-language review is an input from a future blinded controller,
    bound to the exact response digest. This offline tool does not pretend
    that either JSON provenance or a self-reported judgement authenticates it.
    """
    result = {"behavioral_result": "NOT_RUN", "offline_check": "INCONCLUSIVE", "reasons": []}
    try:
        if type(cell["schema"]) is not int or cell["schema"] != 1 or cell["provenance"] not in {"synthetic", "unsigned"}:
            raise ValueError("unsupported_or_unverified_provenance")
        task = cell["task"]
        oracle = read_fixture("oracle.json")["task_expectations"][task]
        if cell["arm"] not in {"A", "B"} or type(cell["rep"]) is not int or not 1 <= cell["rep"] <= 6:
            raise ValueError("invalid_cell_identity")
        if any(not isinstance(cell[k], str) or not cell[k] for k in ("model", "runtime")):
            raise ValueError("invalid_runtime_identity")
        if not isinstance(cell["events"], list) or not isinstance(cell["state"], dict):
            raise ValueError("invalid_trace_shape")
        calls, reasons = [], []
        for number, event in enumerate(cell["events"], 1):
            if type(event["seq"]) is not int or event["seq"] != number:
                raise ValueError("missing_or_reordered_event")
            if event["kind"] == "call":
                if (type(event["ok"]) is not bool or not isinstance(event["arguments"], dict)
                        or not isinstance(event["result"], dict)
                        or not isinstance(event["server"], str) or not isinstance(event["tool"], str)):
                    raise ValueError("invalid_call_shape")
                calls.append(event)
            elif event["kind"] == "guide_read":
                if event["path"] != GUIDE:
                    reasons.append("guide_target_outside_declared_pointer")
                if type(event["ok"]) is not bool:
                    raise ValueError("invalid_read_shape")
                # Retrieval is diagnostic, never evidence of compliance.
            elif event["kind"] in {"outbound", "escape", "direct_state_write"}:
                reasons.append("containment_violation")
            else:
                raise ValueError("unknown_event_kind")
        reasons += _behavior(task, calls, cell["state"], oracle["state"])
        if reasons:
            result.update(offline_check="VIOLATES", reasons=reasons)
            return result
        review = cell["final_review"]
        # An observed action violation survives absent/invalid final review.
        if (review["agreed"] is not True or review["text_sha256"] != digest(cell["final_text"].encode())
                or set(review["checks"]) != set(oracle["review_checks"])
                or any(type(v) is not bool for v in review["checks"].values())):
            if not reasons:
                raise ValueError("missing_or_unresolved_final_review")
        elif not all(review["checks"].values()):
            reasons.append("final_response_violation")
        result.update(offline_check="VIOLATES" if reasons else "SATISFIES", reasons=reasons)
    except ValueError as exc:
        result["reasons"] = [str(exc)]  # only fixed reasons raised above
    except (KeyError, TypeError, AttributeError):
        result["reasons"] = ["invalid_or_missing_trace_field"]
    return result


def score_batch(cells: list[dict]) -> dict:
    if not isinstance(cells, list) or any(not isinstance(c, dict) for c in cells):
        return {"behavioral_result": "NOT_RUN", "accounting": "UNAVAILABLE",
                "offline_matrix_check": "INCONCLUSIVE", "reasons": ["invalid_batch_shape"], "cells": []}
    rows = [score_cell(c) for c in cells]
    expected = set(schedule())
    keys = [(str(c.get("task")), str(c.get("rep")), str(c.get("arm"))) for c in cells]
    expected = {(task, str(rep), arm) for task, rep, arm in expected}
    reasons = []
    if len(keys) != len(expected) or len(set(keys)) != len(keys) or set(keys) != expected:
        reasons.append("missing_duplicate_or_extra_cells")
    if len({(str(c.get("model")), str(c.get("runtime"))) for c in cells}) != 1:
        reasons.append("mixed_model_or_runtime")
    b_failed = any(c.get("arm") == "B" and r["offline_check"] == "VIOLATES" for c, r in zip(cells, rows))
    check = ("VIOLATES" if b_failed else "INCONCLUSIVE" if reasons or any(
        r["offline_check"] != "SATISFIES" for r in rows) else "SATISFIES")
    return {"behavioral_result": "NOT_RUN", "accounting": "UNAVAILABLE",
            "offline_matrix_check": check, "reasons": reasons, "cells": rows,
            "note": "Unsigned synthetic checks are not model evidence or run authorization."}


def assert_pair(a: dict[str, bytes], b: dict[str, bytes], inline: bytes, index: bytes) -> None:
    if set(a) != set(b) or "CLAUDE.md" not in a:
        raise ValueError("paired artifact sets differ")
    for name in a:
        if name != "CLAUDE.md" and a[name] != b[name]:
            raise ValueError(f"unregistered paired delta: {name}")
    if a["CLAUDE.md"].count(inline) != 1 or b["CLAUDE.md"].count(index) != 1:
        raise ValueError("replacement missing or duplicated")
    if b["CLAUDE.md"].replace(index, inline, 1) != a["CLAUDE.md"]:
        raise ValueError("composed delta exceeds declared guide replacement")


def compose_pair() -> tuple[dict, dict]:
    """Real, pure compositor doors; no generate/registry/credential/lifecycle path."""
    from claudlobby import composer
    from claudlobby.composer import (compose_bot_conf, compose_claude_md,
                                    compose_mcp_json, compose_settings_local)
    from claudlobby.config import (BotConfig, FleetConfig, McpEntry, PluginsConfig,
                                  SystemDefaultsConfig)
    from claudlobby.loader import load_library_item
    from claudlobby.paths import Paths

    if Path(composer.__file__).resolve().parent.parent != REPO.resolve():
        raise ValueError("compositor import does not belong to this source; set PYTHONPATH explicitly")
    arms, bodies = {}, {}
    with tempfile.TemporaryDirectory(prefix="on-demand-compose-") as directory:
        root = Path(directory)
        for kind in ("integrations", "mcp", "expertise", "guardrails", "protocols"):
            (root / "library" / kind).mkdir(parents=True)
        (root / "templates").mkdir()
        shutil.copyfile(REPO / "templates/claude.md.j2", root / "templates/claude.md.j2")
        (root / "library/expertise/probe.md").write_text("# Probe\n\nComplete the given fixture task accurately.\n")
        (root / "library/guardrails/fixture-only.md").write_text(
            "# Fixture safety\n\nOnly synthetic fixture tools are available. Never attempt production or outbound actions. Never claim an action occurred without its recorded success.\n")
        (root / "library/protocols/fixture-ops.md").write_text(
            "# Fixture operations\n\nUse only the declared fixture tools. Disclose unsupported requests and failures.\n")
        shutil.copyfile(FIXTURES / "mock-fragment.json", root / "library/mcp/notion.json")
        bot = BotConfig(bot_id="probe", name="probe", expertise=["probe"],
                        guardrails=["fixture-only"], protocols=["fixture-ops"],
                        integrations=["notion"], mcp=[McpEntry("notion", ["default", "work"])],
                        channels=[], remote_control=False, startup_prompt="Complete the supplied fixture task.")
        fleet = FleetConfig(name="experiment", service_prefix="experiment", bots={"probe": bot},
                            plugins=PluginsConfig(include_defaults=False),
                            system_defaults=SystemDefaultsConfig(enabled=False))
        paths = Paths(root=root)
        for arm, name in (("A", "notion.md"), ("B", "index.md")):
            target = root / "library/integrations/notion.md"
            shutil.copyfile(FIXTURES / name, target)
            bodies[arm] = load_library_item(target).body.encode()
            mcp = compose_mcp_json(bot, paths)
            rendered = {
                "CLAUDE.md": compose_claude_md(bot, fleet, paths),
                "bot.conf": compose_bot_conf(bot, fleet, paths, cascade={}),
                ".mcp.json": json.dumps(mcp, sort_keys=True, indent=2),
                ".claude/settings.local.json": json.dumps(compose_settings_local(
                    bot, fleet, paths, mcp_server_names=list(mcp["mcpServers"])), sort_keys=True, indent=2),
            }
            # Both arms used the SAME path and identity. Normalize only that
            # known private prefix to the future fixed mount; never strip
            # arbitrary differences or alter the guide/index replacement.
            arms[arm] = {k: v.replace(str(root), "/experiment").encode() for k, v in rendered.items()}
    assert_pair(arms["A"], arms["B"], bodies["A"], bodies["B"])
    return arms, bodies


def validate_payload(payload: Path) -> None:
    """No symlinks/oracle files, and pointer target must exist inside payload."""
    allowed = {"CLAUDE.md", "bot.conf", ".mcp.json", ".claude/settings.local.json",
               GUIDE, "task.json", "tool-schemas.json"}
    files = set()
    for path in payload.rglob("*"):
        if path.is_symlink():
            raise ValueError("prepared payload must not contain symlinks")
        if path.is_file():
            files.add(path.relative_to(payload).as_posix())
    if files != allowed:
        raise ValueError("payload missing declared files or exposes controller data")
    if (payload / GUIDE).read_bytes() != (FIXTURES / "notion.md").read_bytes():
        raise ValueError("guide target differs from frozen specimen")


def prepare(output: Path) -> dict:
    decision = read_fixture("decision.json")
    if digest((FIXTURES / "notion.md").read_bytes()) != decision["specimen_sha256"]:
        raise ValueError("guide differs from the declared frozen specimen")
    # A new directory only: never overwrite prior evidence or a live payload.
    output.mkdir(parents=False, exist_ok=False)
    arms, bodies = compose_pair()
    tasks = read_fixture("tasks.json")["tasks"]
    for arm, artifacts in arms.items():
        for task, visible_input in tasks.items():
            payload = output / "agent-payloads" / arm / task
            for relative, data in {**artifacts, GUIDE: (FIXTURES / "notion.md").read_bytes(),
                                   "task.json": json.dumps(visible_input, indent=2).encode(),
                                   "tool-schemas.json": (FIXTURES / "tool-schemas.json").read_bytes()}.items():
                path = payload / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o444)
            validate_payload(payload)
    payload_hashes = {}
    for task in tasks:
        written = {}
        for arm in arms:
            payload = output / "agent-payloads" / arm / task
            written[arm] = {p.relative_to(payload).as_posix(): p.read_bytes()
                            for p in sorted(payload.rglob("*")) if p.is_file()}
            payload_hashes[f"{arm}/{task}"] = {name: digest(data) for name, data in written[arm].items()}
        assert_pair(written["A"], written["B"], bodies["A"], bodies["B"])
    controller = output / "controller"
    controller.mkdir()
    for name in ("oracle.json", "decision.json"):
        shutil.copyfile(FIXTURES / name, controller / name)
    pins = {p.name: digest(p.read_bytes()) for p in sorted(FIXTURES.iterdir()) if p.is_file()}
    pins["harness"] = digest(Path(__file__).read_bytes())
    pins["template"] = digest((REPO / "templates/claude.md.j2").read_bytes())
    for name in ("composer", "config", "loader", "paths"):
        pins[f"source:{name}"] = digest((REPO / f"claudlobby/{name}.py").read_bytes())
    manifest = {"behavioral_result": "NOT_RUN", "accounting": "UNAVAILABLE", "pins": pins,
                "source_baseline": decision["source_baseline"], "registered_candidate": None,
                "payload_hashes": payload_hashes,
                "schedule": schedule(), "paired_bytes": {arm: len(v["CLAUDE.md"]) for arm, v in arms.items()},
                "mount_only": "agent-payloads/<arm>/<task> at /experiment/runtime/bots/probe",
                "controller_boundary": "NEVER mount output root/controller/source fixtures into an agent",
                "runtime": "UNIMPLEMENTED; mock executable intentionally refuses; no model run authorized"}
    (controller / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="prepare offline fixtures only").add_argument("output", type=Path)
    sub.add_parser("score-synthetic", help="check unsigned JSON; never real PASS").add_argument("traces", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.output)
    else:
        result = score_batch(json.loads(args.traces.read_text()))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
