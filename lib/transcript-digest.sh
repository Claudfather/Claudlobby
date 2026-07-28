#!/usr/bin/env bash
# transcript-digest.sh — SessionEnd hook: distil a finished session into one
# structured JSONL row via a cheap Haiku pass. The ingestion layer for the
# ai-platform fleet-monitor (#785 Phase A), which reasons over per-session
# digests and never over raw transcripts.
#
# Usage in fleet.yaml / system.yaml defaults:
#   hooks:
#     SessionEnd:
#       - command: "$CLAUDLOBBY_ROOT/lib/transcript-digest.sh"
#
# Why not /claudna:capture: capture is clauDNA's user-invocable write door to
# the Claudron vault -- an in-session agent distilling into curated PROSE notes,
# deduped and maturity-stamped. Three things make it the wrong tool here, and
# the boundary is ratified, not stylistic: (1) clauDNA #203 fixed the per-event
# stacking contract -- SessionEnd is Claudron's, clauDNA "has no such hook and
# adds none"; (2) capture cannot fire itself and nothing prompts it at session
# end; (3) capture's quality gate deliberately writes NOTHING when a session
# yields no durable learning, but a monitor NEEDS that null row -- "N tool calls,
# nothing notable" is exactly the idle-bot signal it exists to catch. What IS
# shared is capture's session-mode rubric (context / worked / failed /
# would_change / reusable), reused verbatim below as the extraction schema:
# consolidate the schema, not the mechanism.
#
# Two-stage input reduction, both mandatory:
#   1. DISTIL -- keep only user/assistant text plus tool NAMES; drop tool
#      results, attachments, file-history snapshots, queue ops. Measured on a
#      real transcript: 19.2 MB -> 549 KB, a 36x reduction. A raw byte-tail
#      would spend the whole budget on attachment JSON and carry no signal.
#   2. TAIL-CAP -- keep the last SESSION_DIGEST_TAIL_CHARS of that. 80 KB
#      defaults to ~20k tokens: bounded quota AND correctness, since a real
#      transcript is ~5M tokens and cannot enter a 200K context at all.
#
# Qualifying gate: a session under SESSION_DIGEST_MIN_TURNS still gets a row --
# a `skipped` one, written WITHOUT a model call. The gate bounds spend; it never
# costs the time-series. That is deliberately distinct from a `ok` row whose
# rubric fields all came back empty, which is the model saying "nothing notable
# happened" about a session that did qualify. The monitor needs to tell those apart.
#
# Env:
#   SESSION_DIGEST_ENABLED     — "0" disables entirely (default: enabled)
#   SESSION_DIGEST_MIN_TURNS   — qualifying gate, user+assistant turns (default 6)
#   SESSION_DIGEST_TAIL_CHARS  — tail-cap on the distilled text (default 80000)
#   SESSION_DIGEST_MODEL       — extraction model (default haiku)
#   SESSION_DIGEST_TIMEOUT     — seconds for the model call (default 120)
#   SESSION_DIGEST_LOG_DIR     — output dir (default $CLAUDLOBBY_ROOT/state/transcript-digests)
#   CLAUDE_BIN                 — model binary override (harness seam)
#
# Non-blocking by contract: every failure path still writes a row and exits 0.
# A digest failure must never delay or break a session ending.

trap 'exit 0' ERR
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"

[ "${SESSION_DIGEST_ENABLED:-1}" = "0" ] && exit 0

MIN_TURNS="${SESSION_DIGEST_MIN_TURNS:-6}"
TAIL_CHARS="${SESSION_DIGEST_TAIL_CHARS:-80000}"
MODEL="${SESSION_DIGEST_MODEL:-haiku}"
MODEL_TIMEOUT="${SESSION_DIGEST_TIMEOUT:-120}"
LOG_DIR="${SESSION_DIGEST_LOG_DIR:-$CLAUDLOBBY_ROOT/state/transcript-digests}"

payload="$(cat 2>/dev/null || true)"

mkdir -p "$LOG_DIR" 2>/dev/null || true
OUT="$LOG_DIR/transcript-digest-$(date +%Y-%m-%d).jsonl"

# --- Stage 1: locate + distil ------------------------------------------------
# One python3 pass emits a tab-separated header line (turns, tool_calls,
# transcript_bytes, transcript_path) followed by the distilled text. Values
# travel via env, never interpolated into the program (bot-vitals precedent).
WORK="$(safe_mktemp)"
PAYLOAD_VAL="$payload" TAIL_VAL="$TAIL_CHARS" python3 - >"$WORK" 2>/dev/null <<'PYEOF' || true
import json, os, re, sys

try:
    p = json.loads(os.environ.get("PAYLOAD_VAL") or "{}")
except (json.JSONDecodeError, ValueError):
    p = {}

path = p.get("transcript_path") or ""
sid = p.get("session_id") or ""
cwd = p.get("cwd") or os.getcwd()
# Fall back to the documented layout when the payload omits the path:
# ~/.claude/projects/<cwd-slug>/<session_id>.jsonl
if not path and sid:
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    path = os.path.expanduser(f"~/.claude/projects/{slug}/{sid}.jsonl")

# Redact BEFORE the model sees anything: a transcript carries tokens and
# connection strings, and the digest lands in a shared log. Scrubbing the input
# is strictly stronger than asking the model not to echo what it was shown.
SECRETS = [
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"), "[REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"), "[REDACTED]"),
    (re.compile(r"\b(ntn_[A-Za-z0-9]{16,})"), "[REDACTED]"),
    (re.compile(r"\b(napi_[A-Za-z0-9]{16,})"), "[REDACTED]"),
    (re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_\-]{30,}"), "[REDACTED]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"([a-z][a-z0-9+.\-]*://[^\s:/@]+):[^\s@/]+@"), r"\1:[REDACTED]@"),
]

def scrub(s):
    for rx, rep in SECRETS:
        s = rx.sub(rep, s)
    return s

turns = tool_calls = 0
out = []
size = 0
try:
    size = os.path.getsize(path)
except OSError:
    path = path or ""

try:
    fh = open(path, errors="replace")
except OSError:
    fh = None

if fh is not None:
    with fh:
        for ln in fh:
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                d = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            c = (d.get("message") or {}).get("content")
            parts = []
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(b.get("text") or "")
                    elif b.get("type") == "tool_use":
                        tool_calls += 1
                        parts.append("[tool:%s]" % b.get("name"))
                    # tool_result intentionally dropped: it dominates bytes and
                    # carries the least digest signal per token.
            txt = " ".join(x.strip() for x in parts if x and x.strip())
            if not txt:
                continue
            turns += 1
            out.append("%s: %s" % (t, txt[:600]))

text = scrub("\n".join(out))
try:
    cap = int(os.environ.get("TAIL_VAL") or 80000)
except ValueError:
    cap = 80000
if cap > 0:
    text = text[-cap:]

sys.stdout.write("%d\t%d\t%d\t%s\n" % (turns, tool_calls, size, path))
sys.stdout.write(text)
PYEOF

header="$(head -n 1 "$WORK" 2>/dev/null || true)"
TURNS="$(printf '%s' "$header" | cut -f1)"
TOOL_CALLS="$(printf '%s' "$header" | cut -f2)"
TBYTES="$(printf '%s' "$header" | cut -f3)"
TPATH="$(printf '%s' "$header" | cut -f4)"
case "$TURNS" in ''|*[!0-9]*) TURNS=0 ;; esac
case "$TOOL_CALLS" in ''|*[!0-9]*) TOOL_CALLS=0 ;; esac
case "$TBYTES" in ''|*[!0-9]*) TBYTES=0 ;; esac

# emit_row <status> [rubric_json] [error]
emit_row() {
    STATUS_VAL="$1" RUBRIC_VAL="${2:-}" ERR_VAL="${3:-}" \
    TS_VAL="$(ts_iso)" SID_VAL="$payload" \
    BOT_VAL="${BOT_ID:-unknown}" FLEET_VAL="${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}" \
    TURNS_VAL="$TURNS" TOOLS_VAL="$TOOL_CALLS" TB_VAL="$TBYTES" \
    DC_VAL="$DIGEST_CHARS" MODEL_VAL="$MODEL" TPATH_VAL="$TPATH" \
    python3 - >>"$OUT" 2>/dev/null <<'PYEOF' || true
import json, os

def env(k, d=""):
    return os.environ.get(k) or d

try:
    p = json.loads(env("SID_VAL") or "{}")
except (json.JSONDecodeError, ValueError):
    p = {}

row = {
    "ts": env("TS_VAL"),
    "session_id": p.get("session_id") or "",
    "bot": env("BOT_VAL"),
    "fleet": env("FLEET_VAL"),
    "cwd": p.get("cwd") or "",
    "reason": p.get("reason") or "",
    "status": env("STATUS_VAL"),
    "turns": int(env("TURNS_VAL", "0")),
    "tool_calls": int(env("TOOLS_VAL", "0")),
    "transcript_bytes": int(env("TB_VAL", "0")),
    "digest_chars": int(env("DC_VAL", "0")),
    "model": env("MODEL_VAL"),
    # capture's session-mode rubric, reused verbatim as the schema
    "context": "", "worked": "", "failed": "", "would_change": "", "reusable": "",
}

rub = env("RUBRIC_VAL")
if rub:
    try:
        d = json.loads(rub)
        if isinstance(d, dict):
            for k in ("context", "worked", "failed", "would_change", "reusable"):
                v = d.get(k)
                row[k] = v if isinstance(v, str) else ("" if v is None else json.dumps(v))
    except (json.JSONDecodeError, ValueError):
        row["status"] = "error"
        row["error"] = "model returned unparseable JSON"

e = env("ERR_VAL")
if e:
    row["error"] = e

print(json.dumps(row, ensure_ascii=False))
PYEOF
}

DIGEST_CHARS=0

# --- Qualifying gate ---------------------------------------------------------
# Below the floor: record the session, spend nothing. Distinct from an `ok` row
# with empty rubric fields, which is the model finding nothing notable.
if [ "$TURNS" -lt "$MIN_TURNS" ]; then
    emit_row "skipped" "" "below min_turns=$MIN_TURNS"
    rm -f "$WORK" 2>/dev/null || true
    exit 0
fi

DIGEST_CHARS="$(wc -c <"$WORK" 2>/dev/null | tr -d ' ' || printf '0')"
case "$DIGEST_CHARS" in ''|*[!0-9]*) DIGEST_CHARS=0 ;; esac

CLAUDE="${CLAUDE_BIN:-claude}"
if ! command -v "$CLAUDE" >/dev/null 2>&1; then
    emit_row "error" "" "model binary not found on PATH=$PATH"
    rm -f "$WORK" 2>/dev/null || true
    exit 0
fi

# --- Stage 2: Haiku extraction ----------------------------------------------
PROMPT_FILE="$(safe_mktemp)"
{
    printf '%s\n' "Distil this Claude Code session transcript into ONE JSON object."
    printf '%s\n' "Output ONLY the JSON object — no prose, no code fence."
    printf '%s\n' ""
    printf '%s\n' "Keys (all strings, all required, empty string when nothing qualifies):"
    printf '%s\n' '  context      — one line: the task or situation this session covered.'
    printf '%s\n' '  worked       — a specific tool/approach that helped, and why. Concrete.'
    printf '%s\n' '  failed       — what broke, the root cause, what was tried first. Concrete.'
    printf '%s\n' '  would_change — the specific alternative for next time.'
    printf '%s\n' '  reusable     — only if genuinely generalizable beyond this session.'
    printf '%s\n' ""
    printf '%s\n' "Quality gate: drop any field that is vague, obvious, or duplicative."
    printf '%s\n' "An EMPTY field is correct and expected when a session did routine work."
    printf '%s\n' "Do NOT invent findings to fill fields — a session where nothing notable"
    printf '%s\n' "happened must come back with empty fields and only context filled."
    printf '%s\n' "Never include credentials, tokens, or connection strings."
    printf '%s\n' ""
    printf '%s\n' "--- TRANSCRIPT (tail) ---"
    tail -n +2 "$WORK"
} >"$PROMPT_FILE" 2>/dev/null || true

RAW="$(with_timeout "$MODEL_TIMEOUT" "$CLAUDE" -p --model "$MODEL" <"$PROMPT_FILE" 2>/dev/null || true)"
rm -f "$PROMPT_FILE" "$WORK" 2>/dev/null || true

if [ -z "$RAW" ]; then
    emit_row "error" "" "model returned no output (timeout ${MODEL_TIMEOUT}s or auth failure)"
    exit 0
fi

# Tolerate a fenced or prose-wrapped reply: take the outermost JSON object.
RUBRIC="$(RAW_VAL="$RAW" python3 -c '
import json, os, re, sys
raw = os.environ.get("RAW_VAL") or ""
m = re.search(r"\{.*\}", raw, re.S)
if not m:
    sys.exit(1)
try:
    sys.stdout.write(json.dumps(json.loads(m.group(0))))
except Exception:
    sys.exit(1)
' 2>/dev/null || true)"

if [ -z "$RUBRIC" ]; then
    emit_row "error" "" "model output carried no JSON object"
    exit 0
fi

emit_row "ok" "$RUBRIC" ""
exit 0
