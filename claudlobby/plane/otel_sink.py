"""Private, bounded raw OTLP/JSON files for the host-local intake (#2145)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import stat
import threading
import time

from ..commands._otel_parsers import DEFAULT_RETENTION_DAYS, _positive, retention_parser
from ..source_state import scan_dir
from .wal import human_bytes

SIGNALS = ("logs", "metrics")
MAX_MEGABYTES, MAX_FILES = 64, 15
RAW_SINK_CEILING_MIB = MAX_MEGABYTES * MAX_FILES * len(SIGNALS)
CONTENT_KEYS = ("prompt", "prompt_text", "response", "tool_input", "arguments", "output", "body")
TOOL_PARAMETER_NAMES = ("mcp_server_name", "mcp_tool_name", "skill_name", "subagent_type")
IDENTITY_KEYS = ("user.email", "user.account_uuid", "user.account_id", "user.id", "organization.id")


def sink_dir(root: Path) -> Path:
    return root / "state" / "otel"


def retention_days(script: str) -> int:
    return retention_parser().parse_known_args(shlex.split(script)[1:])[0].retention_days


def strip_content(body: dict) -> int:
    """Fail closed per resource; keep only names from tool_parameters JSON."""
    removed = 0
    for resource in body.get("resourceLogs", []):
        attrs = resource.get("resource", {}).get("attributes", [])
        if any(a.get("key") == "claudlobby.content" and a.get("value", {}).get("stringValue") == "full"
               for a in attrs):
            continue
        for scope in resource.get("scopeLogs", []):
            for record in scope.get("logRecords", []):
                kept = []
                for attr in record.get("attributes", []):
                    key = attr.get("key")
                    if key in CONTENT_KEYS + IDENTITY_KEYS:
                        removed += 1
                        continue
                    if key == "tool_parameters":
                        try:
                            parameters = json.loads(attr.get("value", {}).get("stringValue", ""))
                            if not isinstance(parameters, dict):
                                raise ValueError("not an object")
                            names = {k: v for k, v in parameters.items() if k in TOOL_PARAMETER_NAMES}
                            serialized = json.dumps(names, separators=(",", ":"))
                        except (ValueError, TypeError, RecursionError):
                            removed += 1
                            continue
                        attr = {**attr, "value": {"stringValue": serialized}}
                    kept.append(attr)
                record["attributes"] = kept
    return removed


@dataclass
class SinkScan:
    state: str
    size: int = 0
    oldest_mtime: float | None = None


def _private(path: Path) -> bool:
    st = path.lstat()
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700


def scan_sink(root: Path) -> SinkScan:
    path = sink_dir(root)
    try:
        if not _private(path):
            return SinkScan("not_private")
        probe, entries = scan_dir(path)
        if probe.state != "ok":
            return SinkScan("unreadable")
        result = SinkScan("ok")
        for entry in entries:
            if not any(entry.name == f"{s}.jsonl" or
                       entry.name.startswith(f"{s}-") and entry.suffix == ".jsonl" for s in SIGNALS):
                continue
            try:
                st = entry.lstat()
            except FileNotFoundError:
                continue                    # rotated/pruned since the one listing
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                return SinkScan("not_private")
            result.size += st.st_size
            result.oldest_mtime = st.st_mtime if result.oldest_mtime is None else min(result.oldest_mtime, st.st_mtime)
        return result
    except FileNotFoundError:
        return SinkScan("absent")
    except OSError:
        return SinkScan("unreadable")


class RawSink:
    """One process owns the sink; concurrent handlers serialize every mutation."""
    def __init__(self, root: Path, *, days: int):
        if days <= 0:
            raise ValueError("retention days must be positive")
        self.root, self.days = root, days
        self.path = sink_dir(root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir(mode=0o700, exist_ok=True)
        if scan_sink(root).state != "ok":
            raise OSError("otel sink not_private or unreadable")
        self.lock = threading.Lock()
        self._failed = False
        # A prior crash/failed rollback must not let a new process join its fragment.
        for signal in SIGNALS:
            try:
                fd = os.open(self.path / f"{signal}.jsonl", os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                self._checked_size(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _checked_size(fd: int) -> int:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise OSError("otel sink file not_private")
        if st.st_size and os.pread(fd, 1, st.st_size - 1) != b"\n":
            raise OSError("otel sink incomplete raw tail — repair before restarting")
        return st.st_size

    def append(self, signal: str, body: dict) -> None:
        if signal not in SIGNALS:
            raise ValueError("unknown OTLP signal")
        if signal == "logs":
            strip_content(body)
        line = (json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        with self.lock:
            if self._failed:
                raise OSError("otel sink rollback failed — refusing further writes")
            if not _private(self.path):
                raise OSError("otel sink not_private")
            active = self.path / f"{signal}.jsonl"
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
            fd = os.open(active, flags, 0o600)
            rotated = False
            try:
                offset = self._checked_size(fd)
                if offset and offset + len(line) > MAX_MEGABYTES * 1024 * 1024:
                    os.close(fd)
                    fd = None
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns()}"
                    active.rename(self.path / f"{signal}-{stamp}.jsonl")
                    rotated = True
                    fd = os.open(active, flags, 0o600)
                    offset = self._checked_size(fd)
                try:
                    with os.fdopen(fd, "wb", buffering=0, closefd=False) as stream:
                        if stream.write(line) != len(line):
                            raise OSError("otel sink short raw append")
                except OSError:
                    try:
                        os.ftruncate(fd, offset)
                    except OSError as exc:
                        self._failed = True
                        raise OSError("otel sink rollback failed — refusing further writes") from exc
                    raise
            finally:
                if fd is not None:
                    os.close(fd)
                # Enforce the count even if the rotated append failed. After a
                # complete write, prune failure can return 503 with the body present.
                if rotated:
                    self._prune(time.time())

    def _prune(self, now: float) -> None:
        probe, entries = scan_dir(self.path)
        if probe.state != "ok":
            raise OSError("otel sink unreadable")
        for signal in SIGNALS:
            files = sorted((p for p in entries if p.name == f"{signal}.jsonl" or
                            p.name.startswith(f"{signal}-") and p.suffix == ".jsonl"),
                           key=lambda p: p.lstat().st_mtime, reverse=True)
            for index, path in enumerate(files):
                if index >= MAX_FILES or path.lstat().st_mtime < now - self.days * 86400:
                    path.unlink()

    def prune(self, now: float) -> None:
        with self.lock:
            self._prune(now)


def sink_rung(scan: SinkScan, *, max_days: int | None) -> tuple[bool, str]:
    if scan.state != "ok":
        detail = "UNREADABLE — cannot enumerate (a gap, not a zero)" if scan.state == "unreadable" else scan.state
        return False, detail
    age = max(0, time.time() - scan.oldest_mtime) / 86400 if scan.oldest_mtime else 0
    ceiling = RAW_SINK_CEILING_MIB * 1024 * 1024
    detail = f"{human_bytes(scan.size)} of {human_bytes(ceiling)}, oldest {age:.1f} d"
    if max_days is None:
        detail += " — retention check skipped: host override unavailable"
    if scan.size > ceiling:
        return False, detail + " — over ceiling"
    if max_days is not None and age > max_days + 1:
        return False, detail + " — retention is not keeping up"
    return True, detail + (" — near the ceiling" if scan.size > ceiling * .8 else "")
