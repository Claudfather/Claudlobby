#!/bin/bash
# vault-sync.sh — the SCHEDULED door that syncs the vault and records what
# happened (#1721, wave 2 of the vault-resilience program).
#
# WHY THIS EXISTS. Claudron's sync fires only at session boundaries, under a
# 2-second hook budget, and reports to a log file inside the vault it is
# failing to sync. When a live host wedged, every sync refused for twelve days
# and printed success-shaped output; nothing scheduled ever looked. The sibling
# fixes stop the wedge happening. This is the job that NOTICES one.
#
# WHAT IT IS NOT. It never resolves a conflict, aborts a rebase or expires a
# lock: Claudron's `sync` does those or refuses them. This job runs the door
# and REPORTS -- which is the whole gap, because the refusals were always there
# and nothing read them.
#
# DISCOVERY IS A READ, NOT A DECLARATION. The vault a fleet uses is already
# composed into every bot.conf as CLAUDRON_VAULT_PATH; this
# walks what exists rather than adding a host-level field to keep in step. A
# second copy of that fact is how the two drift.
#
# DORMANT BY DEFAULT (`enroll: false`, compose-time -- #1385 composes NO unit
# at all). Armed, it COMMITS AND PUSHES on every host it runs on, which is the
# "mutates operator source" category the defaults rule reserves for opt-in.
# One knob arms it, in that host's own system.yaml:
#     host: { jobs: { vault-sync: { enroll: true } } }
# then `lib/setup-system`.
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$(cd "$LIB_DIR/.." && pwd)}"
LOG="${CLAUDLOBBY_ROOT}/state/vault-sync.log"
STATE_DIR="${CLAUDLOBBY_ROOT}/state/vault-sync"
mkdir -p "$STATE_DIR" "$(dirname "$LOG")" 2>/dev/null || true

#: An honest budget for a SCHEDULED run. The hook's 2s is the hook's problem
#: and is exactly why a scheduled door is needed: a replay that cannot finish
#: in 2s is not a fault, it is a big pull.
SYNC_TIMEOUT_S="${VAULT_SYNC_TIMEOUT_S:-300}"

#: `claudron sync --check` is Claudron's health verdict and it may not exist on
#: the installed engine yet. Its ABSENCE is recorded as `unknown` -- never as a
#: state this job invented. Unreachable is not empty, and a fabricated "clean"
#: is the exact success-shaped output this program exists to end.
STATE_UNKNOWN="unknown"

_log() { printf '%s %s\n' "$(ts_iso)" "$*" >>"$LOG" 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# Discovery: every vault any bot on this host is wired to.
# ---------------------------------------------------------------------------
# Read in a SUBSHELL per bot.conf: these files are shell, and sourcing them in
# this process would import a fleet's whole env (and its `set -e` posture) into
# a host-wide sweep. The subshell also means one malformed conf cannot take the
# job down for every other vault.
discover_vaults() {
    local bots_dir bot_dir conf path
    while IFS= read -r bots_dir; do
        for bot_dir in "$bots_dir"/*/; do
            conf="$bot_dir/bot.conf"
            [ -f "$conf" ] || continue
            path="$( . "$conf" >/dev/null 2>&1; printf '%s' "${CLAUDRON_VAULT_PATH:-}" )" || continue
            [ -n "$path" ] || continue
            [ -d "$path" ] || continue
            # realpath so two fleets pointing at one vault by different spellings
            # are ONE subject on the plane rather than two half-populated ones.
            (cd "$path" 2>/dev/null && pwd -P) || true
        done
    done < <(host_fleet_bots_dirs) | sort -u
}

# ---------------------------------------------------------------------------
# One vault.
# ---------------------------------------------------------------------------
# The envelope is parsed as JSON, never grepped: #142 is about this door's text
# being success-shaped, so reading the text would inherit the bug it reports.
_envelope_field() {   # <json> <dotted-key> <default>
    printf '%s' "$1" | python3 -S -E -c '
import json,sys
raw=sys.stdin.read()
key=sys.argv[1]; default=sys.argv[2]
try:
    d=json.loads(raw)
except Exception:
    print(default); raise SystemExit(0)
cur=d
for part in key.split("."):
    if isinstance(cur,dict) and part in cur:
        cur=cur[part]
    else:
        print(default); raise SystemExit(0)
if isinstance(cur,bool): print("true" if cur else "false")
elif cur is None: print(default)
else: print(cur)
' "$2" "$3" 2>/dev/null || printf '%s' "$3"
}

_sample() {   # <alias> <metric> <json-value>
    printf '{"event_type":"metric_sample","emitter":"vault-sync","fleet":"_host","payload":{"subject_kind":"vault","subject":"%s","metric":"%s","value":%s}}' \
        "$(json_escape "$1")" "$2" "$3"
}

sync_one_vault() {
    local vault="$1" alias samples="" state="$STATE_UNKNOWN" ok=0 detail=""
    alias="vault:$(basename "$vault")"

    # 1. The health verdict, when the engine has one. rc 2 is argparse's usage
    #    error (the CLI contract), i.e. an engine that predates the flag --
    #    recorded as unknown and NOT treated as a failing vault.
    local chk_out chk_rc=0
    chk_out="$(cd "$vault" && claudron sync --check --json 2>/dev/null)" || chk_rc=$?
    if [ "$chk_rc" -eq 0 ] && [ -n "$chk_out" ]; then
        state="$(_envelope_field "$chk_out" "data.state" "$STATE_UNKNOWN")"
    elif [ "$chk_rc" -eq 2 ]; then
        _log "$alias: engine has no 'sync --check' -- state unknown (expected until the health door ships)"
    else
        _log "$alias: 'sync --check' failed rc=$chk_rc -- state unknown"
    fi

    # 2. The sync itself. Runs whatever --check said, including unknown: the
    #    verdict is a report, never a gate.
    local out rc=0
    out="$(cd "$vault" && claudron sync --json --timeout "$SYNC_TIMEOUT_S" 2>/dev/null)" || rc=$?
    if [ -n "$out" ]; then
        [ "$(_envelope_field "$out" "ok" "false")" = "true" ] && ok=1
        detail="$(_envelope_field "$out" "data.detail" "")"
        [ -n "$detail" ] || detail="$(_envelope_field "$out" "error" "")"
    else
        detail="claudron sync produced no envelope (rc=$rc)"
    fi

    samples="$(_sample "$alias" "vault.sync_ok" "$ok")"
    samples="$samples,$(_sample "$alias" "vault.state" "\"$(json_escape "$state")\"")"
    local f
    for f in ahead behind uncommitted; do
        local v; v="$(_envelope_field "${out:-{\}}" "data.$f" "")"
        case "$v" in ''|*[!0-9]*) ;; *) samples="$samples,$(_sample "$alias" "vault.$f" "$v")" ;; esac
    done
    printf '{"events":[%s]}' "$samples" | plane_emit_events vault-sync \
        || _log "$alias: samples NOT recorded (the sync itself is unaffected)"

    _log "$alias: ok=$ok state=$state ${detail:+detail=$detail}"
    _alert_on_state_change "$vault" "$alias" "$ok" "$state" "$detail"
}

# ---------------------------------------------------------------------------
# Alerting: debounced on STATE CHANGE, deliberately not on a clock.
# ---------------------------------------------------------------------------
# A wedged vault is wedged until somebody fixes it -- it is not a burst. Paging
# every 15 minutes about a condition that is still true is how an operator
# learns to mute the channel, and then the one that matters is muted too. So
# the arm is the TRANSITION, and it clears itself when the state moves back.
_alert_on_state_change() {
    local vault="$1" alias="$2" ok="$3" state="$4" detail="$5"
    local marker="$STATE_DIR/$(printf '%s' "$alias" | tr -c 'A-Za-z0-9._-' '_').last"
    local now prev=""
    now="$([ "$ok" = "1" ] && printf 'ok' || printf 'bad:%s' "$state")"
    [ -f "$marker" ] && prev="$(cat "$marker" 2>/dev/null || true)"
    [ "$now" = "$prev" ] && return 0      # unchanged: say nothing

    # resolve_bots_dir, NOT `host_fleet_bots_dirs | head -1`: taking the first
    # entry is the #1517 defect exactly -- a lexical pick means the recipient is
    # whichever fleet directory sorts first, a reader nobody chose that a newly
    # added directory moves silently, and whose loss is undetectable (alerts
    # stopping looks identical to alerts not firing). The shared resolver plus
    # _emit_fleet_signal's declared-wins logic (CLAUDLOBBY_ALERT_MANAGER) is the
    # host-scoped answer, and it says out loud when it had to discover one.
    local bots_dir; bots_dir="$(resolve_bots_dir "${FLEET:-}" 2>/dev/null || true)"
    if [ "$ok" = "1" ]; then
        # Recovery is worth exactly one line, and only when something was wrong.
        case "$prev" in
            bad:*) emit_fleet_notice "$bots_dir" "vault_sync_recovered" \
                     "vault sync recovered for $alias (was ${prev#bad:})" || true ;;
        esac
    else
        # Emitted DIRECTLY rather than through emit_fleet_event: that door
        # anchors on FLEET_NAME / CLAUDLOBBY_FLEET and a host job has neither,
        # so it would disclose and record nothing. `vault` is a first-class
        # system subject kind, so the event says what it is about.
        printf '{"events":[{"event_type":"system","emitter":"vault-sync","fleet":"_host","payload":{"event":"vault_sync","subject_kind":"vault","subject":"%s","data":{"state":"%s","detail":"%s","ok":false}}}]}' \
            "$(json_escape "$alias")" "$(json_escape "$state")" "$(json_escape "$detail")" \
            | plane_emit_events vault-sync \
            || _log "$alias: the vault_sync event was NOT recorded"
        emit_failure_alert "$bots_dir" "vault_sync_failed" \
            "vault sync FAILED for $alias (state=$state): ${detail:-no detail} -- run 'claudron sync --check' in that vault; this job never resolves a conflict" || true
    fi
    printf '%s' "$now" > "$marker" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
main() {
    if ! command -v claudron >/dev/null 2>&1; then
        _log "claudron CLI not on PATH -- nothing to sync (the job is a no-op, not a failure)"
        exit 0
    fi
    local found=0 v
    while IFS= read -r v; do
        [ -n "$v" ] || continue
        found=$((found + 1))
        sync_one_vault "$v"
    done < <(discover_vaults)
    if [ "$found" -eq 0 ]; then
        # Said out loud: a host whose bots declare no vault is a legitimate
        # state, and a silent zero reads exactly like a broken walk.
        _log "no vault discovered from any bot.conf on this host -- nothing to do"
    fi
}

main "$@"
