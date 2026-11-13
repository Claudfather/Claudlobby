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
# WHAT THE DATA ROOT COUNTS FROM (#2322, #2886): the source revision this host
# runs, never the root checkout's own HEAD. The code that runs is the managed
# Git installation's own checkout, which only the updater moves (host update
# apply); nothing runs from the data root's checkout and nothing pulls it, so
# its HEAD says only how old that checkout is. The root counts from the
# revision the Git runtime reports; a sibling still counts from its HEAD,
# which is what runs. A revision that cannot be read is said plainly
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

# running_revision
# One line naming the source revision this host runs, `running <revision>`,
# or `unknown <reason>` with the reason naming the read and how it failed.
# The read is the Git runtime's own report (#2886). Its committed reads carry
# data.source_revision, the revision the committed configuration was rendered
# from, and they refuse unless it is the HEAD of the managed installation's
# checkout, the code that runs. `config diff` with no fingerprint is such a
# read that needs neither a bot nor a fleet, and a host job has neither:
# `bot status` needs a bot, `context show` a fleet once a host has two,
# `host doctor` loads every fleet's context and env tiers first, and
# `host update status` prints the update record rather than what runs. It
# costs one CLI start and a read of the committed outputs, once a day. The
# composed CLI goes through its launcher, so an update in progress refuses the
# read (the launcher's own line is the reason) rather than letting it answer
# from a checkout the updater is moving.
#
# No sealed-release reader remains: since ec7cea59 no host runs this script
# from a sealed release (generated units, bot starts and operational reads all
# refuse without a managed Git installation), and host activate is gone.
running_revision() {
    # shellcheck source=cli-context.sh
    . "$LIB_DIR/cli-context.sh"
    local json err rc=0
    json=$(safe_mktemp) && err=$(safe_mktemp) \
        || { echo "unknown no scratch file for its read, claudlobby config diff"; return 0; }
    # The read refusing is an answer, not a script error: no ERR breadcrumb.
    ( trap - ERR; set +e
      _claudlobby_require_root && _claudlobby_require_cli || exit 127
      with_timeout 120 "$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" --json config diff ) >"$json" 2>"$err" || rc=$?
    cat "$err" >> "$LOG" 2>/dev/null || true
    python3 -I -S - "$json" "$err" "$rc" <<'PY' || echo "unknown its reader did not run"
import json
import re
import sys

out, err, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
READ = "claudlobby config diff"


def last_line(path):
    try:
        with open(path, errors="replace") as stream:
            lines = [line.strip() for line in stream.read(65536).splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1][:200] if lines else ""


def answer():
    if rc == 124:
        return f"unknown {READ} did not answer within 120 s"
    try:
        with open(out, encoding="utf-8") as stream:
            result = json.loads(stream.read(16 << 20) or "null")
    except (OSError, ValueError):
        result = None
    if not isinstance(result, dict) and rc == 127:
        return f"unknown {READ} did not run: {last_line(err) or 'no composed CLI'}"
    if not isinstance(result, dict):
        return f"unknown {READ} printed no result (exit {rc}): {last_line(err) or 'no output'}"
    if result.get("ok") is not True:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        return (f"unknown {READ} refused (exit {rc}): {error.get('code', 'no code')}: "
                f"{error.get('message', 'no message')}")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    revision = data.get("source_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        return f"unknown {READ} answered with no source revision"
    return f"running {revision}"


try:
    line = answer()
except Exception as exc:
    line = f"unknown its reader failed: {type(exc).__name__}"
print(" ".join(line.split()))
PY
    rm -f "$json" "$err"
}

# update_hint <repo> <ref>
# The remedy a source_behind notice names. A sibling runs from its checkout,
# so it is pulled. The data root's code is the managed installation's
# checkout, which only the updater moves: the operator applies the commit the
# count was taken against, the Git update flow in
# documentation/fleet-update-lifecycle.md ("Managed Git delivery"), and a pull
# of the data root's checkout changes nothing that runs.
update_hint() {
    local commit
    if [ "$1" != "$root_checkout" ]; then
        printf 'git -C %s pull --ff-only' "$1"
        return 0
    fi
    commit=$(git -C "$1" rev-parse --verify --quiet "$2^{commit}" 2>/dev/null || true)
    printf 'as the operator, claudlobby --root %s host update apply %s, then host update status (a pull of this checkout does not update what runs)' \
        "$CLAUDLOBBY_ROOT" "${commit:-FULL_COMMIT}"
}

for repo in "${WATCHED[@]}"; do
    name=$(basename "$repo")

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
        running="$(running_revision || echo "unknown its reader did not run")"
        read -r verdict revision _ <<< "$running"
        if [ "$verdict" != running ]; then
            unknown="${running#unknown }"
            unknown="the source revision this host runs cannot be read (${unknown:-its reader printed nothing})"
        elif ! built="$(git -C "$repo" log -1 --date=short --format=%cd "$revision^{commit}" -- 2>/dev/null || exit $?)"; then
            unknown="it runs ${revision:0:8}, a commit this checkout does not have"
        else
            unknown=""
        fi
        if [ -n "$unknown" ]; then
            notify_currency "$name" "source_unknown" "unreadable" \
                "$subject: $unknown, so the commits on origin/$branch it lacks are not counted — inspect claudlobby --root $CLAUDLOBBY_ROOT host update status"
            log "[$name] RUNNING REVISION UNKNOWN ($unknown) — nothing counted — $(currency_outcome_phrase)"
            continue
        fi
        currency_clear "$name" "source_unknown"
        log "[$name] RUNNING $revision ($built), read with claudlobby config diff"
        anchor="$revision"
        subject+=" runs ${revision:0:8} ($built), which"
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
                "$subject is $behind commit(s) behind origin/$branch — apply with: $(update_hint "$repo" "origin/$branch")"
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
            "$subject is $tag_behind commit(s) behind release $tag — apply with: $(update_hint "$repo" "$tag")"
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
