#!/usr/bin/env bash
# gh-mention-guard.sh — stop bots @-mentioning strangers on GitHub (#1019).
#
# PreToolUse hook. Defuses `@handle` in GitHub-bound content before the tool
# runs, so a teammate or product reference cannot notify a real person.
#
# ---------------------------------------------------------------------------
# WHY A HOOK AND NOT AN INSTRUCTION
# ---------------------------------------------------------------------------
# A bot wrote `@vera` in a PR comment; GitHub resolved it to Vera Clemens, a
# real person unconnected to this project, and emailed her. She asked us to
# stop. Every fleet bot name is a real account, and the class is wider than bot
# names: `Botfather` is a real user, present in our issues only because we
# documented Telegram's BotFather. So it is "any @word that happens to be a
# real handle" — unbounded, and it grows without us doing anything.
#
# Nothing in library/ ever instructs @-mentioning on GitHub. What it DOES
# instruct, correctly, is Telegram tagging. The habit leaks across surfaces —
# a correct convention applied mid-prose to the wrong one, which another
# instruction cannot catch. TELEGRAM IS UNTOUCHED by this hook.
#
# ---------------------------------------------------------------------------
# THE RULE (allowlist inversion)
# ---------------------------------------------------------------------------
#   1. A composed bot name is ALWAYS rewritten — it cannot be allowlisted.
#   2. Any other handle is rewritten UNLESS explicitly allowlisted.
#
# (1) beats (2) deliberately: without it someone eventually allowlists a bot's
# name meaning OUR bot and silently re-arms the original bug.
#
# Default-deny is right because the action is REWRITE, not block. A false
# positive costs backticks — `Botfather` is better prose for a product name
# anyway — while a false negative emails someone who asked us to stop. Those
# costs are not comparable.
#
# ---------------------------------------------------------------------------
# TWO SURFACES, TWO REPLACEMENTS — the asymmetry is a safety property
# ---------------------------------------------------------------------------
#   mcp__github__*  ->  @vera becomes `vera`   (backticks; safe in a JSON field)
#   Bash `gh …`     ->  @vera becomes vera     (bare; NEVER backticks)
#
# A comment body normally sits inside a double-quoted shell string, where a
# backtick is COMMAND SUBSTITUTION. Verified: the naive backtick rewrite makes
# the shell EXECUTE the handle, turning a notification bug into arbitrary code
# execution. Both forms defeat the harm — GitHub only notifies on a literal
# `@handle`.
#
# ---------------------------------------------------------------------------
# THE CHEAP PATH STAYS CHEAP
# ---------------------------------------------------------------------------
# This runs on EVERY tool call, on every bot, on a Pi. So the common case —
# a payload with no `@` in it at all — must cost nothing:
#
#   1. literal `@` test on the raw payload   (bash builtin, ZERO forks)
#   1b. literal `Bash`/`mcp__github__` test  (bash builtin, ZERO forks)
#   2. tool_name / surface check             (jq only)
#   3. the Python rewriter                   (only if 1 and 2 both pass)
#
# lib-common.sh is NOT sourced on any of those paths — only inside _bail, the
# error path. Sourcing it costs ~50ms on a Pi and nothing above step 3 uses it.
#
# Most tool calls exit at step 1 and never fork anything. Measured overhead is
# recorded in the PR; re-measure if you add work above step 3.
#
# ---------------------------------------------------------------------------
# FAILS OPEN, LOUDLY
# ---------------------------------------------------------------------------
# A missing manifest, absent jq/python, or an unparseable payload allows the
# call and emits a script_error breadcrumb. Blocking every GitHub write across
# the fleet on a missing file is a worse outage than the bug this guards, and
# the manifest is absent only if `generate` has not run — already broken.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_allow() { exit 0; } # no decision — normal permission flow applies

# --- step 1: the zero-fork prefilter ----------------------------------------
# Deliberately BEFORE sourcing lib-common or spawning jq. A payload with no `@`
# cannot contain a mention, and that is the overwhelming majority of tool calls.
payload="$(cat)"
case "$payload" in
*@*) ;;
*) _allow ;;
esac

# --- step 1b: second zero-fork prefilter ------------------------------------
# jq costs ~40ms per invocation on a Pi and it is the dominant cost once the `@`
# test passes — emails, decorators and file paths make that path common. This
# rejects ONLY when it is certain: a payload containing neither the literal
# `Bash` nor `mcp__github__` anywhere cannot be a Bash or GitHub-MCP call, so
# there are no false negatives. Note the direction — it is a fast REJECT, never
# a fast accept; anything that might match falls through to the real jq check.
case "$payload" in
*Bash* | *mcp__github__*) ;;
*) _allow ;;
esac

_bail() { # <reason> — fail open, but leave a breadcrumb
    # lib-common is sourced HERE rather than at the top: it is ~2400 lines of
    # bash and measured 50ms to parse on a Pi, which every tool call whose
    # payload merely CONTAINS an `@` would otherwise pay — emails, decorators
    # and file paths are common. Nothing above this point needs it.
    # shellcheck source=lib-common.sh
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_script_error >/dev/null 2>&1; then
        emit_script_error "" "gh-mention-guard.sh" 1 "$1 — GitHub mention guard INACTIVE" 2>/dev/null || true
    fi
    _allow
}

command -v jq >/dev/null 2>&1 || _bail "jq not available"
PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || _bail "python3 not available"
REWRITER="$LIB_DIR/mention-rewrite.py"
[ -r "$REWRITER" ] || _bail "mention-rewrite.py missing"

HOST_DIR="${CLAUDLOBBY_ROOT:-}/runtime/_host"
BOTS_FILE="${GH_MENTION_HANDLES_FILE:-$HOST_DIR/bot-handles}"
ALLOW_FILE="${GH_MENTION_ALLOWLIST_FILE:-$HOST_DIR/mention-allowlist}"
[ -r "$BOTS_FILE" ] || _bail "no bot-handles manifest at $BOTS_FILE (run: claudlobby generate)"

# --- step 2: is this tool call GitHub-bound? --------------------------------
# ONE jq on the common path. The Bash branch does not verify tool_name first:
# extracting .tool_input.command from a non-Bash payload simply yields empty,
# the writer patterns below then fail, and the call is allowed — the same
# answer a tool_name check would give, for one fork instead of two. Only the
# rarer MCP branch pays the extra lookup.
case "$payload" in
*'"mcp__github__'*)
    tool="$(jq -r '.tool_name // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
    case "$tool" in
    mcp__github__*) surface="mcp" ;;
    *) _allow ;;
    esac
    ;;
*)
    cmd="$(jq -r '.tool_input.command // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
    [ -n "$cmd" ] || _allow
    # Only gh invocations that WRITE something a person can be notified by.
    # `gh api … body=` is included deliberately: it is a real writer, and the
    # scrub of this very incident was performed with it.
    #
    # ─────────────────────────────────────────────────────────────────────
    # HAND-PROBING THIS GUARD? THREE WAYS TO GET A FALSE NEGATIVE.
    # All three were hit for real, by different people, on the same day. Each
    # produces "no rewrite", which is indistinguishable from the guard being
    # absent — so each reads as proof that it is dead. Copy the working probe
    # at the bottom rather than composing your own.
    #
    # 1. A READER measures nothing. `gh pr list`, `gh pr view`, `gh --version`
    #    are allowed BY DESIGN — nothing they do can notify anyone.
    #        gh pr list                      -> no fire (correct)
    #
    # 2. A WRITER VERB IS NOT ENOUGH. The patterns below need `gh` at a real
    #    word boundary: (^|[;&|(]|whitespace). Inside a quoted echo the
    #    preceding character is a QUOTE, so the boundary never matches and the
    #    correct verb still does not fire:
    #        echo "gh pr comment test vera"  -> no fire (LOOKS broken, is not)
    #        gh pr comment 1 --body "vera"   -> fires
    #
    # 3. DO NOT BATCH YOUR CONTROLS. One Bash call is ONE command string. If
    #    any part of it matches a writer, the rewrite applies to the WHOLE
    #    string — including the reader line you added as a control, which then
    #    falsely appears to have been stripped:
    #        gh pr comment 1 -b "vera"
    #        gh pr list # alex            <- alex gets rewritten too
    #    Run each control as its OWN call. This also bites indirectly: a probe
    #    merely CONTAINING the text `gh api ... body=` makes its own command a
    #    writer, so your test data is rewritten before your program reads it.
    #
    # WORKING PROBE — posts nothing, one call, writer-shaped:
    #     gh pr comment 1 --body "hi @<a-bot-name>"
    # Feed as the Bash tool_input.command; expect the sigil stripped on return.
    # ─────────────────────────────────────────────────────────────────────
    if grep -Eq '(^|[;&|(]|\s)gh\s+(issue|pr)\s+(comment|create|edit|review)\b' <<<"$cmd" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+api\b.*\b(body|title)=' <<<"$cmd" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+release\s+create\b' <<<"$cmd"; then
        surface="bash"
    else
        _allow
    fi
    ;;
esac

# --- step 3: rewrite ---------------------------------------------------------
_rw() { "$PY_BIN" "$REWRITER" --bots "$BOTS_FILE" --allow "$ALLOW_FILE" "$@"; }

if [ "$surface" = "bash" ]; then
    new_cmd="$(printf '%s' "$cmd" | _rw --style bare)" || _bail "rewriter failed"
    [ "$new_cmd" = "$cmd" ] && _allow
    jq -n --arg c "$new_cmd" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",updatedInput:{command:$c}}}'
    exit 0
fi

orig="$(jq -c '.tool_input' <<<"$payload" 2>/dev/null)"
updated="$(printf '%s' "$orig" | _rw --style backtick \
    --field body --field title --field commit_message --field message --field comment \
    2>/dev/null)" || _bail "rewriter failed"
[ -z "$updated" ] && _bail "rewriter produced nothing"
[ "$(jq -cS . <<<"$updated" 2>/dev/null)" = "$(jq -cS . <<<"$orig" 2>/dev/null)" ] && _allow

jq -n --argjson u "$updated" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",updatedInput:$u}}'
exit 0
