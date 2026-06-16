#!/bin/bash
# auto-deploy.sh — safe, self-healing platform-repo deploy for a claudlobby host.
#
# Keeps the host's $CLAUDLOBBY_ROOT git checkout current with its remote and
# applies the new code LIVE via reload-fleet.sh (Mechanism 1 — no restart, no
# context loss). Designed to run daily via the opt-in `auto-deploy` fleet timer.
#
# Safety model — it REFUSES rather than risk a bad deploy, and every refusal is a
# clean no-op the next run can retry:
#   * not a git checkout        -> skip (some installs are tarball/npm)
#   * working tree dirty        -> skip (local/human edits in flight)
#   * not on the deploy branch  -> skip (host parked on a feature branch; this is
#                                  why it never yanks a host off a mid-flight WIP
#                                  branch like an in-review socket-isolation branch)
#   * already up to date        -> no-op
#   * CI red on the deploy branch -> skip (don't ship a known-broken branch)
# Only after every gate passes does it `git pull --ff-only` and reload. A failed
# reload ROLLS THE CHECKOUT BACK to the pre-deploy SHA, re-generates, and is LOUD
# (deploy_failed event + manager alert via the shared emit_failure_alert path —
# the same primitive reload-fleet.sh uses, so the fleet update mechanisms share
# one alert path rather than forking it).
#
# Reuses, does not fork: reload-fleet.sh (apply), ci-health-check.sh (gate),
# with_lock / emit_failure_alert / install_error_trap / resolve_bots_dir / ts_iso.
#
# Config (env — overridable by the timer unit or for tests):
#   AUTO_DEPLOY_BRANCH   branch to track   (default: main)
#   AUTO_DEPLOY_REMOTE   git remote        (default: origin)
#   AUTO_DEPLOY_CI_GATE  1=gate on CI health, 0=skip the CI gate (default: 1)
#
# Usage: auto-deploy.sh [<fleet-name>]
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
export CLAUDLOBBY_ROOT
FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
BRANCH="${AUTO_DEPLOY_BRANCH:-main}"
REMOTE="${AUTO_DEPLOY_REMOTE:-origin}"
CI_GATE="${AUTO_DEPLOY_CI_GATE:-1}"

REPO_DIR="$CLAUDLOBBY_ROOT"
BOTS_DIR="$(resolve_bots_dir "$FLEET")"
mkdir -p "${CLAUDLOBBY_ROOT}/state"
LOG="${CLAUDLOBBY_ROOT}/state/auto-deploy.log"

log() { printf '%s %s\n' "$(ts_iso)" "$1" >> "$LOG"; }

# LOUD failure — the shared primitive reload-fleet.sh also uses, so the fleet
# update mechanisms share one alert path rather than forking it.
loud_fail() {
    local reason="$1"
    log "deploy_failed: $reason"
    emit_failure_alert "$BOTS_DIR" "deploy_failed" "$reason"
}

# A clean, expected refusal: log + exit 0 so the next timer run simply retries.
skip() {
    log "skip: $1"
    exit 0
}

# --- Health gates (refuse rather than risk a bad deploy) ---------------------
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || skip "$REPO_DIR is not a git checkout (tarball/npm install?)"

# Working tree must be clean — never pull over local or human edits.
if [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]; then
    skip "working tree dirty at $REPO_DIR — not pulling over local changes"
fi

# Must be on the deploy branch — never yank a host off a feature branch.
current_branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [ "$current_branch" != "$BRANCH" ]; then
    skip "on branch '$current_branch', not deploy branch '$BRANCH' — leaving host as-is"
fi

# Fetch — a fetch failure is worth alerting (network/auth), not a silent skip.
if ! git -C "$REPO_DIR" fetch --quiet "$REMOTE" "$BRANCH" 2>>"$LOG"; then
    loud_fail "git fetch $REMOTE $BRANCH failed"
    exit 1
fi

# Already current? -> no-op (idempotent short-circuit, like update-claude-code.sh).
behind="$(git -C "$REPO_DIR" rev-list --count "HEAD..$REMOTE/$BRANCH" 2>/dev/null || echo 0)"
if [ "$behind" -eq 0 ]; then
    log "no-op: already up to date with $REMOTE/$BRANCH"
    exit 0
fi

# CI gate — don't ship a known-broken branch. Unknown (no gh / API error, exit 2)
# fails OPEN so a gh outage can't freeze deploys; known-red (exit 1) skips.
if [ "$CI_GATE" = "1" ]; then
    ci_rc=0
    ( cd "$REPO_DIR" && "$LIB_DIR/ci-health-check.sh" --branch "$BRANCH" --quiet ) || ci_rc=$?
    if [ "$ci_rc" -eq 1 ]; then
        skip "CI red on $BRANCH — not deploying $behind commit(s) over a broken branch"
    elif [ "$ci_rc" -eq 2 ]; then
        log "warn: CI health unknown (gh missing / API error) — proceeding fail-open"
    fi
fi

# --- Deploy: capture rollback point, fast-forward, apply live ----------------
OLD_SHA="$(git -C "$REPO_DIR" rev-parse HEAD)"

# The whole pull -> reload -> (rollback) runs under one lock so two timer runs can
# never deploy concurrently. reload-fleet.sh takes its own distinct lock inside.
run_deploy() {
    log "deploying: $behind commit(s) from $REMOTE/$BRANCH (at $OLD_SHA)"

    if ! git -C "$REPO_DIR" pull --ff-only "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
        loud_fail "git pull --ff-only $REMOTE $BRANCH failed (diverged? non-fast-forward?)"
        return 1
    fi
    NEW_SHA="$(git -C "$REPO_DIR" rev-parse HEAD)"

    # Apply LIVE via reload-fleet (Mechanism 1) — reuse, don't fork the reload path.
    if "$LIB_DIR/reload-fleet.sh" "$FLEET" >>"$LOG" 2>&1; then
        log "deploy OK: $OLD_SHA -> $NEW_SHA, reloaded fleet '${FLEET:-<none>}' live"
        return 0
    fi

    # Reload failed -> roll the checkout back to the known-good SHA and re-generate
    # so the host is never left on un-reloaded new code. Safe by construction: the
    # tree was verified clean and we only fast-forwarded, so OLD_SHA's commits are
    # all on the remote and nothing local is lost.
    log "reload failed after pull — rolling back $NEW_SHA -> $OLD_SHA"
    if git -C "$REPO_DIR" reset --hard "$OLD_SHA" >>"$LOG" 2>&1; then
        if [ -n "$FLEET" ]; then
            claudlobby --fleet "$FLEET" generate >>"$LOG" 2>&1 \
                || log "warn: regenerate after rollback failed"
        fi
        loud_fail "deploy reload failed; rolled back $NEW_SHA -> $OLD_SHA"
    else
        loud_fail "deploy reload failed AND rollback to $OLD_SHA failed — host on $NEW_SHA, manual repair needed"
    fi
    return 1
}

# run_deploy logs + alerts on every path and returns the right code; the `|| exit`
# keeps with_lock in a condition so a clean failure exits 1 without the ERR trap
# double-emitting a script_error on top of run_deploy's own deploy_failed alert.
with_lock "${CLAUDLOBBY_ROOT}/state/auto-deploy.lock" run_deploy || exit 1
