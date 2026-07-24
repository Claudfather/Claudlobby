#!/usr/bin/env python3
"""Per-session token accounting from Claude Code transcripts.

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
  - Sidechain (subagent) turns carry top-level `isSidechain: true`; they are
    split out and excluded unless --include-sidechains.

Usage:
  transcript-usage.py <transcript.jsonl | dir>... [--include-sidechains]
                      [--json] [--comms-share]

  A directory argument is walked for *.jsonl. Default output is human-readable;
  --json emits a machine-readable object with both `main` and `with_sidechains`
  aggregates. --comms-share adds the outbound-comms token estimate.
"""

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field

# Cost weights mirror the published Claude billing ratios relative to a base
# input token (=1.0): cache writes cost 1.25x, cache reads 0.1x, output 5.0x.
# `cost_weighted_total` uses these so a protocol whose standing cache_read weight
# exceeds its savings still shows up as a regression on the cost axis.
WEIGHTS = {"input": 1.0, "cache_creation": 1.25, "cache_read": 0.1, "output": 5.0}

# Outbound-comms tool surfaces. Telegram posts carry text in input.text; the
# shell comms helpers are matched by marker substring inside a Bash command.
TELEGRAM_COMMS_TOOLS = {
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__edit_message",
}
COMMS_BASH_MARKERS = ("report-back.sh", "tg-post.sh", "dispatch.sh", "dispatch-task.sh")

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
        return d


@dataclass
class ParseResult:
    path: str
    main: Usage = field(default_factory=Usage)
    sidechain: Usage = field(default_factory=Usage)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _scan_comms(content) -> tuple[int, int]:
    """Return (blocks, chars) of outbound-comms text in an assistant content list."""
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
            if any(marker in cmd for marker in COMMS_BASH_MARKERS):
                blocks += 1
                chars += len(cmd)
    return blocks, chars


def parse_file(path: str) -> ParseResult:
    """Sum usage over the assistant turns in one transcript, main vs sidechain."""
    result = ParseResult(path=path)
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
                continue  # malformed line — skip, keep going
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            bucket = result.sidechain if obj.get("isSidechain") else result.main
            bucket.turns += 1
            bucket.input_tokens += _int(usage.get("input_tokens"))
            bucket.output_tokens += _int(usage.get("output_tokens"))
            bucket.cache_creation_input_tokens += _int(
                usage.get("cache_creation_input_tokens")
            )
            bucket.cache_read_input_tokens += _int(usage.get("cache_read_input_tokens"))
            model = msg.get("model")
            if model:
                bucket.models.add(model)
            blocks, chars = _scan_comms(msg.get("content"))
            bucket.comms_blocks += blocks
            bucket.comms_chars += chars
    return result


def _iter_transcripts(paths) -> list:
    """Expand file/dir args into a sorted list of transcript files."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.jsonl"))))
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
    if args.comms_share and primary.output_tokens:
        share = 100.0 * primary.comms_est_tokens / primary.output_tokens
        print(
            f"  comms est ~{primary.comms_est_tokens} tok = {share:.1f}% of output tokens (estimate)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
