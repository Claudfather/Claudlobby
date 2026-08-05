#!/usr/bin/env bash
# gh-mention-guard.sh — stop bots @-mentioning each other on GitHub (#1019).
#
# PreToolUse hook. Rewrites `@<botname>` out of GitHub-bound content before the
# tool runs, so a teammate reference cannot notify a stranger.
#
# ---------------------------------------------------------------------------
# WHY THIS IS A HOOK AND NOT AN INSTRUCTION
# ---------------------------------------------------------------------------
# Every one of the fleet's 21 bot names is a real GitHub account, 19 of them
# real people — `vera` is Vera Clemens, who received our notifications and asked
# us to stop. The names collide because fleet bots are named after common first
# names and short handles were claimed a decade ago; any future bot named the
# same way collides too.
#
# Nothing in library/, templates/ or documentation/ ever tells a bot to write
# `@name` on GitHub. What the framework DOES instruct — correctly — is Telegram
# tagging (telegram-routing.md, worker-lifecycle.md, software-engineering.md),
# where `@name` is right and harmless. The habit then leaks onto GitHub. That is
# a correct convention applied to the wrong surface, mid-prose, which is exactly
# what another instruction cannot catch. Hence a mechanical guard.
#
# TELEGRAM IS UNTOUCHED. This hook only ever inspects GitHub-bound tool calls.
#
# ---------------------------------------------------------------------------
# TWO SURFACES, TWO REPLACEMENTS — and the asymmetry is a safety property
# ---------------------------------------------------------------------------
#   mcp__github__*  →  @vera becomes `vera`   (backticks; house style)
#   Bash `gh …`     →  @vera becomes vera     (bare; NO backticks)
#
# The Bash case must NOT use backticks, and this is the whole reason the two
# differ. A comment body is usually inside a double-quoted shell string, where a
# backtick is COMMAND SUBSTITUTION — rewriting `gh pr comment -b "thanks @vera"`
# into "thanks `vera`" would make the shell try to EXECUTE `vera`. Verified: the
# naive rewrite turns a notification bug into arbitrary command execution. So
# the shell surface strips the sigil instead. Both forms defeat the harm
# identically, because GitHub only notifies on a literal `@handle`.
#
# ---------------------------------------------------------------------------
# THE NAME LIST IS COMPOSED, HOST-WIDE
# ---------------------------------------------------------------------------
# Read from $CLAUDLOBBY_ROOT/runtime/_host/bot-handles, which `generate` writes
# from every fleet's FleetConfig.bots on the host. Never hardcoded: a literal
# list re-breaks the moment a bot is added (#1009's defect class). Host-wide,
# not fleet-scoped: cross-fleet references are routine — this fleet's issues
# name kev, craig, saul and clog constantly — and a fleet-scoped list would miss
# exactly the mentions written by someone with no relationship to that bot.
#
# Only bot names are rewritten. A real collaborator handle (the operator's own,
# a reviewer's) is left alone: this exists to stop US notifying people, not to
# strip every mention.
#
# FAILS OPEN, LOUDLY. A missing manifest, absent jq, or unparseable payload
# allows the call and emits a script_error breadcrumb. Blocking every GitHub
# write across the fleet on a missing file is a worse outage than the bug it
# guards, and the manifest is only absent if `generate` has not run — already a
# broken state. tests assert the manifest is composed so that state is caught
# before it reaches a bot.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh" 2>/dev/null || true

_allow() { exit 0; } # no decision — normal permission flow applies

_bail() { # <reason> — fail open, but leave a breadcrumb
    if command -v emit_script_error >/dev/null 2>&1; then
        emit_script_error "" "gh-mention-guard.sh" 1 "$1 — GitHub mention guard INACTIVE" 2>/dev/null || true
    fi
    _allow
}

command -v jq >/dev/null 2>&1 || _bail "jq not available"

payload="$(cat)"
[ -n "$payload" ] || _allow

tool="$(jq -r '.tool_name // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
[ -n "$tool" ] || _allow

# --- the composed, host-wide handle list ------------------------------------
HANDLES_FILE="${GH_MENTION_HANDLES_FILE:-${CLAUDLOBBY_ROOT:-}/runtime/_host/bot-handles}"
[ -r "$HANDLES_FILE" ] || _bail "no bot-handles manifest at $HANDLES_FILE (run: claudlobby generate)"

# One alternation of shell-safe names. Anchored with word boundaries at use.
names="$(grep -Ex '[A-Za-z0-9][A-Za-z0-9_-]*' "$HANDLES_FILE" 2>/dev/null | sort -u | paste -sd'|' -)"
[ -n "$names" ] || _bail "bot-handles manifest is empty"

# --- does this tool call reach GitHub? --------------------------------------
case "$tool" in
mcp__github__*) surface="mcp" ;;
Bash)
    cmd="$(jq -r '.tool_input.command // empty' <<<"$payload" 2>/dev/null)"
    # Only gh invocations that WRITE something a person can be notified by.
    # `gh api … body=` is included deliberately: it is a real writer, and the
    # scrub of this very incident was performed with it.
    if grep -Eq '(^|[;&|(]|\s)gh\s+(issue|pr)\s+(comment|create|edit|review)\b' <<<"$cmd" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+api\b.*\b(body|title)=' <<<"$cmd" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+release\s+create\b' <<<"$cmd"; then
        surface="bash"
    else
        _allow
    fi
    ;;
*) _allow ;;
esac

# --- rewrite ----------------------------------------------------------------
if [ "$surface" = "bash" ]; then
    # Bare replacement — see the safety note in the header. NEVER backticks here.
    new_cmd="$(sed -E "s/@($names)\b/\1/g" <<<"$cmd")"
    [ "$new_cmd" = "$cmd" ] && _allow
    jq -n --arg c "$new_cmd" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",updatedInput:{command:$c}}}'
    exit 0
fi

# MCP: rewrite every free-text field the GitHub tools carry. Backticks are safe
# here — the value is a JSON string, never shell input.
updated="$(jq --arg names "$names" '
    def scrub: if type == "string"
               then gsub("@(?<n>" + $names + ")\\b"; "`\(.n)`")
               else . end;
    .tool_input
    | with_entries(
        if (.key | test("^(body|title|commit_message|message|comment)$"))
        then .value |= scrub else . end)
' <<<"$payload" 2>/dev/null)" || _bail "mcp rewrite failed"

orig="$(jq -c '.tool_input' <<<"$payload" 2>/dev/null)"
[ "$(jq -c . <<<"$updated" 2>/dev/null)" = "$orig" ] && _allow

jq -n --argjson u "$updated" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",updatedInput:$u}}'
exit 0
