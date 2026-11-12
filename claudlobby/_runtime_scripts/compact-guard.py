#!/usr/bin/env python3
"""compact-guard.py — the request side of keepalive's compaction (#1053, build PR 4).

keepalive.sh types `/compact` into a bot's pane for a pending request, the file
`data/.compact-request` that `bot compact <its own ID>` writes (PR 2), once every
guard holds. The guards about the pane (the turn markers, the whole screen, the
send lock) and the host's compaction slot are keepalive's. This script holds the
guards about the request, takes a request away, and reads the proof.

``check --bot-dir D --root R --fleet F --bot B --transcripts DIR --loop V --reflect V``
    Prints one tab-separated line, ``verdict reason hook request_id by
    requested_at requested_epoch`` ("-" for an empty field), then one JSON line:
    the row data (the request and every reading). ``verdict`` is:

    - ``none``: no request is pending.
    - ``drop``: the request ends unsent, and this call took it away. It is 2 hours
      old or more by its ``requested_epoch`` (``request_expired``), never by the
      file's time, which a cancel's put-back renews; that field is not an integer,
      or the file cannot be read (``request_unreadable``); an assignment was
      delivered to the bot since it (``delivered_since_request``); the bot's own
      session compacted since it (``compacted_since_request``) or started after it
      (``new_session_since_request``); or, for a request made at a close (``by``
      names an ``assignment:``), the context is under the floor (``below_floor``:
      ``COMPACT_AT_CLOSE_TOKENS``, default 400000).
    - ``skip``: not now, and the request stays. A composed PreCompact hook would
      refuse the first compaction (``precompact_hook_refuses``, hook ``claudron``
      where the Claudron session loop is on and the engine its hook names reports
      a version under 0.10.0 or none it can read, hook ``claudna`` where clauDNA's
      prompt is armed); ``bot.conf`` has no ``CLAUDRON_SESSION_LOOP`` line
      (``precompact_hooks_unknown``, composed before #2799); or a fact a guard
      needs cannot be read (``plane_unread``, ``transcript_unread``,
      ``context_unread``). An unknown never reads as clear.
    - ``go``: every guard about the request holds.

    ``--loop`` and ``--reflect`` are ``bot.conf``'s ``CLAUDRON_SESSION_LOOP`` and
    ``CLAUDNA_PRECOMPACT_REFLECT`` as `bot_conf_get` reads them ("" for no line).
    The engine's version is read once per request (``data/.compact-guard-cache``),
    with a 5 s bound.

``take --bot-dir D``
    Takes the pending request away (`compact_request.take_request`: one rename,
    so of two takers one gets it) and prints the same two lines, verdict
    ``taken`` or ``none``.

``proof --transcripts DIR --since EPOCH``
    The compaction the bot's own newest session wrote at or after the send
    (`transcript_usage.compaction_since`). One line ``proven at trigger
    pre_tokens post_tokens``, ``none`` or ``unknown reason``, then the JSON.

The request and the transcript are read by the package's own stdlib modules,
`compact_request` and `transcript_usage`, loaded from this script's build (the
`plane-readers.py` rule: never an installed package or PYTHONPATH); the plane by
`plane-readers.py` beside it, read-only. A plane that holds no identity for the
fleet is a wrong root, read as unknown, never as no delivery.

Exit 0 with the two lines; 2 for a usage or load error. Stdlib only, Python 3.9;
keepalive runs it as `python3 -S -E`.

Test seams: COMPACT_GUARD_NOW (epoch seconds for the clock),
COMPACT_GUARD_DELIVERIES (a JSON file, a list of {"msg_id", "occurred_at"},
read in place of the plane), COMPACT_GUARD_VERSION_TIMEOUT_S.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MAX_AGE_S = 7200          # a request 2 hours old or more expires unsent
FLOOR_DEFAULT = 400000    # a close's request acts at this context or more (PR 5)
VERSION_TIMEOUT_S = 5.0   # the engine's `version`, read once per request
FIXED_ENGINE = (0, 10, 0)  # Claudron's hook stopped refusing in 0.10.0
CACHE_NAME = ".compact-guard-cache"
HERE = os.path.dirname(os.path.realpath(__file__))
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class GuardError(Exception):
    """The guard cannot run: a module it needs is not this build's."""


def _package_module(name: str):
    """One of the package's stdlib modules, from this script's own build."""
    if name not in {"compact_request", "transcript_usage"}:
        raise GuardError(f"unsupported package module {name}")
    if (os.path.basename(HERE) != "_runtime_scripts"
            or os.path.basename(os.path.dirname(HERE)) != "claudlobby"):
        raise GuardError("compact-guard.py is outside a Claudlobby layout")
    package = os.path.realpath(os.path.dirname(HERE))
    target = os.path.join(package, f"{name}.py")
    parent = os.path.dirname(package)
    if sys.path[:1] != [parent]:
        sys.path.insert(0, parent)
    try:
        selected = importlib.import_module("claudlobby")
        module = importlib.import_module(f"claudlobby.{name}")
    except ImportError as exc:
        raise GuardError(f"cannot load {name}: {exc}") from exc
    for mod, want in ((selected, os.path.join(package, "__init__.py")), (module, target)):
        if os.path.realpath(getattr(mod, "__file__", "") or "") != want:
            raise GuardError(f"{mod.__name__} came from outside this build")
    return module


def _plane_readers():
    spec = importlib.util.spec_from_file_location(
        "plane_readers", os.path.join(HERE, "plane-readers.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now() -> float:
    seam = os.environ.get("COMPACT_GUARD_NOW")
    return float(seam) if seam else time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def deliveries_since(root: str, fleet: str, bot: str, since: int) -> list:
    """The assignment deliveries recorded to the bot after ``since``, oldest
    first, at most 5: each a communication that names an assignment and the
    bot as its recipient. Raises when the plane cannot answer."""
    seam = os.environ.get("COMPACT_GUARD_DELIVERIES")
    if seam:
        with open(seam, encoding="utf-8") as fh:
            rows = json.load(fh)
        return [r for r in rows if datetime.fromisoformat(
            r["occurred_at"].replace("Z", "+00:00")).timestamp() > since][:5]
    readers = _plane_readers()
    conn = readers.connect(root)
    try:
        readers.fleet_uid(conn, fleet)  # a plane that never saw the fleet refuses
        entry = readers.bot_entry(conn, fleet, bot)
        if entry is None:  # no identity: nothing was ever recorded to the bot
            return []
        uids = entry["uids"]
        sql = ("SELECT msg_id, occurred_at FROM communications"
               " WHERE assignment_id IS NOT NULL AND occurred_at > ?"
               " AND recipient_uid IN (" + ",".join("?" * len(uids)) + ")"
               " ORDER BY occurred_at LIMIT 5")
        rows = conn.execute(sql, (readers.since_form(_iso(since)), *uids)).fetchall()
    finally:
        conn.close()
    return [{"msg_id": msg, "occurred_at": at} for msg, at in rows]


def _settings_precompact(bot_dir: Path) -> tuple:
    """The PreCompact commands of the bot's composed settings, or a reason."""
    path = bot_dir / ".claude" / "settings.local.json"
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError, AttributeError) as exc:
        return None, f"settings unreadable: {type(exc).__name__}"
    commands = []
    for group in hooks.get("PreCompact") or []:
        for hook in (group or {}).get("hooks") or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands, None


def _engine(bot_dir: Path) -> dict:
    """The engine Claudron's composed PreCompact hook names, and its version."""
    commands, problem = _settings_precompact(bot_dir)
    out = {"command": None, "executable": None, "version": None, "error": problem}
    if commands is None:
        return out
    named = [c for c in commands if re.search(r"(^|\s)hook\s+pre-compact(\s|$)", c)]
    if not named:
        out["error"] = "no composed Claudron PreCompact hook"
        return out
    out["command"] = named[0]
    try:
        word = shlex.split(named[0])[0]
    except (ValueError, IndexError):
        out["error"] = "the hook's command does not parse"
        return out
    exe = word if os.path.isabs(word) else shutil.which(word)
    out["executable"] = exe or word
    if not exe:
        out["error"] = f"{word} is not on PATH"
        return out
    timeout = float(os.environ.get("COMPACT_GUARD_VERSION_TIMEOUT_S") or VERSION_TIMEOUT_S)
    try:
        done = subprocess.run([exe, "version"], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = f"version: {type(exc).__name__}"
        return out
    found = _VERSION.search(done.stdout or "")
    if done.returncode != 0 or not found:
        out["error"] = f"version: exit {done.returncode}, no X.Y.Z"
        return out
    out["version"] = found.group(0)
    return out


def _cached_engine(bot_dir: Path, request_id) -> dict:
    """The engine's reading for this request: read once, then from the cache."""
    cache = bot_dir / "data" / CACHE_NAME
    try:
        held = json.loads(cache.read_text(encoding="utf-8"))
        if held.get("request_id") == request_id and isinstance(held.get("engine"), dict):
            return held["engine"]
    except (OSError, ValueError, AttributeError):
        pass
    engine = _engine(bot_dir)
    try:
        tmp = cache.with_name(f"{CACHE_NAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"request_id": request_id, "engine": engine}) + "\n",
                       encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass
    return engine


def precompact(bot_dir: Path, loop: str, reflect: str, request_id) -> dict:
    """Whether a composed PreCompact hook would refuse this compaction (#1053's
    Nov 7 amendment): ``refuses``, its ``reason`` and ``hook``, and the readings."""
    out = {"claudron_session_loop": loop or None, "claudna_precompact_reflect": reflect or None,
           "refuses": False, "reason": None, "hook": None}
    if not loop:
        return {**out, "refuses": True, "reason": "precompact_hooks_unknown"}
    if reflect == "1":
        return {**out, "refuses": True, "reason": "precompact_hook_refuses", "hook": "claudna"}
    if loop == "1":
        engine = _cached_engine(bot_dir, request_id)
        out["engine"] = engine
        version = engine.get("version")
        parts = tuple(int(p) for p in _VERSION.fullmatch(version).groups()) if version else None
        if parts is None or parts < FIXED_ENGINE:
            return {**out, "refuses": True, "reason": "precompact_hook_refuses", "hook": "claudron"}
    return out


def _floor() -> int:
    try:
        return int(os.environ.get("COMPACT_AT_CLOSE_TOKENS") or FLOOR_DEFAULT)
    except ValueError:
        return FLOOR_DEFAULT


def _field(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\t\r\n]", " ", text)
    return text or "-"


def _emit(verdict: str, reason, hook, request: Optional[dict], data: dict) -> int:
    req = request or {}
    print("\t".join(_field(v) for v in (verdict, reason, hook, req.get("request_id"), req.get("by"),
                                       req.get("requested_at"), req.get("requested_epoch"))))
    print(json.dumps(data, sort_keys=True, separators=(",", ":")))
    return 0


def _summary(request: dict) -> dict:
    return {key: request.get(key) for key in ("request_id", "by", "requested_at",
                                              "requested_epoch", "context")}


def check(args) -> int:
    cr = _package_module("compact_request")
    tu = _package_module("transcript_usage")
    bot_dir = Path(args.bot_dir)
    request = cr.read_request(bot_dir)
    if request is None:
        return _emit("none", None, None, None, {})
    now = _now()
    readings: dict = {}
    summary = _summary(request)

    def ends(verdict: str, reason: str, hook=None) -> int:
        nonlocal summary
        if verdict == "drop":
            taken = cr.take_request(bot_dir)
            if taken is None:  # withdrawn since the read
                return _emit("none", None, None, None, {})
            summary = _summary(taken) if taken else summary
        state = {"drop": "dropped", "skip": "kept"}.get(verdict, "pending")
        data = {**summary, "reason": reason, "hook": hook, "request": state, "readings": readings}
        return _emit(verdict, reason, hook, summary, data)

    epoch = request.get("requested_epoch")
    if not request or type(epoch) is not int:
        return ends("drop", "request_unreadable")
    readings["age_s"] = int(now - epoch)
    if now - epoch >= MAX_AGE_S:
        return ends("drop", "request_expired")
    try:
        found = deliveries_since(args.root, args.fleet, args.bot, epoch)
    except Exception as exc:  # noqa: BLE001 - an unread plane is named, never clear
        readings["deliveries"] = {"read": "unread", "error": f"{type(exc).__name__}: {exc}"[:300]}
        return ends("skip", "plane_unread")
    readings["deliveries"] = {"read": "ok", "since_request": found}
    if found:
        return ends("drop", "delivered_since_request")
    transcripts = Path(args.transcripts)
    since = tu.compaction_since(transcripts, epoch)
    readings["compaction_since_request"] = {key: since[key] for key in (
        "compaction", "session_after", "reason", "session", "bytes_read")}
    if since["reason"]:
        return ends("skip", "transcript_unread")
    if since["compaction"]:
        return ends("drop", "compacted_since_request")
    if since["session_after"]:
        return ends("drop", "new_session_since_request")
    if str(request.get("by") or "").startswith("assignment:"):
        live = tu.context_in(transcripts)
        readings["context"] = {"tokens": live["tokens"], "at": live["at"], "reason": live["reason"]}
        readings["floor"] = _floor()
        if live["tokens"] is None:
            return ends("skip", "context_unread")
        if live["tokens"] < readings["floor"]:
            return ends("drop", "below_floor")
    hooks = precompact(bot_dir, args.loop, args.reflect, request.get("request_id"))
    readings["precompact"] = hooks
    if hooks["refuses"]:
        return ends("skip", hooks["reason"], hooks["hook"])
    return ends("go", None)


def take(args) -> int:
    cr = _package_module("compact_request")
    taken = cr.take_request(Path(args.bot_dir))
    if taken is None:
        return _emit("none", None, None, None, {})
    return _emit("taken", None, None, taken, _summary(taken))


def proof(args) -> int:
    tu = _package_module("transcript_usage")
    since = tu.compaction_since(Path(args.transcripts), args.since)
    row = since["compaction"]
    if row:
        print("proven\t" + "\t".join(_field(row.get(k)) for k in (
            "at", "trigger", "pre_tokens", "post_tokens")))
    elif since["reason"]:
        print(f"unknown\t{_field(since['reason'])}")
    else:
        print("none")
    print(json.dumps(since, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compact-guard.py",
                                     description="The request side of keepalive's compaction.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    c = verbs.add_parser("check")
    for name in ("--bot-dir", "--root", "--fleet", "--bot", "--transcripts"):
        c.add_argument(name, required=True)
    c.add_argument("--loop", default="")
    c.add_argument("--reflect", default="")
    t = verbs.add_parser("take")
    t.add_argument("--bot-dir", required=True)
    p = verbs.add_parser("proof")
    p.add_argument("--transcripts", required=True)
    p.add_argument("--since", required=True, type=float)
    args = parser.parse_args(argv)
    try:
        return {"check": check, "take": take, "proof": proof}[args.verb](args)
    except GuardError as exc:
        print(f"compact-guard.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
