#!/usr/bin/env bash
# update-siblings.sh — fast-forward stale framework checkouts to the newest
# cut RELEASE, guarded, and say so every time it moves one (#1009).
#
# The applier half of source currency; notify-behind.sh is the reporter. Split
# because they run on different clocks for different reasons: reporting is
# daily and harmless, applying swaps code under a live fleet and is weekly.
#
# ---------------------------------------------------------------------------
# WHY A RELEASE TAG AND NOT origin/main
# ---------------------------------------------------------------------------
# A sibling here is a DEPENDENCY, not a work-in-progress: Claudron backs every
# bot's vault door. Auto-pulling origin/main would put unreleased dev code
# (0.5.0.dev0 as of writing) onto four production fleets with no human in the
# loop — the inverse of #1009's incident and a worse one, because it fails
# forward into code nobody chose to ship.
#
# The cost of that choice is explicit and must not be hidden: when main carries
# fixes that were never released, this script correctly does nothing, and
# notify-behind.sh raises `source_release_gap` naming exactly that. That is the
# situation #1009 was filed from — Claudron at v0.4.0, the newest tag, with two
# data-integrity fixes sitting unreleased on main. The remedy there is to cut a
# release, which is a human decision, not an unattended pull.
#
# A repo with no release tags ships by merging, so its default branch IS its
# track — see repo_newest_tag, which owns that rule for both scripts. There is
# deliberately no "track main instead" env knob: the composer emits only
# CLAUDLOBBY_ROOT and PATH into a host unit, so such a knob could never reach
# the scheduled run, and a flag the timer cannot set is scaffolding that only
# ever desynchronises the reporter from the applier.
#
# $CLAUDLOBBY_ROOT IS DELIBERATELY NOT UPDATED HERE. Pulling the compositor
# itself is root-self-update, which system.yaml states ships "behind explicit
# toggles via their own plans, never here" — and it is not the same decision as
# updating a dependency: this script lives IN that repo, bash reads a script
# incrementally by file offset, so fast-forwarding claudlobby would rewrite
# update-siblings.sh underneath the running interpreter. notify-behind.sh still
# REPORTS the root; applying is the operator's.
#
# ---------------------------------------------------------------------------
# WHY THIS CLOCK
# ---------------------------------------------------------------------------
# Editable installs make the swap immediate: /home/user/claudron resolves the
# `claudron` import straight to the checkout, so a pull changes the CLI for the
# NEXT subprocess call — no reinstall, no restart, no warning. A running session
# keeps the module it imported at start, but every fresh `claudron` invocation
# it makes crosses the version boundary mid-task.
#
# So this runs weekly, in the maintenance block, 30 minutes ahead of
# weekly-worker-restart (Sun 05:00) — the same stage-then-apply-at-a-restart
# shape update-claude-code.sh already uses for the binary, which stages daily
# and lets the weekly bounce apply it.
#
# Stated honestly rather than overclaimed: weekly-worker-restart is dormant by
# default, so on a fleet that never enrolled it the exposure runs until each
# bot's next natural restart. That is the same exposure the staged binary
# already carries, it is bounded by the guards below, and every crossing is on
# the record via the sibling_updated event. A fleet that wants the window tight
# enrolls the weekly restart.
#
# ---------------------------------------------------------------------------
# GUARDS — a pull that eats somebody's work is worse than a stale sibling
# ---------------------------------------------------------------------------
#   * dirty working tree, local unpushed commits, or detached HEAD → SKIP +
#     notice, never a stash and never a force (repo_pull_blocker).
#   * fast-forward ONLY — never merge, never rebase. A checkout that cannot
#     fast-forward has diverged, which is a human's to resolve.
#   * every movement emits sibling_updated. #1009 is a defect about an
#     invisible sibling; an auto-updating sibling that says nothing is the same
#     defect wearing better clothes.
#
# Usage: update-siblings.sh [<fleet-name>] [--dry-run]
#   --dry-run reports what it would do and touches nothing.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

DRY_RUN=0
FLEET=""
for arg in "$@"; do
    case "$arg" in
    -h | --help) show_help "${BASH_SOURCE[0]}"; exit 0 ;;
    --dry-run) DRY_RUN=1 ;;
    *) [ -z "$FLEET" ] && FLEET="$arg" ;;
    esac
done
FLEET="${FLEET:-${CLAUDLOBBY_FLEET:-}}"

BOTS_DIR="$(resolve_bots_dir "$FLEET")"
LOG="${CLAUDLOBBY_ROOT}/state/update-siblings.log"
STATE_DIR="${CLAUDLOBBY_ROOT}/state/currency"
mkdir -p "$STATE_DIR" 2>/dev/null || true
setup_log_dir "$LOG"

ts=$(ts_iso)

if ! git -C "$CLAUDLOBBY_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "$ts SKIP — $CLAUDLOBBY_ROOT is not a git checkout" >> "$LOG"
    exit 0
fi

WATCHED=()
while IFS= read -r _r; do
    [ -n "$_r" ] && WATCHED+=("$_r")
done < <(discover_framework_checkouts)

echo "$ts START dry_run=$DRY_RUN watching ${#WATCHED[@]}: ${WATCHED[*]}" >> "$LOG"

SELF=$(git -C "$CLAUDLOBBY_ROOT" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$CLAUDLOBBY_ROOT")

for repo in "${WATCHED[@]}"; do
    name=$(basename "$repo")

    if [ "$repo" = "$SELF" ]; then
        echo "$ts [$name] SKIP — the compositor itself is not auto-updated (see header)" >> "$LOG"
        continue
    fi

    if ! with_timeout 120 git -C "$repo" fetch --quiet --tags origin 2>>"$LOG"; then
        echo "$ts [$name] FETCH FAILED — skipped" >> "$LOG"
        emit_script_error "" "update-siblings.sh" 1 \
            "git fetch origin failed for $name — update skipped"
        continue
    fi

    # Resolve the target BEFORE the safety check, so a blocked repo can still
    # report whether it was actually behind — "skipped because dirty" is only
    # actionable when the operator knows an update was waiting.
    target=$(repo_currency_target "$repo")

    if ! behind=$(git -C "$repo" rev-list --count "HEAD..$target" 2>>"$LOG"); then
        echo "$ts [$name] SKIP — cannot compare against $target" >> "$LOG"
        emit_script_error "" "update-siblings.sh" 1 \
            "$name: cannot resolve $target — currency unknown, repo unwatched"
        continue
    fi

    if [ "${behind:-0}" -eq 0 ]; then
        echo "$ts [$name] CURRENT at $target" >> "$LOG"
        currency_clear "$name" "sibling_update_blocked"
        currency_clear "$name" "sibling_update_failed"
        continue
    fi

    blocker=$(repo_pull_blocker "$repo")

    # --dry-run short-circuits ABOVE every side effect, notices included. A
    # dry run that pages the operator is not a dry run, and the house
    # convention (orphan-browser-reaper.sh) is "reports without killing or
    # notifying".
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ -n "$blocker" ]; then
            echo "$ts [$name] DRY-RUN would SKIP ($blocker) — $behind behind $target" >> "$LOG"
        else
            echo "$ts [$name] DRY-RUN would fast-forward $behind commit(s) to $target" >> "$LOG"
        fi
        continue
    fi

    if [ -n "$blocker" ]; then
        echo "$ts [$name] BLOCKED ($blocker) — $behind behind $target, not pulling" >> "$LOG"
        notify_currency "$name" "sibling_update_blocked" "$behind" \
            "$name on $(hostname) is $behind commit(s) behind $target but was NOT updated: $blocker — resolve it by hand, then: git -C $repo pull --ff-only"
        continue
    fi

    from=$(git -C "$repo" rev-parse --short HEAD)
    # --ff-only on merge, not pull: pull would consult the branch's configured
    # rebase/merge behaviour, and a repo with pull.rebase=true would rebase the
    # checkout instead of refusing. merge --ff-only cannot do anything but
    # fast-forward or fail.
    if ! git -C "$repo" merge --ff-only "$target" >>"$LOG" 2>&1; then
        # A DISTINCT type from sibling_update_blocked, and the distinction is
        # load-bearing rather than cosmetic. "We refused, because a person is
        # working here" and "we tried and git would not" are different events
        # for the operator, and they are the only thing that can tell the two
        # apart in a test: `git merge --ff-only` refuses a dirty tree on its
        # own, so asserting merely that HEAD did not move passes just as well
        # with the guards deleted. It did — that is how this was found.
        echo "$ts [$name] FF FAILED — diverged from $target, human needed" >> "$LOG"
        notify_currency "$name" "sibling_update_failed" "$target" \
            "$name on $(hostname) could not fast-forward to $target (diverged) — resolve by hand: git -C $repo status"
        continue
    fi
    to=$(git -C "$repo" rev-parse --short HEAD)

    echo "$ts [$name] UPDATED $from -> $to ($behind commit(s) to $target)" >> "$LOG"
    # Loud by construction. A silent auto-update is #1009 inverted: the fleet
    # would again be running code nobody knew had changed.
    notify_currency "$name" "sibling_updated" "$to" \
        "$name on $(hostname) fast-forwarded $from -> $to ($behind commit(s)) to $target — running sessions pick it up on their next $name call; restart to be certain"
done

echo "$ts DONE" >> "$LOG"
exit 0
