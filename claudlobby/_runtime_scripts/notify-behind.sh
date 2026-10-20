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
# WHAT THE DATA ROOT COUNTS FROM (#2322): the release this host runs, never its
# own checkout. Under sealed releases nothing runs from the root checkout and
# nothing pulls it, so its HEAD says only how old that checkout is, and an
# activation never moves it. The root counts from the source revision of the
# release the host has selected; a sibling still counts from its HEAD, which
# is what runs. A release that cannot be read is said plainly
# (source_unknown), with no count in its place.
#
# Quiet-failure discipline: an offline host or a non-git root logs + exits 0,
# leaving a script_error breadcrumb on the plane — a missed nudge is
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

log() { printf '%s %s\n' "$(ts_iso)" "$*" >> "$LOG"; }

if ! git -C "$CLAUDLOBBY_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    log "SKIP — $CLAUDLOBBY_ROOT is not a git checkout"
    exit 0
fi

WATCHED=()
while IFS= read -r _r; do
    [ -n "$_r" ] && WATCHED+=("$_r")
done < <(discover_framework_checkouts)

log "WATCHING ${#WATCHED[@]} repo(s): ${WATCHED[*]}"
root_checkout=$(git -C "$CLAUDLOBBY_ROOT" rev-parse --show-toplevel 2>/dev/null) || root_checkout="$CLAUDLOBBY_ROOT"
host="$(hostname || echo unknown-host)"

# currency_outcome_phrase
# Render the verdict notify_currency just recorded, for the audit log.
#
# THE DEFECT THIS EXISTS FOR: each of the three sites below used to write
# "notice raised" to the log BEFORE calling notify_currency, and never read its
# result. So the log could only ever say one thing, which made it useless as a
# check and worse than useless as evidence: with the fallback Telegram token
# dead (#552) the log recorded a run of correctly-escalated notices while the
# journal carried ALERT-DELIVERY-FAILED for every one of them. Two artifacts
# disagreeing, and the one a human audits was the one asserting nothing was
# wrong. A log line that can only ever say one thing is not a check.
#
# Three outcomes, kept distinct. `suppressed` is not a failure — the debounce
# working as designed — but it is also not a notice, so folding it into either
# neighbour restates the original lie in a quieter register.
currency_outcome_phrase() {
    case "${_CURRENCY_OUTCOME:-suppressed}" in
        delivered)
            printf 'notice DELIVERED' ;;
        undelivered)
            printf 'notice RAISED BUT NOT DELIVERED (channel rejected it; see the ALERT-DELIVERY-FAILED journal line and the alert_delivery_failed ledger row)' ;;
        *)
            printf 'notice SUPPRESSED (debounced, already notified — nothing sent)' ;;
    esac
}

# running_release
# One line naming what this host runs: `selected <release-id> <source-revision>`,
# or `unknown <reason>`. These are the two reads runtime admission makes before
# it lets a bot start: the host's release selection, then that release's sealed
# manifest, its digest checked. The package's own readers make both, under the
# interpreter beside the composed CLI (as runtime-admission.sh runs it), so no
# file is parsed here. Not `host status`: before answering, it hashes every
# file of every installed release, which this answer does not need.
running_release() {
    # shellcheck source=cli-context.sh
    . "$LIB_DIR/cli-context.sh"
    local python="${CLAUDLOBBY_CLI:-}"
    python="${python%/*}/python"
    if ! _claudlobby_require_cli 2>>"$LOG" || [ ! -x "$python" ]; then
        echo "unknown CLAUDLOBBY_CLI names no composed CLI with its interpreter beside it"
        return 0
    fi
    "$python" -I -B - "$CLAUDLOBBY_ROOT" 2>>"$LOG" <<'PY'
import sys
from pathlib import Path


def running(root):
    from claudlobby.activation_state import read_selection
    from claudlobby.releases import read_release
    selected = read_selection(root)
    if selected is None:
        return "unknown no release is selected on this host"
    release_id = selected["release_id"]
    try:
        release = read_release(root, release_id, verify_files=False)
    except Exception as exc:
        return f"unknown release {release_id[:10]}: {exc}"
    return f"selected {release.release_id} {release.inputs.source_revision}"


try:
    line = running(Path(sys.argv[1]))
except Exception as exc:
    line = f"unknown {type(exc).__name__}: {exc}"
print(" ".join(line.split()))
PY
}

for repo in "${WATCHED[@]}"; do
    name=$(basename "$repo")
    apply_hint="git -C $repo pull --ff-only"

    if ! with_timeout 120 git -C "$repo" fetch --quiet --tags origin 2>>"$LOG"; then
        log "[$name] FETCH FAILED — source currency unknown (nudge skipped)"
        emit_script_error "" "notify-behind.sh" 1 \
            "git fetch origin failed for $name — source currency unknown"
        continue
    fi

    branch=$(repo_default_branch "$repo")
    anchor=HEAD
    subject="$name on $host"
    if [ "$repo" = "$root_checkout" ]; then
        apply_hint="build and seal an immutable Claudlobby release, stage its config plan, then run host activate as the operator; do not pull the running root"
        running="$(running_release || echo "unknown its reader did not run")"
        read -r verdict rel_id rel_source <<< "$running"
        if [ "$verdict" != selected ]; then
            unknown="${running#unknown }"
            unknown="${unknown:-its reader printed nothing}"
        elif ! built="$(git -C "$repo" log -1 --date=short --format=%cd "$rel_source^{commit}" -- 2>/dev/null || exit $?)"; then
            unknown="release ${rel_id:0:10} was built from ${rel_source:0:8}, a commit this checkout does not have"
        else
            unknown=""
        fi
        if [ -n "$unknown" ]; then
            notify_currency "$name" "source_unknown" "unreadable" \
                "$subject: the running release cannot be read ($unknown), so the commits on origin/$branch it lacks are not counted — inspect claudlobby --root $CLAUDLOBBY_ROOT host releases"
            log "[$name] RUNNING RELEASE UNKNOWN ($unknown) — nothing counted — $(currency_outcome_phrase)"
            continue
        fi
        currency_clear "$name" "source_unknown"
        log "[$name] RUNNING release $rel_id, built from $rel_source ($built)"
        anchor="$rel_source"
        subject+=" runs release ${rel_id:0:10} (built from ${rel_source:0:8}, $built), which"
    fi
    if ! behind="$(git -C "$repo" rev-list --count "$anchor..origin/$branch" 2>>"$LOG" || exit $?)"; then
        log "[$name] SKIP — no origin/$branch to compare against"
        continue
    fi

    # Empty tag == no release track, so the default branch is the track.
    # repo_newest_tag owns that rule and the reasoning, for both scripts.
    tag=$(repo_newest_tag "$repo")

    if [ -z "$tag" ]; then
        if [ "$behind" -gt 0 ]; then
            notify_currency "$name" "source_behind" "$behind" \
                "$subject is $behind commit(s) behind origin/$branch — apply with: $apply_hint"
            log "[$name] BEHIND origin/$branch by $behind (untagged repo) — $(currency_outcome_phrase)"
        else
            log "[$name] IN SYNC with origin/$branch"
            currency_clear "$name" "source_behind"
        fi
        continue
    fi

    tag_behind=$(git -C "$repo" rev-list --count "$anchor..$tag" 2>/dev/null || echo 0)
    if [ "${tag_behind:-0}" -gt 0 ]; then
        # Behind a cut release — the case update-siblings.sh can actually fix.
        notify_currency "$name" "source_behind" "$tag_behind" \
            "$subject is $tag_behind commit(s) behind release $tag — apply with: $apply_hint"
        log "[$name] BEHIND TAG $tag by $tag_behind — $(currency_outcome_phrase)"
    elif [ "$behind" -gt 0 ]; then
        # On the newest release, but main has moved. Deliberately NOT phrased as
        # "pull": on a versioned dependency that would mean running unreleased
        # dev code. The remedy named is cutting a release — a human decision.
        # This is the shape #1009 was filed from and could not previously say.
        notify_currency "$name" "source_release_gap" "$behind" \
            "$subject is at its newest release ($tag) but origin/$branch is $behind commit(s) ahead — unreleased fixes are not deployed; cut a release or upgrade deliberately"
        log "[$name] AT RELEASE $tag, main +$behind — release-gap $(currency_outcome_phrase)"
    else
        log "[$name] IN SYNC with origin/$branch and release $tag"
        currency_clear "$name" "source_behind"
        currency_clear "$name" "source_release_gap"
    fi
done
exit 0
