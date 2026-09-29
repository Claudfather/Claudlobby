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
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 8192

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
    truncated_file: int = 0


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


def parse_file(path: str, *, since: datetime | None = None,
               until: datetime | None = None, max_bytes: int | None = None) -> ParseResult:
    """Count each identified assistant message once within this file and window.

    Real interactive transcripts repeat flat usage on multiple content-block
    lines for one message (harness/ab-comms-eval.sh's existing real-run blocker).
    Keys remain file-local and include session ID; unkeyed turns are counted but
    explicitly disclosed by bounded fleet reads rather than guessed together.
    """
    result = ParseResult(path=path)
    seen: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        read_bytes = 0
        while True:
            line = fh.readline(max_bytes + 1 if max_bytes is not None else -1)
            if not line:
                break
            read_bytes += len(line.encode("utf-8", errors="replace"))
            if max_bytes is not None and read_bytes > max_bytes:
                result.truncated_file = 1
                break
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
                try:
                    instant = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
                    if instant.tzinfo is None:
                        raise ValueError("timestamp has no offset")
                    instant = instant.astimezone(timezone.utc)
                except (KeyError, TypeError, AttributeError, ValueError):
                    result.missing_timestamps += 1
                    continue
                if since is not None and instant < since:
                    continue
                if until is not None and instant > until:
                    continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict) or not usage:
                result.missing_usage += 1
                continue
            values = tuple(_int(usage.get(name)) for name in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens"))
            key = (obj.get("sessionId"), msg.get("id"))
            if all(isinstance(part, str) and part for part in key):
                if key in seen:
                    result.duplicate_messages += 1
                    if seen[key] != values:
                        result.conflicting_duplicates += 1
                    # Distinct content-block lines may still contain distinct
                    # communication tools; only flat usage is repeated.
                    blocks, chars = _scan_comms(msg.get("content"))
                    bucket = result.sidechain if obj.get("isSidechain") else result.main
                    bucket.comms_blocks += blocks
                    bucket.comms_chars += chars
                    continue
                seen[key] = values
            else:
                result.unkeyed_turns += 1
            bucket = result.sidechain if obj.get("isSidechain") else result.main
            bucket.turns += 1
            bucket.input_tokens += values[0]
            bucket.output_tokens += values[1]
            bucket.cache_creation_input_tokens += values[2]
            bucket.cache_read_input_tokens += values[3]
            model = msg.get("model")
            if isinstance(model, str) and model:
                bucket.models.add(model)
            blocks, chars = _scan_comms(msg.get("content"))
            bucket.comms_blocks += blocks
            bucket.comms_chars += chars
    return result


def _bounded_files(directory: Path, since: datetime):
    """Enumerate only one bot's current cwd slug, never an entire account."""
    files: list[tuple[Path, int]] = []
    issues: list[str] = []
    stack = [(directory, 0)]
    entries = bytes_total = 0
    skipped = outside_window = 0
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
            if len(files) >= MAX_FILES or bytes_total + stat.st_size > MAX_BYTES:
                issues.append("file_or_byte_limit_reached")
                skipped += 1
                return files, issues, skipped, outside_window
            files.append((Path(child.path), stat.st_size))
            bytes_total += stat.st_size
    return files, issues, skipped, outside_window


def collect_bot_usage(paths, fleet, bot_id: str, since: datetime,
                      until: datetime) -> dict:
    """Read current-cwd Claude transcripts for one declared bot, with coverage."""
    from .composer import account_dir
    from .isolation import expand_home, transcript_slug

    bot = fleet.bots[bot_id]
    account = expand_home(account_dir(bot, fleet), Path.home())
    sharing = (sorted(name for name, other in fleet.bots.items() if name != bot_id
                      and expand_home(account_dir(other, fleet), Path.home()) == account)
               if account is not None else [])
    base = {"bot": bot_id, "account": bot.account,
            "attribution": "configured_account_and_current_bot_cwd_only",
            "shared_account_with_selected_fleet_bots": sharing,
            "other_fleets_on_same_account": "not_inspected",
            "window": {"since": since.isoformat(), "until": until.isoformat()},
            "scan_limits": {"files": MAX_FILES, "bytes": MAX_BYTES,
                            "entries": MAX_ENTRIES, "depth": 4},
            "quota": {"status": "unavailable", "reason": "no_provider_observation"}}
    empty = _counts(Usage())
    if account is None:
        return {**base, "usage": empty, "coverage": {"status": "unavailable",
                "issues": ["account_directory_unresolved"], "files_read": 0,
                "files_skipped_at_least": 0}}
    cwd_slug = transcript_slug(paths.bot_runtime(bot_id))
    peers = [name for name in sharing if transcript_slug(paths.bot_runtime(name)) == cwd_slug]
    if peers:
        return {**base, "usage": empty, "coverage": {"status": "unavailable",
                "issues": ["ambiguous_shared_account_cwd_slug"],
                "shared_with": sorted(peers), "files_read": 0, "files_skipped_at_least": 0}}
    directory = account / "projects" / cwd_slug
    if not directory.is_dir() or directory.is_symlink():
        return {**base, "usage": empty, "coverage": {"status": "unavailable",
                "issues": ["transcript_directory_missing_or_untrusted"],
                "files_read": 0, "files_skipped_at_least": 0}}
    files, issues, skipped, outside_window = _bounded_files(directory, since)
    main = Usage()
    side = Usage()
    unreadable_files = 0
    diagnostics = {name: 0 for name in ("malformed_lines", "missing_timestamps",
                   "missing_usage", "unkeyed_turns", "duplicate_messages",
                   "conflicting_duplicates", "truncated_file")}
    for path, size in files:
        try:
            row = parse_file(str(path), since=since, until=until, max_bytes=size)
        except OSError:
            skipped += 1
            unreadable_files += 1
            issues.append("unreadable_transcript_file")
            continue
        main += row.main
        side += row.sidechain
        for name in diagnostics:
            diagnostics[name] += getattr(row, name)
    if any(diagnostics[name] for name in ("malformed_lines", "missing_timestamps",
                                             "missing_usage", "unkeyed_turns",
                                             "conflicting_duplicates", "truncated_file")):
        issues.append("transcript_rows_not_fully_attributable")
    files_read = len(files) - unreadable_files
    if not files_read:
        issues.append("no_readable_recent_transcripts")
    return {**base, "usage": _counts(main + side), "main": _counts(main),
            "sidechain": _counts(side),
            "coverage": {"status": "unavailable" if not files_read else
                         "partial" if issues else "observed",
                         "issues": sorted(set(issues)), "files_read": files_read,
                         "files_skipped_at_least": skipped, "candidate_files": len(files),
                         "additional_files_after_limit": "unknown" if any(
                             "limit_reached" in issue for issue in issues) else 0,
                         "older_files_excluded_by_mtime": outside_window,
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
