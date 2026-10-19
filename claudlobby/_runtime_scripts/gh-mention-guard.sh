#!/usr/bin/env bash
# gh-mention-guard.sh — stop bots @-mentioning strangers on GitHub (#1019).
#
# PreToolUse hook. Defuses `@handle` in GitHub-bound content before the tool
# runs, so a teammate or product reference cannot notify a real person.
#
# ---------------------------------------------------------------------------
# WHY A HOOK AND NOT AN INSTRUCTION
# ---------------------------------------------------------------------------
# A bot wrote `@vera` in a PR comment; GitHub resolved it to a
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
# the manifest is absent only if the staged composition was not activated — already broken.

set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_allow() { exit 0; } # no decision — normal permission flow applies

# --- step 1: the zero-fork prefilter ----------------------------------------
# Deliberately BEFORE sourcing lib-common or spawning jq. A payload with no `@`
# cannot contain a mention, and that is the overwhelming majority of tool calls.
payload="$(cat)"
# The `@` test alone is NOT sufficient, and assuming it was cost a silent
# bypass: content passed BY REFERENCE (--body-file, --notes-file, $(cat …),
# gh api's --input) carries its mentions in a FILE, so the command
# legitimately contains no `@` at all. Those markers must survive the
# prefilter or step 2b never runs.
case "$payload" in
*@* | *--body-file* | *--notes-file* | *'$(cat '* | *--input*) ;;
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
[ -r "$BOTS_FILE" ] || _bail "no bot-handles manifest at $BOTS_FILE (run: claudlobby --root <data-root> config plan --release <sealed-release-id>; then: claudlobby --root <data-root> host activate <plan-id> --install-directory <native-user-unit-dir>)"

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
    # A backslash-newline continues a line, and grep tests one line at a time:
    # unjoined, `gh api … \` and the `--input FILE` or `body=` on the next line
    # never meet, and the write passes as a read (#1537's review). Every test
    # below reads the joined text; step 3 still rewrites the original.
    _nl=$'\n'
    cmdj=${cmd//"\\$_nl"/ }
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
    # `gh api … --input FILE` sends FILE as the request body: the review and
    # comment POSTs the same-identity protocol teaches (#1537) go that way.
    if grep -Eq '(^|[;&|(]|\s)gh\s+(issue|pr)\s+(comment|create|edit|review)\b' <<<"$cmdj" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+api\b.*\b(body|title)=' <<<"$cmdj" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+api\b.*\s--input([= ]|$)' <<<"$cmdj" \
        || grep -Eq '(^|[;&|(]|\s)gh\s+release\s+create\b' <<<"$cmdj"; then
        surface="bash"
    else
        _allow
    fi
    ;;
esac

# --- step 2b: content passed BY REFERENCE ------------------------------------
# REWRITE WHAT THE TOOL CALL OWNS; REFUSE WHAT THE AUTHOR OWNS.
#
# That is the whole rule, and it explains why rewriting is right for a command
# string and wrong for a file without either being a special case. A command
# string is EPHEMERAL: it exists for one invocation, nobody reads it again, and
# rewriting it loses nothing while preserving the intent ("post this"). A file
# is DURABLE: someone authored it deliberately, may reuse it, may commit it —
# and an edit made behind their back is invisible at the moment it happens.
#
# This is not theoretical. This guard silently stripped every sigil from a test
# file WHILE IT WAS BEING AUTHORED (the command quoted a `gh … body=` example,
# which made the whole command writer-shaped). It was caught only because the
# tests failed. The by-reference case would do that directly and routinely.
#
# So: scan, never mutate. No mention -> allow untouched, which is the common
# case and costs nothing. A mention that would notify -> REFUSE, naming file,
# line, handle and the fix. Blocking costs one visible, recoverable turn;
# mutating costs an invisible change to somebody's artifact; allowing emails a
# stranger. Block is the cheapest of the three and the only one that teaches.
#
# vera found `--body-file`; it was seven forms in three classes.
_deny() { # <reason>
    jq -n --arg r "$1" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0
}

if [ "$surface" = "bash" ]; then
    # (a) STDIN — content lives in a pipeline that has not run. Unreadable, so
    # unscannable and unrewritable. The guard must not appear to cover a path it
    # cannot see (that is the manufactured all-clear shape), so it refuses and
    # names the one-line fix. Verified before ruling: nothing in claudlobby/_runtime_scripts/, library/
    # or claudlobby/ pipes stdin to gh, so this breaks no shipped tooling.
    if grep -Eq -- '--body-file[= ]+-([[:space:]]|$)|(body|title)=@-([[:space:]]|$)' <<<"$cmdj"; then
        _deny "gh reading the body from STDIN cannot be checked for @-mentions — the content is in a pipeline this hook cannot read, and every fleet bot name is a real GitHub account (#1019). Write the body to a temp file and pass --body-file <path>; the file is scanned and passes untouched when it holds no mention."
    fi

    # --- reading a path the way gh will -------------------------------------
    # This hook runs BEFORE the command, in the Bash tool's current directory,
    # so it reads a relative path from where the command STARTS. It reads the
    # file gh will send or it refuses; a best guess is the bypass. #1537's
    # review: `cd sub && gh api … --input review.json` passed on a clean
    # review.json beside the hook while gh sent a dirty sub/review.json.
    _PATH='("[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:]"'"'"';&|()<>]+)'
    _refpaths() { # <ERE prefix> — each path written after it, quotes kept, one per line
        grep -oE -- "$1$_PATH" <<<"$cmdj" | sed -E "s/^$1//"
    }
    _catpaths() { # each word inside a `$(cat …)` body, one per line
        local _l _w
        grep -oE -- '\$\(cat [^)]+\)' <<<"$cmdj" | sed -E 's/^\$\(cat //; s/\)$//' | while IFS= read -r _l; do
            read -r -a _w <<<"$_l"
            [ "${#_w[@]}" -gt 0 ] && printf '%s\n' "${_w[@]}"
        done
    }
    _literal() { # <path as written> — print the path the shell hands gh, or fail when the shell builds it as the command runs
        local p=$1
        case "$p" in
        \'*\')
            p=${p#\'}
            p=${p%\'}
            ;; # single quotes: nothing expands
        \"*\")
            p=${p#\"}
            p=${p%\"}
            case "$p" in *'$'* | *'`'* | *'\'*) return 1 ;; esac
            ;;
        *) case "$p" in *'$'* | *'`'* | *'\'* | *'*'* | *'?'* | *'['* | *'{'* | '~'*) return 1 ;; esac ;;
        esac
        printf '%s' "$p"
    }
    _gh_alone() { # gh is the whole command, so nothing runs before it that could change directory
        local bare
        grep -Eq '^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:];&|]*[[:space:]]+)*gh[[:space:]]' <<<"$cmdj" || return 1
        case "$cmdj" in *"$_nl"*) return 1 ;; esac
        bare=$(sed -E "s/'[^']*'|\"([^\"\\\\]|\\\\.)*\"//g" <<<"$cmdj") || return 1
        ! grep -Eq ';|\||(^|[^<>])&([^<>]|$)' <<<"$bare"
    }
    _cd_in_cmd() { # the command holds a way to change directory: cd, pushd, popd, source, eval, `.`, env -C, -execdir
        grep -Eq -- '(^|[^[:alnum:]_./-])(cd|pushd|popd)([[:space:];&|)]|$)|(^|[;&|({`]|\$\(|[[:space:]](if|then|else|elif|do|while|until))[[:space:]]*(source|eval|\.)[[:space:]]|(^|[^[:alnum:]_./-])env[[:space:]]([^;&|]*[[:space:]])?(-C|--chdir)|--chdir|[[:space:]]-(execdir|okdir)([[:space:]]|$)' <<<"$cmdj"
    }

    # (b) A FILE ON DISK — readable, so scan it. Covers --body-file and
    # --notes-file (space- or equals-separated, quoted or not: a quoted path was
    # never read before #1537's review), -F/--field/-f/--raw-field body=@FILE,
    # and --body "$(cat FILE)".
    #   - A relative path in a command that can change directory is refused:
    #     gh could send a different file than the one read here.
    #   - Unlike (b2), a relative path beside other commands is still read, and
    #     one built when the command runs, or not written yet, passes unread.
    #     The common one-line form writes the body with a heredoc in this same
    #     command, whose text step 3 rewrites, and refusing these would refuse
    #     that form. A list of ways to change directory is never complete, so
    #     this is the weaker rule; tightening it waits on a measure of how
    #     often the fleet uses each form (#1537's follow-up issue).
    while IFS= read -r _raw; do
        [ -n "$_raw" ] || continue
        _f=$(_literal "$_raw") || continue
        [ "$_f" = "-" ] && continue # STDIN: refused in (a)
        case "$_f" in
        /*) ;;
        *)
            if _cd_in_cmd; then
                _deny "$(printf '%s is a relative path in a command that can change directory (cd, pushd, popd, source, eval, env -C or find -execdir). This guard reads it from the directory the command starts in, before anything runs, so gh could send a different file (#1019). Pass an absolute path.' "$_f")"
            fi
            ;;
        esac
        [ -r "$_f" ] || continue # not written yet: nothing to scan
        if _hits=$("$PY_BIN" "$REWRITER" --bots "$BOTS_FILE" --allow "$ALLOW_FILE" --report < "$_f"); then
            continue # exit 0 => no mention => allow untouched
        fi
        _deny "$(printf '%s carries @-mentions that would notify real GitHub accounts (#1019):\n%s\nEvery fleet bot name is a real account, and so are handles like Botfather, latest and 216. Edit the file to use backticks (`name`) and retry — the file is NOT modified for you, because it is yours.' "$_f" "$_hits")"
    done <<<"$(_refpaths '--(body|notes)-file[=[:space:]]+'; _refpaths '(body|title)=@'; _catpaths)"

    # (b2) gh api --input FILE — the whole request body, usually JSON. Each way
    # the hook could read a different file than gh sends, or none, is refused,
    # because a guard that appears to cover a path it cannot see is the defect
    # (#1537), and one that reads its best guess repeats it:
    #   - `--input -` reads STDIN, unreadable here, as in (a);
    #   - a path built when the command runs ($VAR, $(…), backticks, ~, a glob,
    #     an escape) names no file the hook can read;
    #   - a RELATIVE path is read only when gh is the whole command: anything
    #     before gh could change directory, and no list of the ways is complete;
    #   - the same command writes FILE (`jq … > FILE; gh api … --input FILE`),
    #     so the hook would read its OLD content, or none;
    #   - FILE does not exist yet, so there is nothing to read.
    # The taught route (same-identity-fallback) writes the body in one command
    # and posts it with gh alone in the next, so it passes every rule above.
    # A readable FILE is scanned with --json-strings: each decoded string is
    # read as text, so a body line starting with a handle (`\n@name` in the
    # raw JSON) and a code fence count exactly as they do in a body file.
    while IFS= read -r _raw; do
        [ -n "$_raw" ] || continue
        _f=$(_literal "$_raw") || _deny "$(printf 'The --input path %s is built when the command runs (a variable, ~, a glob, an escape or a command substitution), so this guard cannot know which file gh will send, and it does not guess (#1019). Pass a literal path: an absolute one, or a relative one with gh api as the whole command.' "$_raw")"
        if [ "$_f" = "-" ]; then
            _deny "gh api --input - reads the request body from STDIN, which cannot be checked for @-mentions before the post (#1019). Write the body to a file in one command, then post it with --input <file> in the next."
        fi
        case "$_f" in
        /*) ;;
        *) _gh_alone || _deny "$(printf '%s is a relative path in a command that runs more than gh. This guard reads it from the directory the command starts in, before anything runs, and cannot tell whether something before gh changes directory (cd, pushd, a subshell, a sourced script), so gh could send a different file (#1019). Pass --input an absolute path, or post with gh api as the whole command, as the protocol does.' "$_f")" ;;
        esac
        _q=$(printf '%s' "$_f" | sed 's/[.[\*^$/]/\\&/g')
        if grep -Eq -- "(>|>>|\btee\b[^;&|]*|[[:space:]]-o[[:space:]]*)[[:space:]]*[\"']?${_q}[\"']?([[:space:];&|)]|$)" <<<"$cmdj"; then
            _deny "$_f is written by this same command, so the @-mention guard would read its old content or none before the post runs (#1019). Write it in one command, then post it with gh api --input in the next."
        fi
        [ -r "$_f" ] || _deny "$_f does not exist yet, so it cannot be checked for @-mentions before the post (#1019). Write the request body first, then post it with gh api --input in the next command."
        if _hits=$("$PY_BIN" "$REWRITER" --bots "$BOTS_FILE" --allow "$ALLOW_FILE" --report --json-strings < "$_f"); then
            continue
        fi
        _deny "$(printf '%s carries @-mentions that would notify real GitHub accounts (#1019):\n%s\nEvery fleet bot name is a real account, and so are handles like Botfather, latest and 216. Edit the text the request body is built from to use backticks (`name`), rebuild it, and retry: the file is NOT modified for you.' "$_f" "$_hits")"
    done <<<"$(_refpaths '--input[=[:space:]]+')"

    # (c) NOT COVERED, stated rather than pretended: `gh pr create --fill` takes
    # the body from commit messages, which is scannable via git log but is more
    # machinery than this belongs in. Declaring the gap is the honest move; a
    # guard that silently misses a path it appears to cover is the defect this
    # whole issue is about. Filed separately.
fi

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
