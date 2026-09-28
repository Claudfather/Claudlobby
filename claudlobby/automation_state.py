"""Bot automation pause and run history in the existing host-shared fleet state.

Only the selected bot's automation keys are changed. The shell status writer
uses the same lock and preserves these keys through its jq field assignments.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


MAX_STATE_BYTES = 16 * 1024 * 1024
LOCK_WAIT_S = 30.0
OUTCOMES = frozenset({"completed", "bypassed", "needs-input", "blocked", "partial"})


class AutomationStateError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@contextmanager
def _state_lock(path: Path):
    """Hold the shell writer's kernel lock on the same host-shared file."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("fleet state file lock is busy") from None
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _state_file(root: Path) -> Path:
    path = root / "state/fleet-state.json"
    if path.parent.resolve() != path.parent:
        raise AutomationStateError("unavailable", "fleet state directory is redirected")
    return path


def _read(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutomationStateError("unavailable", "fleet state is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise AutomationStateError("unavailable", "fleet state is not a bounded regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_STATE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_STATE_BYTES:
        raise AutomationStateError("unavailable", "fleet state exceeds the read bound")
    try:
        state = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise AutomationStateError("unavailable", "fleet state is invalid JSON") from exc
    if not isinstance(state, dict) or not isinstance(state.get("bots"), dict):
        raise AutomationStateError("unavailable", "fleet state has no bot registry")
    return state


def _row(state: dict | None, fleet: str, bot: str) -> dict | None:
    if state is None:
        return None
    row = state["bots"].get(bot)
    if row is None:
        return None
    if not isinstance(row, dict) or row.get("fleet") != fleet:
        raise AutomationStateError("conflict", "bot state is not stamped with the selected fleet")
    return row


def status(root: Path, fleet: str, bot: str, *, configured: bool) -> dict:
    """Fail closed when runtime state is absent or attribution is ambiguous."""
    state = _read(_state_file(root))
    row = _row(state, fleet, bot)
    pause = row.get("autonomous_runner_pause") if row is not None else None
    if pause is not None and not isinstance(pause, dict):
        raise AutomationStateError("unavailable", "automation pause state is invalid")
    history = row.get("autonomous_runner_runs", []) if row is not None else []
    if not isinstance(history, list):
        raise AutomationStateError("unavailable", "automation run history is invalid")
    reason = ("not_configured" if not configured else
              "state_missing" if row is None else
              "paused" if pause is not None else None)
    return {"fleet": fleet, "bot": bot, "configured": configured,
            "eligible": reason is None, "ineligible_reason": reason,
            "paused": pause is not None, "pause": pause,
            "runs_recorded": len(history), "last_run": history[-1] if history else None,
            "state_observation": "unavailable" if row is None else "recorded"}


def _write(path: Path, state: dict) -> None:
    try:
        body = (json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(body) > MAX_STATE_BYTES:
            raise AutomationStateError("unavailable", "fleet state exceeds the write bound")
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fleet-state-",
                                         delete=False) as stream:
            temp = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp.unlink(missing_ok=True)
    except OSError as exc:
        raise AutomationStateError("unavailable", "fleet state write failed") from exc


def _url(value: str | None, repo: str, kind: str) -> str | None:
    if value is None:
        return None
    route = "pull" if kind == "pr" else "issues"
    if (not isinstance(value, str)
            or not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/{route}/[1-9][0-9]*", value)):
        raise AutomationStateError("invalid_argument", f"--{kind} requires a {repo} {route} URL")
    return value


def record_input(outcome: str, pr: str | None, issue: str | None, repo: str) -> dict:
    if outcome not in OUTCOMES:
        raise AutomationStateError("invalid_argument", "--outcome is not a documented runner outcome")
    return {"outcome": outcome, "pr_url": _url(pr, repo, "pr"),
            "issue_url": _url(issue, repo, "issue")}


def pause_input(reason: str) -> dict:
    if (not isinstance(reason, str) or not reason.strip() or "\x00" in reason
            or len(reason.encode("utf-8")) > 4096):
        raise AutomationStateError("invalid_argument", "--reason requires bounded nonempty text")
    return {"reason": reason.strip()}


def mutate(root: Path, fleet: str, bot: str, verb: str, *, actor: str,
           request_id: str, payload: dict, target_repo: str | None = None) -> dict:
    """Commit one scoped control or run record under the shell writer's lock."""
    if verb not in {"pause", "resume", "record"}:
        raise AutomationStateError("invalid_argument", "unknown automation action")
    if verb == "pause":
        if not isinstance(payload, dict) or payload != pause_input(payload.get("reason")):
            raise AutomationStateError("invalid_argument", "pause accepts only a reason")
    elif verb == "resume":
        if payload != {}:
            raise AutomationStateError("invalid_argument", "resume accepts no input")
    elif (not isinstance(payload, dict) or target_repo is None or payload != record_input(
            payload.get("outcome"), payload.get("pr_url"), payload.get("issue_url"), target_repo)):
        raise AutomationStateError("invalid_argument", "record accepts only validated outcome and links")
    path = _state_file(root)
    try:
        with _state_lock(Path(f"{path}.lock")):
            state = _read(path)
            row = _row(state, fleet, bot)
            if row is None:
                raise AutomationStateError("unavailable", "selected bot has no recorded fleet state")
            controls = row.get("autonomous_runner_controls", [])
            runs = row.get("autonomous_runner_runs", [])
            if not isinstance(controls, list) or not isinstance(runs, list):
                raise AutomationStateError("unavailable", "automation history is invalid")
            for previous in (*controls, *runs):
                if not isinstance(previous, dict):
                    raise AutomationStateError("unavailable", "automation history has an invalid entry")
                if previous.get("request_id") == request_id:
                    if (previous.get("action") != verb or previous.get("input") != payload
                            or previous.get("actor") != actor):
                        raise AutomationStateError("conflict", "automation request ID has different input")
                    return {"recording": "unchanged", "replayed": True,
                            "state": status(root, fleet, bot, configured=True)}
            now = datetime.now(timezone.utc).isoformat()
            entry = {"request_id": request_id, "action": verb, "input": payload,
                     "timestamp": now, "actor": actor}
            if verb == "record":
                runs.append({**entry, **payload})
                row["autonomous_runner_runs"] = runs
            else:
                controls.append(entry)
                row["autonomous_runner_controls"] = controls
                row["autonomous_runner_pause"] = (
                    {"reason": payload["reason"], "since": now, "actor": actor}
                    if verb == "pause" else None)
            state["updated"] = now
            _write(path, state)
            return {"recording": "committed", "replayed": False,
                    "state": status(root, fleet, bot, configured=True)}
    except TimeoutError as exc:
        raise AutomationStateError("unavailable", "fleet state lock is busy; no action performed") from exc
    except OSError as exc:
        raise AutomationStateError("unavailable", "fleet state access failed; inspect the same request ID") from exc
