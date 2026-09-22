#!/bin/bash
# vault-git-guard.sh — PreToolUse: no bot rewrites git STATE inside the vault.
#
# WHY A HOOK RATHER THAN AN INSTRUCTION (the gh-mention-guard.sh argument, and
# it applies harder here). A live host's vault clone sat on a side branch for a
# month; 59 commits of knowledge accrued off the default branch, 53 of them on
# no remote, and the rebase that finally tried to reconcile it was killed
# mid-pick and left the tree detached for twelve days with the wrong files
# checked out. A fleet's mission, charter and projects manifest vanished from
# disk and a `generate` then composed from the reverted manifest. Prose telling
# bots not to do that existed; it is not a mechanism.
#
# SCOPE IS DECIDED BEFORE THE VERB, ALWAYS.
# The dangerous failure here is NOT missing a rebase in the vault — Claudron's
# own sync refuses a side branch as the belt to this hook's braces. It is
# refusing legitimate git work in a bot's own projects/ checkout, on EVERY bot
# at once, because a composed hook is live the instant `generate` writes it and
# there is no canary window (documentation/fleet-update-lifecycle.md). So the
# decider answers "does this git invocation point inside the vault" first, and
# anything that does not is allowed without its verb ever being read.
#
# FAILS OPEN, LOUDLY. Missing jq or python3, an unparseable payload, or a
# decider that cannot answer allows the call and leaves a breadcrumb. Blocking
# every git command fleet-wide is a worse outage than the hazard this guards.
#
# A bot with no CLAUDRON_VAULT_PATH gets no decision at all: there is no vault
# to protect, and a guard that fired there would be refusing work it cannot
# have a reason to refuse.
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_allow() { exit 0; }   # no decision — normal permission flow applies

# --- step 1: the zero-fork prefilter ----------------------------------------
# Before sourcing anything and before any fork. A payload with no `git` in it
# cannot be a git command. This DELIBERATELY over-matches — `github`, `digit`
# and `legitimate` all contain it — because the costs are not symmetric: a
# false positive costs one jq, a false negative costs a wedged vault.
payload="$(cat)"
case "$payload" in
*git*) ;;
*) _allow ;;
esac

# --- step 1b: second zero-fork prefilter ------------------------------------
# A fast REJECT, never a fast accept: a payload containing no literal `Bash`
# anywhere cannot be a Bash tool call, so there are no false negatives here.
case "$payload" in
*Bash*) ;;
*) _allow ;;
esac

_bail() { # <reason> — fail open, but leave a breadcrumb
    # lib-common is sourced HERE, not at the top: it is thousands of lines of
    # bash and every tool call whose payload merely CONTAINS "git" would
    # otherwise pay to parse it.
    # shellcheck source=lib-common.sh
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_script_error >/dev/null 2>&1; then
        emit_script_error "" "vault-git-guard.sh" 1 \
            "$1 — vault git-state guard INACTIVE" 2>/dev/null || true
    fi
    _allow
}

_event() { # <event> <detail> — count a fail-open or a denial on the plane
    . "$LIB_DIR/lib-common.sh" 2>/dev/null || true
    if command -v emit_fleet_event >/dev/null 2>&1; then
        emit_fleet_event "$1" "$2" 2>/dev/null || true
    fi
}

_deny() { # <reason>
    jq -nc --arg r "$1" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0
}

# --- the vault must exist for this hook to have a subject -------------------
[ -n "${CLAUDRON_VAULT_PATH:-}" ] || _allow
VAULT="$(realpath -m "$CLAUDRON_VAULT_PATH" 2>/dev/null || true)"
[ -n "$VAULT" ] || _allow

command -v jq >/dev/null 2>&1 || _bail "jq not available"
PY_BIN="$(command -v python3 || true)"
[ -n "$PY_BIN" ] || _bail "python3 not available"
DECIDER="$LIB_DIR/vault-git-decide.py"
[ -r "$DECIDER" ] || _bail "vault-git-decide.py missing"

tool="$(jq -r '.tool_name // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
case "$tool" in
Bash) ;;
*) _allow ;;
esac

cmd="$(jq -r '.tool_input.command // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"
[ -n "$cmd" ] || _allow
cwd="$(jq -r '.cwd // empty' <<<"$payload" 2>/dev/null)" || _bail "unparseable hook payload"

verdict="$("$PY_BIN" "$DECIDER" --vault "$VAULT" --cwd "$cwd" --command "$cmd" 2>/dev/null)" \
    || _bail "decider failed"
detail="${verdict#*$'\t'}"
verdict="${verdict%%$'\t'*}"

case "$verdict" in
unresolved)
    # Allowed, and COUNTED. The payload named a target this hook cannot read
    # and carried no cwd to fall back to, so no claim can be made about where
    # it points. Silence here would make the blind spot invisible.
    _event vault_guard_unresolved "$detail"
    _allow
    ;;
deny)
    _event vault_guard_denied "$detail"
    _deny "git ${detail} inside the vault is refused by the vault git-state guardrail: the vault clone stays on its default branch and is only ever fast-forwarded. Branch, rebase and reset in a projects/ checkout, and use \`claudron sync\` to move the vault. If the vault looks wedged, do NOT abort or reset it — report it. Ask the operator if you believe this is wrong."
    ;;
esac
_allow
