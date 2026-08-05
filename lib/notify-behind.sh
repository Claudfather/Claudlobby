#!/usr/bin/env bash
# notify-behind.sh — the source-currency nudge (notify-only, never pulls).
#
# Reports how far each framework checkout is behind, for EVERY repo the fleet
# runs on rather than claudlobby alone (#1009). Raises a FLEET NOTICE when
# behind; stays silent when current. Applying updates is update-siblings.sh's
# job — this script reports and nothing else, so a daily signal exists
# independently of the weekly apply window and keeps working on a host that
# never enrolled it.
#
# TWO DISTANCES, reported separately, because they answer different questions
# and #1009 is the case where conflating them loses the answer:
#
#   behind the newest TAG  — you are running an old release. Actionable by you:
#                            update-siblings.sh will fast-forward it.
#   release behind MAIN    — you are on the newest release, but fixes exist
#                            upstream that were never cut into one. NOT
#                            actionable by pulling; the fix is to cut a release.
#
# Tonight's incident is exactly the second shape and had no way to be said:
# Claudron sat at v0.4.0 — the newest tag, so "up to date" by any release test —
# while main carried 16 commits including a silent tag-corruption fix and a 95x
# status speedup. A tag-only check reads green; a main-only check demands
# pulling unreleased dev code onto a production fleet. Reporting both lets the
# operator choose, and names the release-cutting case out loud instead of
# leaving it to be inferred from a version number.
#
# Watched set: discover_framework_checkouts (lib-common.sh) — derived from what
# is installed, not a path list. See that helper for why.
#
# Quiet-failure discipline: an offline host or a non-git root logs + exits 0,
# leaving a script_error breadcrumb in state/events — a missed nudge is
# low-urgency and must never become alert noise. One repo's failure never
# aborts the sweep.
#
# Notices are DEBOUNCED per (repo, event) via notify_currency, unlike the
# single-repo version this replaces. That mattered less when the only watched
# repo self-cleared on the next pull; it matters now, because source_release_gap
# is the normal state of an actively-developed sibling and clears only when a
# human cuts a release. Daily undebounced, that is ~365 Telegram posts a year
# on a condition nobody can resolve today — training operators to ignore FLEET
# NOTICE, which is the failure class #1009 is itself an instance of.
#
# Usage: notify-behind.sh [<fleet-name>]
#   The optional fleet name scopes signal routing; the composed host unit
#   passes none, and routing falls back across local/<fleet>/runtime/bots.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

FLEET="${1:-${CLAUDLOBBY_FLEET:-}}"
BOTS_DIR="$(resolve_bots_dir "$FLEET")"
LOG="${CLAUDLOBBY_ROOT}/state/notify-behind.log"
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

echo "$ts WATCHING ${#WATCHED[@]} repo(s): ${WATCHED[*]}" >> "$LOG"

for repo in "${WATCHED[@]}"; do
    name=$(basename "$repo")

    if ! with_timeout 120 git -C "$repo" fetch --quiet --tags origin 2>>"$LOG"; then
        echo "$ts [$name] FETCH FAILED — source currency unknown (nudge skipped)" >> "$LOG"
        emit_script_error "" "notify-behind.sh" 1 \
            "git fetch origin failed for $name — source currency unknown"
        continue
    fi

    branch=$(repo_default_branch "$repo")
    if ! behind=$(git -C "$repo" rev-list --count "HEAD..origin/$branch" 2>>"$LOG"); then
        echo "$ts [$name] SKIP — no origin/$branch to compare against" >> "$LOG"
        continue
    fi

    # Empty tag == no release track, so the default branch is the track.
    # repo_newest_tag owns that rule and the reasoning, for both scripts.
    tag=$(repo_newest_tag "$repo")

    if [ -z "$tag" ]; then
        if [ "$behind" -gt 0 ]; then
            echo "$ts [$name] BEHIND origin/$branch by $behind (untagged repo) — notice raised" >> "$LOG"
            notify_currency "$name" "source_behind" "$behind" \
                "$name on $(hostname) is $behind commit(s) behind origin/$branch — apply with: git -C $repo pull --ff-only"
        else
            echo "$ts [$name] IN SYNC with origin/$branch" >> "$LOG"
            currency_clear "$name" "source_behind"
        fi
        continue
    fi

    tag_behind=$(git -C "$repo" rev-list --count "HEAD..$tag" 2>/dev/null || echo 0)
    if [ "${tag_behind:-0}" -gt 0 ]; then
        # Behind a cut release — the case update-siblings.sh can actually fix.
        echo "$ts [$name] BEHIND TAG $tag by $tag_behind — notice raised" >> "$LOG"
        notify_currency "$name" "source_behind" "$tag_behind" \
            "$name on $(hostname) is $tag_behind commit(s) behind release $tag — apply with: git -C $repo pull --ff-only"
    elif [ "$behind" -gt 0 ]; then
        # On the newest release, but main has moved. Deliberately NOT phrased as
        # "pull": on a versioned dependency that would mean running unreleased
        # dev code. The remedy named is cutting a release — a human decision.
        # This is the shape #1009 was filed from and could not previously say.
        echo "$ts [$name] AT RELEASE $tag, main +$behind — release-gap notice raised" >> "$LOG"
        notify_currency "$name" "source_release_gap" "$behind" \
            "$name on $(hostname) is at its newest release ($tag) but origin/$branch is $behind commit(s) ahead — unreleased fixes are not deployed; cut a release or upgrade deliberately"
    else
        echo "$ts [$name] IN SYNC with origin/$branch and release $tag" >> "$LOG"
        currency_clear "$name" "source_behind"
        currency_clear "$name" "source_release_gap"
    fi
done
exit 0
