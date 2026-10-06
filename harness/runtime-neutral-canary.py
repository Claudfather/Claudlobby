#!/usr/bin/env python3
"""P0 canaries for the runtime-neutral observability epic (#2145): the Claude batch and the Codex batch.

Three canaries gate the first PRs (epic §6 P0, §10 order 1):

- **C6a**, F4's confirmation gate: Claude Code exports OTLP/HTTP-JSON logs and
  metrics straight to a local endpoint, with no Collector between it and the
  intake. Also: does the exporter's ``session.id`` equal the hook ``session_id``,
  across ``--resume`` too? Do the ``OTEL_RESOURCE_ATTRIBUTES`` keys land? Is the
  temporality delta? Does the exporter buffer through a short receiver outage?
  What do the summarizer's ``claude -p`` children export?
- **C10**: does ``CLAUDE_CODE_CHILD_SESSION`` reach a bot whose tmux server was
  started from inside a Claude session? This decides plan 2 Task 7b.
- **C11**: does the Bash tool's ``CLAUDE_CODE_SESSION_ID`` equal the SessionStart
  ``session_id``, from the main thread and from an Agent subagent? And what env
  does the hook process itself carry for a subagent's ``PostToolUse``?

Verbs:

``receiver --port P --out DIR``
    A raw OTLP/HTTP-JSON receiver on 127.0.0.1, the shape the ``plane-otel``
    intake takes (answers 200 before anything else). It records the *shape* of
    each export (metric names, temporality, attribute keys, event names) and
    the values of a short allowlist of id and enum keys. Never a prompt, a tool
    input or an output.
``hook --log FILE [--stdout TEXT] [--hold MS]``
    For a hook command, under either runtime: appends one line per payload. It
    keeps ids, event names, every payload key's *name and type* (never its
    value, unless it is an id or enum key), the names of the process's
    ``CLAUDE_CODE_*`` and ``CODEX_*`` variables (values only for the id and
    marker variables), and the process's ancestors' command names (the owner
    process, C2). ``--stdout`` prints TEXT for the runtime to read (C3's
    injection nonce, or a JSON decision). ``--hold`` marks the line with MS,
    sleeps MS milliseconds and then appends a second ``held`` line with the same
    pid, so a killed hook shows as a hold with no ``held`` line (C1's
    ``SessionEnd`` budget). It always exits 0.
``setup --dir D --port P``
    Writes a throwaway settings file wiring ``hook`` to every event the
    canaries need, plus the bot env block of epic §6 P2. Then it prints the
    steps.
``c10 --out DIR``
    Starts throwaway tmux servers the way ``start-bot.sh`` does (``tmux -L
    <socket> new-session -d``) and records which Claude markers reach the pane.
    It runs a bare arm and an arm with the ``env -u`` scrub Task 7b would add.
    It needs tmux and nothing else, and it kills every server it starts.
``report DIR``
    Prints a verdict per measured leg, ready to paste into the run log.

A measurement instrument: nothing here runs in production.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shlex
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: Attribute keys whose *values* are kept: ids, enums and versions. Every other value is
#: recorded as its type and length only, so a prompt or a tool input can never land in a log.
VALUE_KEYS = frozenset({
    "session.id", "service.name", "service.version", "event.name", "terminal.type",
    "os.type", "host.arch", "model", "tool_name", "tool_use_id", "decision", "success", "type",
    "source", "agent.runtime", "bot.name", "fleet.name", "claudlobby.bot", "claudlobby.fleet",
    "app.version", "query_source", "error", "status_code", "claudlobby.content",
})
#: Placeholders an exporter writes in place of withheld content. Kept verbatim, so a report can tell a
#: redacted attribute from one carrying text without the receiver ever storing the text.
REDACTION_MARKERS = frozenset({"<REDACTED>", "[REDACTED]", "REDACTED"})
#: Runtime markers whose values the hook and the C10 probe keep. They are ids or flags.
ENV_VALUES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
              "CODEX_SESSION_ID", "CODEX_THREAD_ID")
#: Runtime marker prefixes whose variable *names* a hook records.
ENV_PREFIXES = ("CLAUDE_CODE_", "CODEX_")
#: Hook payload fields worth keeping. Anything else (a prompt, a tool input or output) is dropped.
PAYLOAD_KEPT = ("hook_event_name", "session_id", "source", "reason", "trigger", "tool_name", "tool_use_id",
                "agent_id", "agent_type", "turn_id")
HOOK_EVENTS = ("SessionStart", "SessionEnd", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop")
TEMPORALITY = {0: "unspecified", 1: "delta", 2: "cumulative"}
OTLP_PATHS = ("/v1/logs", "/v1/metrics", "/v1/traces")


# --- shape extraction: pure, tested ---------------------------------------------------------------

def _any_value(value: dict):
    """An OTLP AnyValue as (type, python value)."""
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in value:
            return kind, value[kind]
    return next(iter(value), "empty"), None


def attrs_shape(attributes: list) -> dict:
    """Attribute list → {key: value} for allowlisted keys, {key: "<type>:<len>"} for every other."""
    out = {}
    for attr in attributes or []:
        key = attr.get("key", "")
        kind, value = _any_value(attr.get("value") or {})
        if key in VALUE_KEYS and value is not None:
            out[key] = value
        elif value in REDACTION_MARKERS:  # the exporter's own placeholder: kept, it is the measurement
            out[key] = value
        else:
            out[key] = f"{kind}:{len(str(value)) if value is not None else 0}"
    return out


def metrics_shape(doc: dict) -> list:
    """One row per (resource, metric): name, kind, temporality, datapoint count and attribute keys."""
    rows = []
    for rm in doc.get("resourceMetrics", []):
        resource = attrs_shape((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                kind = next((k for k in ("sum", "gauge", "histogram", "exponentialHistogram") if k in metric), "?")
                body = metric.get(kind) or {}
                points = body.get("dataPoints", [])
                rows.append({
                    "metric": metric.get("name"), "kind": kind,
                    "temporality": TEMPORALITY.get(body.get("aggregationTemporality"), body.get("aggregationTemporality")),
                    "points": len(points), "resource": resource,
                    "point_attrs": sorted({a.get("key") for p in points for a in p.get("attributes", [])}),
                    "session_ids": sorted({str(_any_value(a.get("value") or {})[1]) for p in points
                                           for a in p.get("attributes", []) if a.get("key") == "session.id"}),
                })
    return rows


def logs_shape(doc: dict, received: float) -> list:
    """One row per log record: event name, attribute shape and the export lag (receive − event time)."""
    rows = []
    for rl in doc.get("resourceLogs", []):
        resource = attrs_shape((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                attrs = attrs_shape(rec.get("attributes"))
                body = (rec.get("body") or {}).get("stringValue")
                # The body is kept only when it reads as an event name; otherwise its length.
                body_name = body if body and len(body) < 80 and " " not in body else None
                name = attrs.get("event.name") or body_name
                t_event = int(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano") or 0) / 1e9
                rows.append({"event": name, "body": body_name if body_name or body is None else f"string:{len(body)}",
                             "attrs": attrs, "resource": resource,
                             "lag_s": round(received - t_event, 3) if t_event else None})
    return rows


def env_markers(env: dict) -> dict:
    """The runtime markers in an environment: names of every ``CLAUDE_CODE_*``/``CODEX_*``, values for the id/flag ones."""
    names = sorted(k for k in env if k.startswith(ENV_PREFIXES) or k == "CLAUDECODE")
    return {"names": names, "values": {k: env[k] for k in ENV_VALUES if k in env}}


def payload_shape(payload: dict) -> dict:
    """Every payload key's type (and a string's length): the field names C1 asks for, with no values."""
    def shape(value):
        if isinstance(value, str):
            return f"str:{len(value)}"
        return type(value).__name__
    return {k: shape(v) for k, v in sorted(payload.items())}


def ancestors(pid: int, depth: int = 8, table: str | None = None) -> list:
    """``[{pid, comm}]`` from ``pid``'s parent upwards: the owner-process walk C2 asks for (Linux and macOS).

    One ``ps -A -o pid=,ppid=,comm=`` snapshot (both ``ps`` take it), so the walk costs one process and a ``ps``
    that fails or times out costs only the chain. ``comm`` is kept as its basename: macOS prints the full path,
    which can name the user. pid 1 is recorded (a container's runtime can be it) and ends the walk.
    """
    if table is None:
        try:
            table = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True,
                                   timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    procs = {}
    for row in table.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].rstrip("/")))
    chain = []
    pid = procs.get(pid, (0, ""))[0]
    while pid >= 1 and pid in procs and len(chain) < depth:
        ppid, comm = procs[pid]
        chain.append({"pid": pid, "comm": comm})
        pid = ppid if ppid != pid else 0
    return chain


def hook_record(payload: dict, env: dict, now: float, *, lineage: list | None = None) -> dict:
    """The log line for one hook payload: ids and names only, never conversation text."""
    row = {"ts": now, **{k: payload[k] for k in PAYLOAD_KEPT if payload.get(k) is not None},
           "keys": payload_shape(payload), "env": env_markers(env), "pid": os.getpid(), "ppid": os.getppid()}
    if lineage is not None:
        row["ancestors"] = lineage
    return row


# --- receiver --------------------------------------------------------------------------------------

def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def make_handler(out: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's name
            received = time.time()
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            # Answer first, as the intake does: the exporter never waits on the write.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            ctype = self.headers.get("Content-Type", "")
            meta = {"ts": received, "path": self.path, "bytes": len(raw), "content_type": ctype,
                    "encoding": self.headers.get("Content-Encoding")}
            try:
                body = gzip.decompress(raw) if meta["encoding"] == "gzip" else raw
                doc = json.loads(body)
            except (OSError, ValueError):
                _append(out / "requests.jsonl", {**meta, "json": False})
                return
            _append(out / "requests.jsonl", {**meta, "json": True})
            if self.path.endswith("/v1/metrics"):
                for row in metrics_shape(doc):
                    _append(out / "metrics.jsonl", {"ts": received, **row})
            elif self.path.endswith("/v1/logs"):
                for row in logs_shape(doc, received):
                    _append(out / "logs.jsonl", {"ts": received, **row})
            else:
                _append(out / "traces.jsonl", {"ts": received, "spans": sum(
                    len(ss.get("spans", [])) for rs in doc.get("resourceSpans", []) for ss in rs.get("scopeSpans", []))})

        def log_message(self, *args):  # quiet
            pass
    return Handler


def cmd_receiver(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(out))
    _append(out / "receiver.jsonl", {"ts": time.time(), "up": True, "pid": os.getpid(), "port": args.port})
    print(f"receiver on http://127.0.0.1:{args.port} → {out}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _append(out / "receiver.jsonl", {"ts": time.time(), "up": False, "pid": os.getpid()})
    return 0


# --- hook ------------------------------------------------------------------------------------------

def cmd_hook(args) -> int:
    try:
        line = hook_record(json.loads(sys.stdin.read() or "{}"), dict(os.environ), time.time(),
                           lineage=ancestors(os.getpid()))
    except Exception as exc:  # noqa: BLE001 - a canary must never break a session
        line = {"ts": time.time(), "hook_event_name": "canary-error", "error": repr(exc), "pid": os.getpid()}
    if args.hold:
        line["hold"] = args.hold  # a hold asked for: its `held` line, by pid, says whether it finished
    try:
        _append(Path(args.log), line)
    except OSError:
        pass
    if args.stdout:
        print(args.stdout, flush=True)
    if args.hold:
        time.sleep(args.hold / 1000)
        try:
            _append(Path(args.log), {"ts": time.time(), "held": args.hold, "pid": line["pid"],
                                     "hook_event_name": line.get("hook_event_name"),
                                     "session_id": line.get("session_id")})
        except OSError:
            pass
    return 0


# --- setup -----------------------------------------------------------------------------------------

def env_block(port: int, bot: str, fleet: str = "canary") -> dict:
    """The bot env block of epic §6 P2 for ``content: metadata``, pointed at the canary receiver."""
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
        "OTEL_RESOURCE_ATTRIBUTES": f"agent.runtime=claude,claudlobby.bot={bot},claudlobby.fleet={fleet}",
    }


def cmd_setup(args) -> int:
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    me = shlex.quote(str(Path(__file__).resolve()))
    hook = f"python3 {me} hook --log {shlex.quote(str(root / 'hooks.jsonl'))}"
    settings = {"hooks": {ev: [{"hooks": [{"type": "command", "command": hook}]}] for ev in HOOK_EVENTS}}
    (root / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    env = env_block(args.port, args.bot, args.fleet)
    (root / "otel.env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()))
    print(f"""Wrote {root}/settings.json (hooks → {root}/hooks.jsonl) and {root}/otel.env.

1. Terminal A:  python3 {me} receiver --port {args.port} --out {root}/otlp
2. Terminal B:  . {root}/otel.env && claude --settings {root}/settings.json
   (on the canary bot: add the otel.env lines to the bot's env tier and the hooks to its settings instead)
3. In the session, send exactly:
     Run this Bash command: env | grep ^CLAUDE_CODE_ | grep -E 'SESSION_ID|CHILD' > {root}/bash-main.env
     Then use the Agent tool with a general-purpose subagent to run exactly this Bash command:
     env | grep ^CLAUDE_CODE_ | grep -E 'SESSION_ID|CHILD' > {root}/bash-sub.env
4. Wait 70 s (one metric export interval), then /exit.
5. claude --settings {root}/settings.json --resume <the session id from hooks.jsonl>; send "ok"; wait 70 s; /exit.
6. python3 {me} report {root}
""")
    return 0


# --- c10 -------------------------------------------------------------------------------------------

def c10_arm(out: Path, name: str, env: dict, scrub: bool) -> dict:
    """One throwaway tmux server started the way start-bot.sh starts a bot's, from ``env``."""
    socket = f"rnc-c10-{name}-{os.getpid()}"
    dump = out / f"c10-{name}.env"
    inner = f"env > {shlex.quote(str(dump))}"
    if scrub:  # the Task 7b shape: unset before `exec $CLAUDE`
        inner = "unset CLAUDE_CODE_CHILD_SESSION; " + inner
    try:
        subprocess.run(["tmux", "-L", socket, "new-session", "-d", "-s", "bot", inner + "; sleep 5"],
                       env=env, check=True, timeout=10)
        deadline = time.time() + 5
        while not dump.exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], env=env, capture_output=True, timeout=10)
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line) if dump.exists() else {}
    dump.unlink(missing_ok=True)  # the full env holds tokens; only the markers are kept
    return {"arm": name, "scrub": scrub, "caller": env_markers(env)["values"], "pane": env_markers(seen)["values"],
            "pane_saw_env": bool(seen)}


def cmd_c10(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    caller = dict(os.environ)
    if "CLAUDE_CODE_CHILD_SESSION" not in caller:
        print("note: this shell carries no CLAUDE_CODE_CHILD_SESSION; run c10 from a Claude Code Bash tool "
              "to measure the leak (a bare shell measures only the no-marker baseline)", file=sys.stderr)
    rows = [c10_arm(out, "bare", caller, scrub=False), c10_arm(out, "scrubbed", caller, scrub=True)]
    for row in rows:
        _append(out / "c10.jsonl", {"ts": time.time(), **row})
        print(json.dumps(row, sort_keys=True))
    return 0


# --- report ----------------------------------------------------------------------------------------

def _read(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def report(root: Path) -> dict:
    """Verdicts per measured leg, from whatever the run left in ``root``."""
    hooks, holds, open_hold = [], [], {}
    for h in _read(root / "hooks.jsonl"):
        if "held" in h:  # `--hold`'s second line: it closes the latest open hold of the same pid
            row, started = open_hold.pop(h.get("pid"), (None, 0))
            if row is not None:
                row.update(completed=True, held_after_s=round(h["ts"] - started, 3))
            continue
        hooks.append(h)
        if h.get("hold"):
            row = {"event": h.get("hook_event_name"), "session_id": h.get("session_id"), "hold_ms": h["hold"],
                   "completed": False, "held_after_s": None}
            open_hold[h.get("pid")] = (row, h["ts"])
            holds.append(row)
    requests = _read(root / "otlp" / "requests.jsonl")
    metrics = _read(root / "otlp" / "metrics.jsonl")
    logs = _read(root / "otlp" / "logs.jsonl")
    starts = [h for h in hooks if h.get("hook_event_name") == "SessionStart"]
    hook_sids = {h["session_id"] for h in starts if h.get("session_id")}
    otel_sids = {s for m in metrics for s in m["session_ids"]} | {
        r["attrs"]["session.id"] for r in logs if isinstance(r["attrs"].get("session.id"), str)}
    out = {
        "requests": {p: sum(1 for r in requests if r["path"].endswith(p)) for p in OTLP_PATHS},
        "all_json": all(r.get("json") for r in requests) if requests else None,
        "content_types": sorted({r["content_type"] for r in requests}),
        "metric_names": sorted({m["metric"] for m in metrics}),
        "temporality": sorted({str(m["temporality"]) for m in metrics}),
        "event_names": sorted({str(r["event"]) for r in logs}),
        "resource_keys": sorted({k for row in metrics + logs for k in row["resource"]}),
        "resource_attrs_on_every_row": all(
            {"agent.runtime", "claudlobby.bot"} <= set(row["resource"]) for row in metrics + logs) if metrics or logs else None,
        "hook_session_ids": sorted(hook_sids),
        "otel_session_ids": sorted(otel_sids),
        "otel_ids_matching_a_hook": sorted(otel_sids & hook_sids),
        "otel_ids_without_a_hook": sorted(otel_sids - hook_sids),
        "sources": sorted({h.get("source", "") for h in starts}),
        "max_log_lag_s": max((r["lag_s"] for r in logs if r["lag_s"] is not None), default=None),
    }
    for name in ("bash-main", "bash-sub"):
        path = root / f"{name}.env"
        if path.exists():
            env = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
            out[name] = env
            out[f"{name}_matches_start"] = env.get("CLAUDE_CODE_SESSION_ID") in hook_sids
    sub_hooks = [h for h in hooks if h.get("agent_id") and h.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure")]
    out["subagent_tool_hooks"] = [{"event": h["hook_event_name"], "session_id": h.get("session_id"),
                                   "env": h["env"]["values"]} for h in sub_hooks]
    main_hooks = [h for h in hooks if not h.get("agent_id") and h.get("hook_event_name") == "PostToolUse"]
    out["main_tool_hooks_env"] = sorted({json.dumps(h["env"]["values"], sort_keys=True) for h in main_hooks})
    out["c10"] = _read(root / "c10.jsonl")
    events = [h for h in hooks if "keys" in h]
    out["payload_keys_by_event"] = {
        ev: sorted({k for h in events if h.get("hook_event_name") == ev for k in h["keys"]})
        for ev in sorted({str(h.get("hook_event_name")) for h in events})}
    out["env_values_seen"] = sorted({json.dumps(h["env"]["values"], sort_keys=True) for h in events})
    out["env_names_seen"] = sorted({n for h in events for n in h["env"]["names"]})
    out["ancestor_comms"] = sorted({tuple(a["comm"] for a in h["ancestors"]) for h in events if h.get("ancestors")})
    out["holds"] = holds
    return out


def cmd_report(args) -> int:
    print(json.dumps(report(Path(args.dir)), indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("receiver")
    p.add_argument("--port", type=int, default=4319)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_receiver)
    p = sub.add_parser("hook")
    p.add_argument("--log", required=True)
    p.add_argument("--stdout", default="", help="text to print for the runtime to read (C3)")
    p.add_argument("--hold", type=int, default=0, help="sleep MS, then log a 'held' line (C1's SessionEnd budget)")
    p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("setup")
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=4319)
    p.add_argument("--bot", default="canary")
    p.add_argument("--fleet", default="canary", help="the claudlobby.fleet label (a borrowed bot's real fleet)")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("c10")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_c10)
    p = sub.add_parser("report")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_report)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
