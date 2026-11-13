#!/usr/bin/env python3
"""One transcript-accounting owner for bounded fleet reads and private evaluation.

Reads `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` transcripts and sums the
four usage components over assistant turns, emitting two named cost axes plus an
outbound-comms estimate. The prize-sizing instrument for the token-efficiency
communication protocol (#716 / #729 stage A); #717's token-accounting extends it.

Ground truth (verified against real transcripts):
  - Assistant turns are lines with top-level `type == "assistant"`.
  - Per-turn usage is the FLAT `message.usage` object:
      input_tokens, output_tokens, cache_creation_input_tokens,
      cache_read_input_tokens.
  - `message.usage.iterations[]` breaks the same request into inference passes;
    summing it double-counts (a real 2-iteration turn reported flat cache_read
    79k vs iterations-sum 163k = 2.06x). We read flat and NEVER touch iterations.
  - `cache_read_input_tokens` is summed per turn: each turn is a separate paid
    API call that re-reads the whole cached prefix, so the running total is the
    standing-context cost — not a quantity to dedupe or max.
  - Sidechain (subagent) turns carry top-level `isSidechain: true`, but they do
    NOT sit inline in the parent `.jsonl`: they live in separate nested files at
    `<slug>/<sessionId>/subagents/[workflows/wf_*/]agent-*.jsonl`. A directory
    argument is therefore walked RECURSIVELY — a flat glob misses ~13% of real
    fleet spend. Subagent turns are bucketed as sidechain and excluded from the
    primary total unless --include-sidechains.

Usage:
  python -m claudlobby.transcript_usage <transcript.jsonl | dir>... [--include-sidechains]
                      [--json] [--comms-share]

  A directory argument is walked recursively for *.jsonl (to reach the nested
  subagent transcripts above). Default output is human-readable; --json emits a
  machine-readable object with both `main` and `with_sidechains` aggregates.
  --comms-share adds the outbound-comms estimate + its cost-weighted share of
  spend.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_FILES = 512
# The usage read (#717) takes each transcript from its end back to the
# window's start, so what it costs follows the window, not the file. This
# bounds the bytes one bot's read takes in all; past it the coverage is
# partial and says how many bytes went unread. One bot's 24 h window measured
# 95 MB across 62 transcripts, so this allows 2.8 times that.
MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 8192

# The live-context read (#2206) runs backwards from the end of the newest
# transcript in blocks and stops at the first main-chain usage row, never past
# this cap. Measured over 12,000 rows in four real sessions, the longest
# stretch between two usage rows was 511 KB; the cap allows four times that.
CONTEXT_TAIL_BYTES = 2 * 1024 * 1024
_TAIL_BLOCK = 64 * 1024
# A hook's detached `claude -p` session writes its transcript beside the bot's
# own, and it is not the bot's: its rows name `entrypoint` sdk-cli where an
# interactive session's name cli (the field separated 120 of 120 real files,
# #2212 review). The field follows the prompt, which a headless session holds
# twice before it, so its first value ended 12 to 166 KB into the 13 headless
# files measured, against under 6 KB in 27 interactive ones. The read that
# finds it stops at that row, never past this cap.
SESSION_HEAD_BYTES = 2 * 1024 * 1024
HEADLESS_ENTRYPOINT = "sdk-cli"
# Each session newer than the bot's own costs one head read before it is
# passed over, and a hook can leave several in a row, so the walk examines at
# most this many of the newest. Over 899 sessions in 21 bots' directories, the
# longest run of headless sessions was two (#2212 review). Past the limit the
# answer is unknown, never an older session's figure.
SESSION_WALK_LIMIT = 16
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens")

# Evaluator-relative token weights (not a dollar price or subscription meter).
# `cost_weighted_total` lets paired protocol cells compare their token mix.
WEIGHTS = {"input": 1.0, "cache_creation": 1.25, "cache_read": 0.1, "output": 5.0}

# Outbound-comms tool surfaces. Telegram posts carry text in input.text.
# Historical shell doors remain recognizable in old transcripts; canonical
# commands are classified by their executable and exact write verb.
TELEGRAM_COMMS_TOOLS = {
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__edit_message",
}
COMMS_BASH_MARKERS = ("report-back.sh", "tg-post.sh", "dispatch.sh", "dispatch-task.sh")
ASSIGNMENT_COMMS_VERBS = {"deliver", "progress", "block", "return", "complete", "fail"}

CHARS_PER_TOKEN = 4  # rough estimate for comms text; labeled as an estimate in output


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    turns: int = 0
    comms_blocks: int = 0
    comms_chars: int = 0
    models: set = field(default_factory=set)

    @property
    def protocol_sensitive(self) -> int:
        """Fresh, protocol-movable tokens: input + output (no cache)."""
        return self.input_tokens + self.output_tokens

    @property
    def cost_weighted_total(self) -> float:
        return (
            self.input_tokens * WEIGHTS["input"]
            + self.cache_creation_input_tokens * WEIGHTS["cache_creation"]
            + self.cache_read_input_tokens * WEIGHTS["cache_read"]
            + self.output_tokens * WEIGHTS["output"]
        )

    @property
    def comms_est_tokens(self) -> int:
        return self.comms_chars // CHARS_PER_TOKEN

    @property
    def comms_cost_weighted(self) -> float:
        """Weighted token estimate for outbound text, not a billed charge."""
        return self.comms_est_tokens * WEIGHTS["output"]

    @property
    def comms_share_of_cost_pct(self) -> float:
        """Outbound comms as a share of the evaluator's weighted token estimate.

        Uses cost_weighted_total (not output_tokens) so the number matches "% of
        spend" as the prize-sizing read-out characterizes it.
        """
        cw = self.cost_weighted_total
        return 100.0 * self.comms_cost_weighted / cw if cw else 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
            turns=self.turns + other.turns,
            comms_blocks=self.comms_blocks + other.comms_blocks,
            comms_chars=self.comms_chars + other.comms_chars,
            models=self.models | other.models,
        )

    def to_dict(self, include_comms: bool = False) -> dict:
        d = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "turns": self.turns,
            "protocol_sensitive": self.protocol_sensitive,
            "cost_weighted_total": round(self.cost_weighted_total, 3),
            "models": sorted(self.models),
        }
        if include_comms:
            d["comms_blocks"] = self.comms_blocks
            d["comms_chars"] = self.comms_chars
            d["comms_est_tokens"] = self.comms_est_tokens
            d["comms_cost_weighted"] = round(self.comms_cost_weighted, 3)
            d["comms_share_of_cost_pct"] = round(self.comms_share_of_cost_pct, 6)
        return d


def _counts(usage: Usage) -> dict:
    """Public observed counts; evaluator-only weighted estimates stay private."""
    return {key: getattr(usage, key) for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "turns")} | {"models": sorted(usage.models)}


@dataclass
class ParseResult:
    path: str
    main: Usage = field(default_factory=Usage)
    sidechain: Usage = field(default_factory=Usage)
    malformed_lines: int = 0
    missing_timestamps: int = 0
    missing_usage: int = 0
    unkeyed_turns: int = 0
    duplicate_messages: int = 0
    conflicting_duplicates: int = 0


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _outbound_segment(words: list[str]) -> bool:
    """Classify one simple shell command, not quoted arguments to another tool."""
    i = 0
    if words and words[0] == "env":
        i = 1
    while i < len(words) and "=" in words[i] and not words[i].startswith("--"):
        i += 1
    if i >= len(words):
        return False
    exe = os.path.basename(words[i])
    if exe in ("bash", "sh", "zsh") and i + 1 < len(words):
        return os.path.basename(words[i + 1]) in COMMS_BASH_MARKERS
    if exe in COMMS_BASH_MARKERS:
        return True
    if exe != "claudlobby":
        return False
    i += 1
    while i < len(words):
        option = words[i]
        if option == "--json" or option.startswith(("--root=", "--fleet=")):
            i += 1
        elif option in ("--root", "--fleet"):
            i += 2
        else:
            break
    verb = words[i:]
    if "--help" in verb or "-h" in verb:
        return False
    return (verb[:2] in (["message", "send"], ["message", "reply"])
            or len(verb) >= 2 and verb[0] == "assignment" and verb[1] in ASSIGNMENT_COMMS_VERBS
            or verb[:3] == ["fleet", "reports", "submit"])


def _outbound_bash(command: str) -> bool:
    """Estimate simple commands and shell chains; do not interpret scripts."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return False
    segment = []
    for word in (*words, ";"):
        if word and set(word) <= set(";&|"):
            if _outbound_segment(segment):
                return True
            segment = []
        else:
            segment.append(word)
    return False


def _scan_comms(content) -> tuple[int, int]:
    """Return rough outbound-comms blocks and authored command/text characters.

    Shell scripts, here-docs and external --file payload bytes are not parsed;
    Bash contributes the command string's length, not proven delivered text.
    """
    blocks = chars = 0
    if not isinstance(content, list):
        return 0, 0
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") != "tool_use":
            continue
        name = blk.get("name")
        inp = blk.get("input")
        if not isinstance(inp, dict):
            continue
        if name in TELEGRAM_COMMS_TOOLS:
            blocks += 1
            chars += len(str(inp.get("text", "")))
        elif name == "Bash":
            cmd = str(inp.get("command", ""))
            if _outbound_bash(cmd):
                blocks += 1
                chars += len(cmd)
    return blocks, chars


def _row_instant(obj) -> datetime | None:
    """A transcript row's time as an aware UTC instant; None when it is
    absent, unparseable or has no offset."""
    try:
        instant = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    return instant.astimezone(timezone.utc) if instant.tzinfo is not None else None


def _row_usage(obj) -> tuple[int, int, int, int] | None:
    """An assistant row's flat usage, in _USAGE_FIELDS order; None when it
    carries none. The one usage-row reader: parse_file sums it over a window,
    current_context takes the newest (#2206)."""
    msg = obj.get("message") or {}
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict) or not usage:
        return None
    return tuple(_int(usage.get(name)) for name in _USAGE_FIELDS)


def _loads(line: bytes):
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def compaction_row(obj) -> dict | None:
    """One Claude Code compaction, from its ``compact_boundary`` row; None for
    any other row (#2206).

    The row is ``{"type": "system", "subtype": "compact_boundary", "timestamp",
    "uuid", "sessionId", "compactMetadata": {"trigger", "preTokens",
    "postTokens", ...}}``. ``postTokens`` is set when the compaction ends: an
    absent one reads None, never 0."""
    if (not isinstance(obj, dict) or obj.get("type") != "system"
            or obj.get("subtype") != "compact_boundary"):
        return None
    meta = obj.get("compactMetadata")
    at = _row_instant(obj)
    if not isinstance(meta, dict) or at is None:
        return None
    return {"at": at.isoformat(), "trigger": _text(meta.get("trigger")),
            "pre_tokens": _count(meta.get("preTokens")),
            "post_tokens": _count(meta.get("postTokens")),
            "session": _text(obj.get("sessionId")), "uuid": _text(obj.get("uuid"))}


def _row_record(obj) -> tuple | None:
    """What counting takes from one assistant row: its flat usage, its
    ``(session, message)`` key, whether a sidechain wrote it, its model and its
    outbound comms; None when it carries no usage."""
    values = _row_usage(obj)
    if values is None:
        return None
    msg = obj["message"]
    blocks, chars = _scan_comms(msg.get("content"))
    return (values, (obj.get("sessionId"), msg.get("id")), bool(obj.get("isSidechain")),
            msg.get("model"), blocks, chars)


def _tally(result: ParseResult, seen: dict, record: tuple) -> None:
    """Count one row's ``_row_record`` into ``result``, rows taken in file
    order: a message's first row gives its usage, and each later row with its
    key adds only its comms (content-block lines may carry distinct tools;
    only flat usage repeats)."""
    values, key, sidechain, model, blocks, chars = record
    bucket = result.sidechain if sidechain else result.main
    bucket.comms_blocks += blocks
    bucket.comms_chars += chars
    if all(isinstance(part, str) and part for part in key):
        if key in seen:
            result.duplicate_messages += 1
            if seen[key] != values:
                result.conflicting_duplicates += 1
            return
        seen[key] = values
    else:
        result.unkeyed_turns += 1
    bucket.turns += 1
    bucket.input_tokens += values[0]
    bucket.output_tokens += values[1]
    bucket.cache_creation_input_tokens += values[2]
    bucket.cache_read_input_tokens += values[3]
    if isinstance(model, str) and model:
        bucket.models.add(model)


def parse_file(path: str, *, since: datetime | None = None,
               until: datetime | None = None) -> ParseResult:
    """Count each identified assistant message once within this file and window.

    Real interactive transcripts repeat flat usage on multiple content-block
    lines for one message (harness/ab-comms-eval.sh's existing real-run blocker).
    Keys remain file-local and include session ID; unkeyed turns are counted but
    explicitly disclosed by bounded fleet reads rather than guessed together.
    """
    result = ParseResult(path=path)
    seen: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Cheap reject of non-assistant lines (~70% of a transcript) before the
            # json.loads allocation: an assistant turn always contains the literal
            # substring "assistant" (its own type/role value spells it).
            if "assistant" not in line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                result.malformed_lines += 1
                continue  # malformed line — skip, keep going
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            if since is not None or until is not None:
                instant = _row_instant(obj)
                if instant is None:
                    result.missing_timestamps += 1
                    continue
                if since is not None and instant < since:
                    continue
                if until is not None and instant > until:
                    continue
            record = _row_record(obj)
            if record is None:
                result.missing_usage += 1
                continue
            _tally(result, seen, record)
    return result


def _bounded_files(directory: Path, since: datetime):
    """Enumerate only one bot's current cwd slug, never an entire account."""
    files: list[tuple[Path, int]] = []
    issues: list[str] = []
    stack = [(directory, 0)]
    entries = skipped = outside_window = 0
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as listing:
                children = []
                for child in listing:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        issues.append("entry_limit_reached")
                        return files, issues, skipped, outside_window
                    children.append(child)
        except OSError:
            issues.append("unreadable_transcript_directory")
            continue
        for child in sorted(children, key=lambda entry: entry.name):
            try:
                if child.is_symlink():
                    issues.append("symlink_skipped")
                    skipped += 1
                    continue
                if child.is_dir(follow_symlinks=False):
                    if depth < 4:
                        stack.append((Path(child.path), depth + 1))
                    else:
                        issues.append("depth_limit_reached")
                        skipped += 1
                    continue
                if not child.name.endswith(".jsonl") or not child.is_file(follow_symlinks=False):
                    continue
                stat = child.stat(follow_symlinks=False)
            except OSError:
                issues.append("unreadable_transcript_entry")
                skipped += 1
                continue
            if stat.st_mtime < since.timestamp():
                outside_window += 1
                continue
            if len(files) >= MAX_FILES:
                issues.append("file_limit_reached")
                skipped += 1
                return files, issues, skipped, outside_window
            files.append((Path(child.path), stat.st_size, stat.st_mtime))
    return files, issues, skipped, outside_window


@dataclass(frozen=True)
class TranscriptSource:
    """Where one declared bot's current-cwd transcripts are, or why that is
    unknown: the one resolution `fleet usage`, `fleet status` and the
    compaction recorder share."""
    sharing: list
    directory: Path | None = None
    issue: str | None = None
    shared_with: tuple = ()


def transcript_source(paths, fleet, bot_id: str) -> TranscriptSource:
    from .composer import account_dir
    from .isolation import expand_home, transcript_slug

    bot = fleet.bots[bot_id]
    account = expand_home(account_dir(bot, fleet), Path.home())
    sharing = (sorted(name for name, other in fleet.bots.items() if name != bot_id
                      and expand_home(account_dir(other, fleet), Path.home()) == account)
               if account is not None else [])
    if account is None:
        return TranscriptSource(sharing, issue="account_directory_unresolved")
    cwd_slug = transcript_slug(paths.bot_runtime(bot_id))
    peers = [name for name in sharing if transcript_slug(paths.bot_runtime(name)) == cwd_slug]
    if peers:
        return TranscriptSource(sharing, issue="ambiguous_shared_account_cwd_slug",
                                shared_with=tuple(sorted(peers)))
    directory = account / "projects" / cwd_slug
    if not directory.is_dir() or directory.is_symlink():
        return TranscriptSource(sharing, issue="transcript_directory_missing_or_untrusted")
    return TranscriptSource(sharing, directory=directory)


def session_transcripts(directory: Path) -> tuple[list, str | None]:
    """A bot's session transcripts, the top-level ``*.jsonl`` files only, as
    ``(name, size, mtime_ns)``; or why the listing is unknown. Subagent
    transcripts nest below their session and are never one."""
    found = []
    entries = 0
    try:
        with os.scandir(directory) as listing:
            for child in listing:
                entries += 1
                if entries > MAX_ENTRIES:
                    return [], "entry_limit_reached"
                if not child.name.endswith(".jsonl"):
                    continue
                try:
                    if child.is_symlink() or not child.is_file(follow_symlinks=False):
                        continue
                    stat = child.stat(follow_symlinks=False)
                except OSError:
                    # It may be the newest: an unknown order is no answer.
                    return [], "unreadable_transcript_entry"
                found.append((child.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return [], "unreadable_transcript_directory"
    return found, None


def _first_entrypoint(fh, size: int, cap: int) -> tuple[str | None, str]:
    """A transcript's first top-level ``entrypoint`` value, read forwards from
    its start over its first ``size`` bytes in blocks, never past ``cap``; and
    how the read ended: ``found``, ``absent`` (the file holds none),
    ``beyond_cap``, or ``incomplete`` (its last line is still being written)."""
    pos, carry = 0, b""
    while pos < size:
        if pos >= cap:
            return None, "beyond_cap"
        step = min(_TAIL_BLOCK, size - pos, cap - pos)
        block = fh.read(step)
        if len(block) != step:
            raise OSError("transcript shrank during the read")
        pos += step
        lines = (carry + block).split(b"\n")
        carry = lines.pop()
        for line in lines:
            if b'"entrypoint"' in line:
                obj = _loads(line)
                if isinstance(obj, dict) and isinstance(obj.get("entrypoint"), str):
                    return obj["entrypoint"], "found"
    if not carry:
        return None, "absent"
    obj = _loads(carry)
    if not isinstance(obj, dict):
        return None, "incomplete"
    value = obj.get("entrypoint")
    return (value, "found") if isinstance(value, str) else (None, "absent")


def newest_own_session(directory: Path, sessions: list, *,
                       cap: int = SESSION_HEAD_BYTES) -> tuple[tuple | None, str | None]:
    """The newest of a bot's ``sessions`` (as ``session_transcripts`` lists
    them) that is its own and not a hook's headless ``claude -p`` call: its
    first ``entrypoint`` value is not HEADLESS_ENTRYPOINT, and a session with
    none counts as the bot's own. It examines at most the SESSION_WALK_LIMIT
    newest. ``(entry, None)``; ``(None, None)`` when every session is
    headless; ``(None, reason)`` when there is no answer: a newer session
    cannot be told apart from the bot's own (it may be the bot's), or the
    sessions examined are all headless and older ones are left unexamined."""
    newest = sorted(sessions, key=lambda entry: (entry[2], entry[0]), reverse=True)
    for entry in newest[:SESSION_WALK_LIMIT]:
        try:
            with open(directory / entry[0], "rb") as fh:
                value, ended = _first_entrypoint(fh, entry[1], cap)
        except OSError:
            return None, "unreadable_transcript_file"
        if ended == "beyond_cap":
            return None, "session_entrypoint_beyond_cap"
        if ended == "incomplete":
            return None, "session_head_incomplete"
        if value != HEADLESS_ENTRYPOINT:
            return entry, None
    if len(newest) > SESSION_WALK_LIMIT:
        return None, "no_own_session_within_limit"
    return None, None


class _Tail:
    """The complete lines of one file, newest first, read backwards in blocks
    and never more than ``cap`` bytes from its end."""

    def __init__(self, fh, size: int, cap: int):
        self.fh, self.size, self.cap = fh, size, cap
        self.bytes_read = 0
        self.capped = False

    def lines(self):
        pos, carry = self.size, b""
        while pos > 0:
            if self.bytes_read >= self.cap:
                self.capped = True
                return
            step = min(_TAIL_BLOCK, pos, self.cap - self.bytes_read)
            pos -= step
            self.fh.seek(pos)
            block = self.fh.read(step)
            if len(block) != step:
                raise OSError("transcript shrank during the read")
            self.bytes_read += step
            parts = (block + carry).split(b"\n")
            carry = parts[0]  # whole only once the read reaches the file's start
            for part in reversed(parts[1:]):
                if part:
                    yield part
        if carry:
            yield carry


def current_context(paths, fleet, bot_id: str, *, cap: int = CONTEXT_TAIL_BYTES,
                    head_cap: int = SESSION_HEAD_BYTES) -> dict:
    """One bot's live context (#2206): its newest main-chain usage row's input
    + cache read + cache creation tokens and that row's time, with any
    compaction written after the row. The row is from the bot's own newest
    session, never a hook's headless one (``newest_own_session``). ``tokens``
    is None, never 0, with a ``reason`` when no such row can be read.

    Cost per call: one listing of the bot's transcript directory (at most
    MAX_ENTRIES entries); the head of its own newest session and of each newer
    one, at most SESSION_WALK_LIMIT heads in all, each forwards in 64 KiB
    blocks, stopping at the first ``entrypoint`` and never past ``head_cap``;
    then reads from the end of its own newest session, in 64 KiB blocks,
    stopping at the row and never past ``cap``. ``bytes_read`` counts that last
    read.
    """
    source = transcript_source(paths, fleet, bot_id)
    if source.directory is None:
        return {"tokens": None, "at": None, "reason": source.issue, "session": None,
                "compacted_after": None, "bytes_read": 0, "cap": cap}
    return context_in(source.directory, cap=cap, head_cap=head_cap)


def context_in(directory: Path, *, cap: int = CONTEXT_TAIL_BYTES,
               head_cap: int = SESSION_HEAD_BYTES) -> dict:
    """``current_context`` for a bot whose transcript directory is known: what it
    reads once ``transcript_source`` has found the directory. A runtime script
    that knows the bot's directory (#1053's compact-guard.py) calls this, so it
    reads the context as the CLI does without loading the fleet's config."""
    result = {"tokens": None, "at": None, "reason": None, "session": None,
              "compacted_after": None, "bytes_read": 0, "cap": cap}
    sessions, issue = session_transcripts(directory)
    chosen = None
    if issue is None:
        chosen, issue = newest_own_session(directory, sessions, cap=head_cap)
    if chosen is None:
        return {**result, "reason": issue or "no_transcript"}
    name, size, _ = chosen
    result["session"] = name[:-len(".jsonl")]
    tail = None
    try:
        with open(directory / name, "rb") as fh:
            tail = _Tail(fh, size, cap)
            for line in tail.lines():
                if b'"compact_boundary"' in line:
                    row = compaction_row(_loads(line))
                    if row is not None and result["compacted_after"] is None:
                        result["compacted_after"] = {key: row[key] for key in (
                            "at", "trigger", "pre_tokens", "post_tokens")}
                    continue
                if b'"assistant"' not in line:
                    continue
                obj = _loads(line)
                if (not isinstance(obj, dict) or obj.get("type") != "assistant"
                        or obj.get("isSidechain")):
                    continue
                values, at = _row_usage(obj), _row_instant(obj)
                tokens = values[0] + values[2] + values[3] if values else 0
                # A row Claude Code writes itself for an error or an interrupt
                # (model <synthetic>) carries zero usage: no call, so no context.
                if tokens and at is not None:
                    return {**result, "tokens": tokens, "at": at.isoformat(),
                            "bytes_read": tail.bytes_read}
    except OSError:
        return {**result, "reason": "unreadable_transcript_file",
                "bytes_read": tail.bytes_read if tail else 0}
    return {**result, "bytes_read": tail.bytes_read,
            "reason": ("no_main_usage_row_within_cap" if tail.capped
                       else "no_main_usage_row_in_transcript")}


def compaction_since(directory: Path, since: float, *, cap: int = CONTEXT_TAIL_BYTES,
                     head_cap: int = SESSION_HEAD_BYTES) -> dict:
    """The newest compaction a bot's own newest session wrote at or after
    ``since`` (epoch seconds), for #1053's executor: its ``compact_boundary`` row
    as ``compaction_row`` reads it (``at``, ``trigger``, ``pre_tokens``,
    ``post_tokens``), or None. Unlike ``compacted_after`` it does not stop at the
    newest usage row, so a turn after the compaction does not hide it.

    The session's rows are read from its end backwards, in 64 KiB blocks, and the
    read stops at the first timed row older than ``since`` (Claude Code appends
    rows in time order; see ``parse_window``), and never past ``cap``. A read
    that reaches the session's first row without passing ``since`` sets
    ``session_after: true``: the whole session is newer than ``since``. ``reason``
    names why the answer is unknown: a capped read, or an unreadable listing,
    session or file."""
    result = {"compaction": None, "session_after": False, "reason": None, "session": None,
              "bytes_read": 0, "cap": cap}
    sessions, issue = session_transcripts(directory)
    chosen = None
    if issue is None:
        chosen, issue = newest_own_session(directory, sessions, cap=head_cap)
    if chosen is None:
        return {**result, "reason": issue or "no_transcript"}
    name, size, _ = chosen
    result["session"] = name[:-len(".jsonl")]
    floor = datetime.fromtimestamp(since, timezone.utc)
    tail = None
    try:
        with open(directory / name, "rb") as fh:
            tail = _Tail(fh, size, cap)
            for line in tail.lines():
                obj = _loads(line)
                at = _row_instant(obj) if isinstance(obj, dict) else None
                if at is None:
                    continue
                if at < floor:
                    return {**result, "bytes_read": tail.bytes_read}
                if b'"compact_boundary"' in line:
                    row = compaction_row(obj)
                    if row is not None:
                        return {**result, "bytes_read": tail.bytes_read,
                                "compaction": {key: row[key] for key in (
                                    "at", "trigger", "pre_tokens", "post_tokens")}}
    except OSError:
        return {**result, "reason": "unreadable_transcript_file",
                "bytes_read": tail.bytes_read if tail else 0}
    if tail.capped:
        return {**result, "bytes_read": tail.bytes_read, "reason": "since_not_reached_within_cap"}
    return {**result, "bytes_read": tail.bytes_read, "session_after": True}


def parse_window(path: str, size: int, since: datetime, until: datetime,
                 budget: int) -> tuple[ParseResult, int, str]:
    """What ``parse_file(path, since=since, until=until)`` counts in the
    file's first ``size`` bytes, read from that end backwards in 64 KiB
    blocks and never more than ``budget`` bytes (#717). Claude Code appends
    rows in time order, so the read stops at the first assistant row older
    than ``since``: every row before it is older too (of 83,108 timed
    assistant rows in 435 real transcripts, none was older than a row before
    it). The rows read are
    counted oldest first, as ``parse_file`` takes them. Returns the counts,
    the bytes read, and where the read stopped: ``window_start``,
    ``file_start`` or ``byte_limit``. Bytes appended after the listing
    measured ``size`` are not read; the window closed before that listing."""
    result = ParseResult(path=path)
    records = []
    ended = "file_start"
    with open(path, "rb") as fh:
        tail = _Tail(fh, size, budget)
        for line in tail.lines():
            if b"assistant" not in line:
                continue
            # Decoded as parse_file reads, so a bad byte reads the same in both.
            obj = _loads(line.decode("utf-8", errors="replace"))
            if obj is None:  # no line naming "assistant" is the JSON null
                result.malformed_lines += 1
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            instant = _row_instant(obj)
            if instant is None:
                result.missing_timestamps += 1
                continue
            if instant < since:
                ended = "window_start"
                break
            if instant > until:
                continue
            record = _row_record(obj)
            if record is None:
                result.missing_usage += 1
            else:
                records.append(record)
        if tail.capped:  # set only when the read ran out of budget, never at a break
            ended = "byte_limit"
    seen: dict = {}
    for record in reversed(records):
        _tally(result, seen, record)
    return result, tail.bytes_read, ended


# Coverage issues from the listing that leave further files uncounted.
_LISTING_LIMITS = ("entry_limit_reached", "file_limit_reached", "depth_limit_reached")


def collect_bot_usage(paths, fleet, bot_id: str, since: datetime,
                      until: datetime) -> dict:
    """Read current-cwd Claude transcripts for one declared bot, with coverage."""
    bot = fleet.bots[bot_id]
    source = transcript_source(paths, fleet, bot_id)
    sharing = source.sharing
    base = {"bot": bot_id, "account": bot.account,
            "attribution": "configured_account_and_current_bot_cwd_only",
            "shared_account_with_selected_fleet_bots": sharing,
            "other_fleets_on_same_account": "not_inspected",
            "window": {"since": since.isoformat(), "until": until.isoformat()},
            "scan_limits": {"files": MAX_FILES, "bytes": MAX_BYTES,
                            "entries": MAX_ENTRIES, "depth": 4},
            "quota": {"status": "unavailable", "reason": "no_provider_observation"}}
    empty = _counts(Usage())
    if source.directory is None:
        shared = {"shared_with": list(source.shared_with)} if source.shared_with else {}
        return {**base, "usage": empty, "coverage": {"status": "unavailable",
                "issues": [source.issue], **shared, "files_read": 0,
                "files_skipped_at_least": 0}}
    files, issues, skipped, outside_window = _bounded_files(source.directory, since)
    # The most recently written first, so a read cut at the byte limit leaves
    # the oldest bytes unread.
    files.sort(key=lambda entry: entry[2], reverse=True)
    main = Usage()
    side = Usage()
    files_read = 0
    diagnostics = {name: 0 for name in ("malformed_lines", "missing_timestamps",
                   "missing_usage", "unkeyed_turns", "duplicate_messages",
                   "conflicting_duplicates")}
    # What the read took and what it left: bytes older than the window are
    # proven so by a row; bytes left at the byte limit are a coverage gap.
    read = {"bytes_read": 0, "bytes_before_window": 0, "bytes_unread_at_limit": 0}
    for index, (path, size, _mtime) in enumerate(files):
        if read["bytes_read"] >= MAX_BYTES:
            issues.append("byte_limit_reached")
            skipped += len(files) - index
            read["bytes_unread_at_limit"] += sum(entry[1] for entry in files[index:])
            break
        try:
            row, taken, ended = parse_window(str(path), size, since, until,
                                             MAX_BYTES - read["bytes_read"])
        except OSError:
            skipped += 1
            issues.append("unreadable_transcript_file")
            continue
        files_read += 1
        read["bytes_read"] += taken
        if ended == "window_start":
            read["bytes_before_window"] += size - taken
        elif ended == "byte_limit":
            issues.append("byte_limit_reached")
            read["bytes_unread_at_limit"] += size - taken
        main += row.main
        side += row.sidechain
        for name in diagnostics:
            diagnostics[name] += getattr(row, name)
    if any(diagnostics[name] for name in ("malformed_lines", "missing_timestamps",
                                             "missing_usage", "unkeyed_turns",
                                             "conflicting_duplicates")):
        issues.append("transcript_rows_not_fully_attributable")
    # A complete scan with no in-window candidates is an idle bot: observed zero.
    idle = not files and not issues
    if not files_read and not idle:
        issues.append("no_readable_recent_transcripts")
    return {**base, "usage": _counts(main + side), "main": _counts(main),
            "sidechain": _counts(side),
            "coverage": {"status": "observed" if idle else
                         "unavailable" if not files_read else
                         "partial" if issues else "observed",
                         "issues": sorted(set(issues)), "files_read": files_read,
                         "files_skipped_at_least": skipped, "candidate_files": len(files),
                         "additional_files_after_limit": "unknown" if any(
                             issue in _LISTING_LIMITS for issue in issues) else 0,
                         "older_files_excluded_by_mtime": outside_window, **read,
                         "limits": {"files": MAX_FILES, "bytes": MAX_BYTES,
                                    "entries": MAX_ENTRIES, "depth": 4},
                         "diagnostics": diagnostics,
                         "shared_account": bool(sharing)}}


def collect_fleet_usage(paths, fleet, since: datetime, until: datetime) -> dict:
    """Sum only attributable bot rows; disclose any missing fleet coverage."""
    bots = [collect_bot_usage(paths, fleet, bot_id, since, until)
            for bot_id in sorted(fleet.bots)]
    observed = [row for row in bots if row["coverage"]["status"] != "unavailable"]
    fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "turns")
    total = {field: sum(row["usage"][field] for row in observed) for field in fields}
    total["models"] = sorted({model for row in observed for model in row["usage"]["models"]})
    state = ("unavailable" if not observed else
             "partial" if any(row["coverage"]["status"] != "observed" for row in bots)
             else "observed")
    return {"fleet": fleet.name, "window": {"since": since.isoformat(),
                                            "until": until.isoformat()},
            "usage": total, "bots": bots,
            "coverage": {"status": state, "bots_observed": len(observed),
                         "bots_total": len(bots),
                         "meaning": "sum of readable current-cwd Claude transcripts only"},
            "quota": {"status": "unavailable", "reason": "no_provider_observation"}}


# --- the per-call view: what each call's context held (#2909) ----------------


# `bot usage --calls` reads the bot's newest own session back from its end,
# never past this cap. In one long session (2026-11-13) the newest 100 calls
# took 4.4 MiB, so the cap holds over three times CALLS_MAX such calls. One line
# as long as the cap took 5.5 s to read, the join cost #2756 (note 3) names;
# that review measured real lines at 0.3 MiB at most.
CALLS_TAIL_BYTES = 16 * 1024 * 1024
CALLS_DEFAULT = 20
CALLS_MAX = 100
# Before each call the view lists this many of its largest row groups and sums
# the rest, so one call costs a bounded number of lines whatever its step held.
CALL_GROUPS = 5
_LABEL_CHARS = 120
# A tool's target is its name, and for these tools also the input value that
# names what the call works on. No other input is ever reported: a command or a
# message body may hold a secret.
_TOOL_TARGET_KEYS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
                     "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
                     "Skill": "skill", "Agent": "subagent_type", "Task": "subagent_type"}
_ATTACHMENT_TARGET_KEYS = ("path", "filename", "file_path", "skillName", "hookName")


def _leaf_chars(value) -> int:
    """The characters of every string inside a JSON value."""
    total, stack = 0, [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return total


def _label(value, limit: int = _LABEL_CHARS) -> str | None:
    """A kind or target as the view reports it: a non-empty string, cut to its
    last ``limit`` characters, since a path's end names its file."""
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= limit else "…" + value[-(limit - 1):]


def _tool_target(name, tool_input) -> str:
    target = _label(name, 60) or "unnamed"
    key = _TOOL_TARGET_KEYS.get(target)
    value = tool_input.get(key) if key and isinstance(tool_input, dict) else None
    if isinstance(value, str) and value:
        return f"{target} {_label(value, _LABEL_CHARS - len(target) - 1)}"
    return target


def _fields_chars(block: dict) -> int:
    """A content block's characters, its ``type`` name aside."""
    return _leaf_chars([value for key, value in block.items() if key != "type"])


def _block_chars(content) -> tuple[int, int]:
    """A tool result's text characters and its image count."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and block.get("type") == "image":
            images += 1
        elif isinstance(block, dict) and block.get("type") == "text":
            chars += _leaf_chars(block.get("text"))
    return chars, images


def _row_parts(obj) -> list:
    """What one transcript row stored, as ``[kind, target, chars, images,
    tool_use_id]`` parts: one per content block of a message row, one for an
    attachment or system row. A tool result's target is its tool call's,
    filled in from the id once the read is done. Any other row (a snapshot, a
    request record, a mode change) stores no message, and the view lists none
    of it. An attachment is sized by its ``rendered`` text, the text Claude
    Code sent; one without it is kind ``unrendered:<type>``, sized by its
    fields, which ``_step_groups`` tallies apart."""
    kind = obj.get("type")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    parts = []
    if kind == "assistant":
        for block in message.get("content") or ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                parts.append(["output", _tool_target(block.get("name"), block.get("input")),
                              len(json.dumps(block.get("input"), ensure_ascii=False)), 0,
                              block.get("id") if isinstance(block.get("id"), str) else None])
            elif block.get("type") in ("text", "thinking"):
                parts.append(["output", block["type"], _leaf_chars(block.get(block["type"])),
                              0, None])
            else:
                parts.append(["output", _label(block.get("type"), 60) or "unknown",
                              _fields_chars(block), 0, None])
    elif kind == "user":
        content = message.get("content")
        target = ("compaction_summary" if obj.get("isCompactSummary")
                  else "meta" if obj.get("isMeta") else "text")
        if isinstance(content, str):
            parts.append(["user", target, len(content), 0, None])
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                chars, images = _block_chars(block.get("content"))
                parts.append(["tool_result", None, chars, images, block.get("tool_use_id")])
            elif block.get("type") == "text":
                parts.append(["user", target, _leaf_chars(block.get("text")), 0, None])
            elif block.get("type") == "image":
                parts.append(["user", "image", 0, 1, None])
            else:
                parts.append(["user", _label(block.get("type"), 60) or "unknown",
                              _fields_chars(block), 0, None])
    elif kind == "attachment":
        attachment = obj.get("attachment") if isinstance(obj.get("attachment"), dict) else {}
        name = _label(attachment.get("type"), 60) or "unknown"
        target = next((_label(attachment[key]) for key in _ATTACHMENT_TARGET_KEYS
                       if isinstance(attachment.get(key), str) and attachment[key]), None)
        if "rendered" in obj:
            parts.append([f"attachment:{name}", target, _leaf_chars(obj["rendered"]), 0, None])
        else:
            fields = {key: value for key, value in attachment.items() if key != "type"}
            parts.append([f"unrendered:{name}", target, _leaf_chars(fields), 0, None])
    elif kind == "system":
        parts.append([f"system:{_label(obj.get('subtype'), 60) or 'none'}", None,
                      _leaf_chars(obj.get("content")), 0, None])
    return parts


def _call_of(obj) -> tuple | None:
    """``(message id, context, output tokens, instant, model)`` for a row of a
    main-chain API call; None for any other row. A row Claude Code writes
    itself (model <synthetic>, for an error or an interrupt) carries zero
    usage, so it is no call."""
    if obj.get("type") != "assistant" or obj.get("isSidechain"):
        return None
    values = _row_usage(obj)
    if values is None:
        return None
    message = obj["message"]
    context = values[0] + values[2] + values[3]
    if not context or not isinstance(message.get("id"), str) or not message["id"]:
        return None
    return message["id"], context, values[1], _row_instant(obj), message.get("model")


def _step_groups(parts: list, tools: dict, groups: int) -> dict:
    """One call's step: its parts grouped by kind and target, the ``groups``
    largest by characters listed and the rest summed. Attachment rows that
    store no rendered text (hook records, among others) are tallied apart in
    ``unrendered``: the transcript does not show that they reach the model."""
    totals: dict = {}
    unrendered = [0, 0]
    for kind, target, chars, images, ref in parts:
        if kind.startswith("unrendered:"):
            unrendered[0] += 1
            unrendered[1] += chars
            continue
        if kind == "tool_result":
            target = tools.get(ref, "unmatched")
        entry = totals.setdefault((kind, target), [0, 0, 0])
        entry[0] += 1
        entry[1] += chars
        entry[2] += images
    ranked = sorted(totals.items(), key=lambda item: (-item[1][1], -item[1][2],
                                                      item[0][0], item[0][1] or ""))
    cut = ranked[groups:]
    return {"count": sum(entry[0] for entry in totals.values()),
            "chars": sum(entry[1] for entry in totals.values()),
            "images": sum(entry[2] for entry in totals.values()),
            "groups": [{"kind": kind, "target": target, "count": count, "chars": chars,
                        "images": images}
                       for (kind, target), (count, chars, images) in ranked[:groups]],
            "groups_cut": len(cut), "chars_cut": sum(entry[1] for _key, entry in cut),
            "unrendered": {"count": unrendered[0], "chars": unrendered[1]}}


def _calls_result(limit: int, cap: int, groups: int) -> dict:
    return {"scope": "newest_own_session", "session": None, "reason": None, "calls": [],
            "limit": limit, "ended": None, "complete": False, "bytes_read": 0, "cap": cap,
            "groups_per_call": groups,
            "diagnostics": {"malformed_lines": 0, "untimed_calls": 0}}


def calls_in(directory: Path, since: datetime, until: datetime, *,
             limit: int = CALLS_DEFAULT, cap: int | None = None,
             head_cap: int = SESSION_HEAD_BYTES, groups: int = CALL_GROUPS) -> dict:
    """The per-call view (#2909) for a bot whose transcript directory is known:
    the newest ``limit`` main-chain API calls of its newest own session
    (``newest_own_session``) timed within the window, oldest first.

    Each call gives its time, message id, model, context (input + cache read
    + cache creation tokens, as ``current_context`` reads it), output tokens
    (the largest its rows record), and its growth over the previous call: None
    for the first call read and for the first call after a compaction. Its
    ``step`` is what the transcript stored from the previous call's first row
    up to its own, which holds the previous call's output; after a compaction
    it starts at the ``compact_boundary`` row instead, since the rows before
    it left the context. Rows are counted in characters, never tokens.

    The session is read from its end backwards in 64 KiB blocks, never past
    ``cap`` bytes (CALLS_TAIL_BYTES), and the read stops at the first row of
    the call before the previous call of the oldest call shown. ``ended``
    says how it stopped: ``window_start`` (it passed the window's start),
    ``call_limit`` (older calls in the window are not shown), ``session_start``
    or ``byte_cap``. Below ``session_start`` the oldest call read is the
    previous call only, and after ``byte_cap`` the second oldest too, so every
    call shown has its whole step. ``complete`` is true when every main-chain
    call of the session in the window is shown."""
    cap = CALLS_TAIL_BYTES if cap is None else cap
    result = _calls_result(limit, cap, groups)
    sessions, issue = session_transcripts(directory)
    chosen = None
    if issue is None:
        chosen, issue = newest_own_session(directory, sessions, cap=head_cap)
    if chosen is None:
        return {**result, "reason": issue or "no_transcript"}
    name, size, _ = chosen
    result["session"] = name[:-len(".jsonl")]
    diagnostics = result["diagnostics"]
    rows, met, shown, previous, stopped, tail = [], set(), set(), None, False, None
    try:
        with open(directory / name, "rb") as fh:
            tail = _Tail(fh, size, cap)
            for line in tail.lines():
                # Decoded as parse_window reads, so a bad byte reads the same in both.
                obj = _loads(line.decode("utf-8", errors="replace"))
                if not isinstance(obj, dict):
                    diagnostics["malformed_lines"] += 1
                    continue
                if obj.get("isSidechain"):
                    continue
                call = _call_of(obj)
                if call is not None and call[0] not in met:
                    if previous is not None:
                        stopped = True
                        break
                    met.add(call[0])
                    at = call[3]
                    if at is None:
                        diagnostics["untimed_calls"] += 1
                    if at is not None and since <= at <= until and len(shown) < limit:
                        shown.add(call[0])
                    elif at is None or at <= until:
                        previous = call
                boundary = obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"
                parts = _row_parts(obj)
                if call is not None or boundary or parts:
                    rows.append((call, boundary, parts))
    except OSError:
        return {**result, "reason": "unreadable_transcript_file",
                "bytes_read": tail.bytes_read if tail else 0}
    rows.reverse()
    if stopped or not tail.capped:
        # The oldest call read is whole: a row older than it was read, or the
        # session's first row was.
        cut_by_limit = previous is not None and (previous[3] is None or previous[3] >= since)
        ended = ("call_limit" if cut_by_limit else "window_start" if previous is not None
                 else "session_start")
    else:
        ended = "byte_cap"
    tools = {part[4]: part[1] for _call, _boundary, parts in rows for part in parts
             if part[0] == "output" and part[4]}
    calls, by_id, step, compacted = [], {}, [], False
    for call, boundary, parts in rows:
        if boundary:
            step, compacted = [], True
            continue
        if call is not None and call[0] not in by_id:
            mid, context, output, at, model = call
            last = calls[-1] if calls else None
            by_id[mid] = {"at": at.isoformat() if at else None, "message": mid,
                          "model": _label(model, 60), "context": context,
                          "growth": (None if last is None or compacted
                                     else context - last["context"]),
                          "output_tokens": output, "after_compaction": compacted,
                          "step": step}
            calls.append(by_id[mid])
            step, compacted = [], False
        elif call is not None:
            by_id[call[0]]["output_tokens"] = max(by_id[call[0]]["output_tokens"], call[2])
        step.extend(parts)
    # A step is whole only from the previous call's first row: the oldest call
    # read has none unless the read reached the session's start, and after a
    # byte cap the oldest call's own rows may be cut too.
    hidden = set()
    if ended != "session_start" and calls:
        hidden.add(calls[0]["message"])
    if ended == "byte_cap" and len(calls) > 1:
        hidden.add(calls[1]["message"])
    for record in calls:
        record["step"] = _step_groups(record["step"], tools, groups)
    return {**result, "calls": [record for record in calls
                                if record["message"] in shown and record["message"] not in hidden],
            "ended": ended, "complete": ended in ("window_start", "session_start"),
            "bytes_read": tail.bytes_read}


def collect_bot_calls(paths, fleet, bot_id: str, since: datetime, until: datetime, *,
                      limit: int = CALLS_DEFAULT) -> dict:
    """``calls_in`` for one declared bot: its current-cwd transcripts, found as
    ``transcript_source`` finds them for ``bot usage`` and ``fleet status``."""
    source = transcript_source(paths, fleet, bot_id)
    if source.directory is None:
        return {**_calls_result(limit, CALLS_TAIL_BYTES, CALL_GROUPS), "reason": source.issue}
    return calls_in(source.directory, since, until, limit=limit)


def _iter_transcripts(paths) -> list:
    """Expand file/dir args into a sorted list of transcript files."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            # Recurse: subagent transcripts nest under
            # <sessionId>/subagents/[workflows/wf_*/]agent-*.jsonl at varying depth,
            # never inline in the parent .jsonl. A flat glob misses ~13% of real
            # spend. Non-transcript nested .jsonl contributes nothing (filtered on
            # type == "assistant").
            files.extend(
                sorted(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True))
            )
        else:
            files.append(p)
    return files


def _fmt(u: Usage, comms: bool) -> str:
    lines = [
        f"  turns={u.turns}  models={','.join(sorted(u.models)) or '-'}",
        f"  input={u.input_tokens}  output={u.output_tokens}"
        f"  cache_creation={u.cache_creation_input_tokens}  cache_read={u.cache_read_input_tokens}",
        f"  protocol_sensitive={u.protocol_sensitive}  cost_weighted_total={u.cost_weighted_total:.1f}",
    ]
    if comms:
        lines.append(
            f"  comms_blocks={u.comms_blocks}  comms_est_tokens~={u.comms_est_tokens} (chars/{CHARS_PER_TOKEN})"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Per-session token accounting from Claude Code transcripts."
    )
    ap.add_argument(
        "paths", nargs="+", help="transcript .jsonl files or directories to walk"
    )
    ap.add_argument(
        "--include-sidechains",
        action="store_true",
        help="fold subagent turns into the primary total",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable object",
    )
    ap.add_argument(
        "--comms-share",
        action="store_true",
        help="report the outbound-comms token estimate",
    )
    args = ap.parse_args(argv)

    files = _iter_transcripts(args.paths)
    per_file = []
    agg_main = Usage()
    agg_side = Usage()
    for f in files:
        try:
            r = parse_file(f)
        except OSError as e:
            print(f"warn: cannot read {f}: {e}", file=sys.stderr)
            continue
        per_file.append(r)
        agg_main = agg_main + r.main
        agg_side = agg_side + r.sidechain
    agg_combined = agg_main + agg_side

    if args.as_json:
        out = {
            "files": [
                {
                    "path": r.path,
                    "main": r.main.to_dict(args.comms_share),
                    "with_sidechains": (r.main + r.sidechain).to_dict(args.comms_share),
                }
                for r in per_file
            ],
            "aggregate": {
                "main": agg_main.to_dict(args.comms_share),
                "with_sidechains": agg_combined.to_dict(args.comms_share),
            },
            "weights": WEIGHTS,
        }
        print(json.dumps(out, indent=2))
        return 0

    primary = agg_combined if args.include_sidechains else agg_main
    label = "main+sidechains" if args.include_sidechains else "main"
    print(f"transcripts: {len(files)}  files parsed: {len(per_file)}")
    print(f"AGGREGATE ({label}):")
    print(_fmt(primary, args.comms_share))
    if not args.include_sidechains and agg_side.turns:
        print(
            f"  (sidechain turns excluded: {agg_side.turns}; --include-sidechains to fold in)"
        )
    if args.comms_share and primary.cost_weighted_total:
        print(
            f"  comms est ~{primary.comms_est_tokens} tok = "
            f"{primary.comms_share_of_cost_pct:.2f}% of spend (cost-weighted estimate)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
