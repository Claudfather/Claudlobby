#!/bin/bash
# Credential keepalive — pings fleet-critical credentials and alerts on
# state transitions (ok→fail or fail→ok). Run daily by a scheduled service
# (launchd on macOS, systemd timer on Linux).
#
# Why: PATs / API tokens / MCP keys silently expire and only fail on
# next use, so an outage surfaces whenever a bot happens to need that
# credential. A daily probe shrinks that window to 24h and routes the
# failure to Telegram so the human sees it before the next bot trips it.
#
# Adding a new provider: drop in a `check_<name>` function that calls
# `record_and_alert <provider> <status> <detail>`, then add it to the
# CHECKS list. Status is "ok" / "fail" / "skip"
# (skip = required env var missing — recorded but never alerted).
#
# Env vars consulted (sourced from $CLAUDLOBBY_ROOT/.env):
#   GITHUB_PERSONAL_ACCESS_TOKEN  — fleet GitHub PAT
#   RAILWAY_API_TOKEN             — account-wide Railway token
#   MCP_PROBE_URL                 — optional: streamable-HTTP MCP endpoint
#   MCP_PROBE_TOKEN               — optional: bearer for the MCP probe
#
# Telegram: every declared bot with a TELEGRAM_BOT_HANDLE gets a per-bot
# getMe validation — see check_telegram_tokens.
#
# To probe additional fleet-specific MCPs, copy `check_streamable_mcp`
# below into a fleet overlay script and pass per-MCP env var names.

set -euo pipefail

# Composed fleet timers pass the fleet name positionally on ExecStart
# (composer contract — same as fleet-pulse/log-rotate-fleet); the composed
# unit env also carries CLAUDLOBBY_FLEET, the fallback for argless runs.
# Used by the per-bot Telegram check to enumerate + namespace the right
# fleet; other checks are fleet-agnostic.
FLEET_ARG="${1:-${CLAUDLOBBY_FLEET:-}}"

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
install_error_trap ""
ENV_FILE="${CLAUDLOBBY_ENV:-$CLAUDLOBBY_ROOT/.env}"
LOG="${CLAUDLOBBY_CREDS_LOG:-$CLAUDLOBBY_ROOT/lib/creds-check.log}"
STATE="${CLAUDLOBBY_CREDS_STATE:-$CLAUDLOBBY_ROOT/state/creds-check-state.json}"
mkdir -p "$(dirname "$STATE")"
TG_POST="$CLAUDLOBBY_ROOT/lib/tg-post.sh"

# Schedulers (launchd / systemd timer) start with a minimal PATH; .env is
# the source of truth for runtime credentials. Parse it safely before any check.
parse_env_file "$ENV_FILE"

JQ="$(command -v jq || echo "${_HOMEBREW:-/usr/local}/bin/jq")"
CURL="$(command -v curl || echo /usr/bin/curl)"

mkdir -p "$(dirname "$LOG")"
[ -f "$STATE" ] || echo '{}' > "$STATE"

ts() { date -Iseconds; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG"; }
_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }  # bash 3.2 has no ${var,,}

# ---------------------------------------------------------------------
# State + alert plumbing
# ---------------------------------------------------------------------

prev_status() {
    "$JQ" -r --arg k "$1" '.[$k].status // ""' "$STATE"
}

record_and_alert() {
    local provider="$1" status="$2" detail="$3"
    local prev now
    prev="$(prev_status "$provider")"
    now="$(ts)"

    # Lock state file writes to prevent concurrent corruption
    (
    flock -x 200
    if "$JQ" --arg k "$provider" --arg s "$status" --arg d "$detail" --arg t "$now" \
        '.[$k] = {status: $s, detail: $d, ts: $t}' "$STATE" > "$STATE.tmp"; then
        mv "$STATE.tmp" "$STATE"
    else
        # Silent state corruption is the worst failure mode here — the
        # next tick would read a stale prev_status and miss the
        # transition. Surface it loudly.
        rm -f "$STATE.tmp"
        log "STATE WRITE FAILED for $provider — $status will not transition"
    fi
    ) 200>"$STATE.lock"

    log "$provider $status ($detail)"

    case "$status" in
        fail)
            # Edge alert: only fire on ok→fail (or first-ever fail).
            # Repeated fail ticks stay quiet so a long outage doesn't
            # spam the group; recovery posts again once it heals.
            if [ "$prev" != "fail" ]; then
                "$TG_POST" "creds-check: $provider FAIL — $detail" >> "$LOG" 2>&1 || \
                    log "tg-post failed for $provider FAIL"
            fi
            ;;
        ok)
            if [ "$prev" = "fail" ]; then
                "$TG_POST" "creds-check: $provider RECOVERED" >> "$LOG" 2>&1 || \
                    log "tg-post failed for $provider RECOVERED"
            fi
            ;;
        skip)
            :
            ;;
    esac
}

# ---------------------------------------------------------------------
# Provider checks
# ---------------------------------------------------------------------

check_github_pat() {
    local token="${GITHUB_PERSONAL_ACCESS_TOKEN:-${GITHUB_TOKEN:-${GITHUB_PAT:-}}}"
    if [ -z "$token" ]; then
        record_and_alert "github_pat" "skip" "no GITHUB_PERSONAL_ACCESS_TOKEN"
        return
    fi
    local code
    local curl_err_file auth_cfg
    curl_err_file=$(safe_mktemp)
    auth_cfg=$(safe_mktemp)
    printf 'header = "Authorization: Bearer %s"\n' "$token" > "$auth_cfg"
    printf 'header = "Accept: application/vnd.github+json"\n' >> "$auth_cfg"
    code="$("$CURL" -sS -o /dev/null -w '%{http_code}' \
        --config "$auth_cfg" \
        --max-time 10 \
        https://api.github.com/user 2>"$curl_err_file")" \
        || code="curl_err($(head -c 120 "$curl_err_file"))"
    if [ "$code" = "200" ]; then
        record_and_alert "github_pat" "ok" "HTTP 200"
    else
        record_and_alert "github_pat" "fail" "HTTP $code on /user"
    fi
}

check_railway_token() {
    local token="${RAILWAY_API_TOKEN:-}"
    if [ -z "$token" ]; then
        record_and_alert "railway_token" "skip" "no RAILWAY_API_TOKEN"
        return
    fi
    # Direct GraphQL probe — avoids hard dependency on `railway` CLI
    # being on PATH. The me query is the cheapest auth probe.
    local resp_body code curl_err_file auth_cfg
    resp_body=$(safe_mktemp)
    curl_err_file=$(safe_mktemp)
    auth_cfg=$(safe_mktemp)
    printf 'header = "Authorization: Bearer %s"\n' "$token" > "$auth_cfg"
    printf 'header = "Content-Type: application/json"\n' >> "$auth_cfg"
    code="$("$CURL" -sS -o "$resp_body" -w '%{http_code}' \
        -X POST https://backboard.railway.app/graphql/v2 \
        --config "$auth_cfg" \
        --max-time 10 \
        -d '{"query":"query{me{name}}"}' 2>"$curl_err_file")" \
        || code="curl_err($(head -c 120 "$curl_err_file"))"
    if [ "$code" != "200" ]; then
        record_and_alert "railway_token" "fail" "HTTP $code on /graphql/v2"
        return
    fi
    # Even a 200 can carry an auth error in the response body.
    if "$JQ" -e '.errors' "$resp_body" >/dev/null 2>&1; then
        local err
        err="$("$JQ" -r '.errors[0].message // "unknown error"' "$resp_body" | head -c 120)"
        record_and_alert "railway_token" "fail" "graphql error: $err"
        return
    fi
    record_and_alert "railway_token" "ok" "HTTP 200"
}

check_streamable_mcp() {
    # Generic streamable-HTTP MCP probe. Defaults to MCP_PROBE_URL /
    # MCP_PROBE_TOKEN env vars; pass an explicit name as $1 to record
    # results under a different key (useful for fleet-specific overlays).
    local name="${1:-mcp_probe}"
    local url="${MCP_PROBE_URL:-}"
    if [ -z "$url" ]; then
        record_and_alert "$name" "skip" "no MCP_PROBE_URL"
        return
    fi
    local token="${MCP_PROBE_TOKEN:-}"
    # Streamable-HTTP MCP requires a JSONRPC POST and Accept covering
    # both application/json and text/event-stream. ``initialize`` is the
    # canonical first-call handshake — auth-touching, side-effect-free,
    # 401 on a bad token. A bare GET returns 406 even when healthy, so
    # the GET shape would generate constant false positives.
    #
    # protocolVersion defaults to 2025-03-26; override via MCP_PROTOCOL_VERSION
    # env var when the upstream spec bumps. A stale pin returns 4xx — surfaces
    # as a real FAIL alert pointing here, the right failure mode.
    local body
    local proto_version="${MCP_PROTOCOL_VERSION:-2025-03-26}"
    body="{\"jsonrpc\":\"2.0\",\"id\":\"creds-check\",\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"${proto_version}\",\"capabilities\":{},\"clientInfo\":{\"name\":\"claudlobby-creds-check\",\"version\":\"1.0\"}}}"
    local code curl_err_file auth_cfg
    curl_err_file=$(safe_mktemp)
    auth_cfg=$(safe_mktemp)
    printf 'header = "Accept: application/json, text/event-stream"\n' > "$auth_cfg"
    printf 'header = "Content-Type: application/json"\n' >> "$auth_cfg"
    if [ -n "$token" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$token" >> "$auth_cfg"
    fi
    code="$("$CURL" -sS -o /dev/null -w '%{http_code}' \
        -X POST \
        --config "$auth_cfg" \
        --max-time 10 \
        --data "$body" \
        "$url" 2>"$curl_err_file")" \
        || code="curl_err($(head -c 120 "$curl_err_file"))"
    if [ "$code" = "200" ]; then
        record_and_alert "$name" "ok" "HTTP 200"
    else
        record_and_alert "$name" "fail" "HTTP $code on initialize"
    fi
}

check_telegram_tokens() {
    # Per-bot Telegram token validation (#502). A channel bot whose token
    # was revoked/regenerated — or resolves EMPTY through the env tiers
    # (#492, an empty later-tier stub shadowing a lower-tier value) — sits
    # deaf with no credential signal. Token resolution is the lib-common
    # SSOT shared with bridge_state (resolve_bot_telegram_token), so this
    # check validates exactly the token the bot runs with. One getMe per
    # channel bot per daily tick; the token rides a curl config file,
    # never argv; only ok/error_code is ever recorded.
    local bots_dir
    bots_dir="$(resolve_bots_dir "$FLEET_ARG")"
    [ -d "$bots_dir" ] || return 0

    # Filter through the declared-bots SSOT (same as fleet-pulse/keepalive-all)
    # so stale/cross-fleet residue dirs never fire false token alerts.
    local declared_bots
    declared_bots=$(parse_fleet_bots "$CLAUDLOBBY_ROOT/local/$FLEET_ARG/fleet.yaml")

    local d bot key handle token resp okflag username errcode
    for d in "$bots_dir"/*/; do
        [ -f "$d/bot.conf" ] || continue
        bot="$(basename "$d")"
        bot_in_fleet "$bot" "$declared_bots" || continue
        handle="$(bot_conf_get "$d" TELEGRAM_BOT_HANDLE "")" || true
        [ -n "$handle" ] || continue  # not a channel bot

        # Fleet-namespaced state key: multi-fleet hosts share one state file,
        # fleets may legitimately reuse bot names, and the alert text must say
        # which fleet's bot failed. Root mode keeps the bare key.
        key="telegram_${FLEET_ARG:+${FLEET_ARG}_}${bot}"

        token="$(resolve_bot_telegram_token "$d")" || true

        if [ -z "$token" ]; then
            # Configured for Telegram but no credential reaches it: an
            # outage, not a skip — alert.
            record_and_alert "$key" "fail" \
                "token resolves empty across env tiers (var named in bot.conf)"
            continue
        fi

        resp="$(_telegram_getme "$token")"
        if [ -z "$resp" ]; then
            record_and_alert "$key" "fail" "getMe no response (network or timeout)"
            continue
        fi

        okflag="$(printf '%s' "$resp" | "$JQ" -r '.ok // false' 2>/dev/null)" || okflag=false
        if [ "$okflag" != "true" ]; then
            errcode="$(printf '%s' "$resp" | "$JQ" -r '.error_code // "?"' 2>/dev/null)" || errcode="?"
            record_and_alert "$key" "fail" "getMe error_code=$errcode"
            continue
        fi

        username="$(printf '%s' "$resp" | "$JQ" -r '.result.username // ""' 2>/dev/null)" || username=""
        if [ -n "$username" ] && [ "$(_lc "$username")" != "$(_lc "$handle")" ]; then
            # Valid token for the WRONG bot — cross-wired .env.
            record_and_alert "$key" "fail" \
                "getMe answers @$username but bot.conf handle is @$handle (cross-wired token)"
            continue
        fi

        record_and_alert "$key" "ok" "getMe ok (@$username)"
    done
}

# ---------------------------------------------------------------------
# getMe probe (shared)
# ---------------------------------------------------------------------
# Echo the Telegram getMe response body (empty on no response). The token rides
# a curl config file, never argv. Shared by check_telegram_tokens (per-bot
# credential validation) and resolve_delivery_token (alert-channel selection)
# so the argv-safety + timeout invariant lives in one place.
_telegram_getme() {
    local _tok="$1" _cfg _resp
    _cfg="$(safe_mktemp)"
    printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$_tok" > "$_cfg"
    _resp="$("$CURL" -sS --max-time 10 --config "$_cfg" 2>/dev/null)" || _resp=""
    rm -f "$_cfg"
    printf '%s' "$_resp"
}

# ---------------------------------------------------------------------
# Alert delivery token (#542)
# ---------------------------------------------------------------------
# record_and_alert delivers via tg-post, which needs a valid bot token. A
# scheduled run carries none in its env, and tg-post's channel-dir fallback is
# unreliable — a dead or absent default-channel token drops every alert silently
# while the run still exits 0. resolve_delivery_token echoes the first declared
# channel bot's token that getMe confirms is live (empty if none), resolved via
# the token SSOT (resolve_bot_telegram_token, which reaches the fleet's real
# tokens). Validated so a bot whose own token is dead — exactly what this script
# exists to catch — cannot become the silent alert channel.
#
# NOTE: the chat-id side is now unified — this path, fleet-pulse escalation, and
# _emit_fleet_signal all resolve the target chat-id via lib-common's
# resolve_alert_target (#572). The TOKEN side is not yet: this validated resolver
# + _telegram_getme still live here, while the other two paths lean on tg-post's
# fragile channel-dir token. Promoting this pair to lib-common and pointing all
# three at it would give them one validated delivery path (follow-up: #552).
resolve_delivery_token() {
    local _dir _declared _d _tok
    _dir="$(resolve_bots_dir "$FLEET_ARG")"
    [ -d "$_dir" ] || return 0
    _declared="$(parse_fleet_bots "$CLAUDLOBBY_ROOT/local/$FLEET_ARG/fleet.yaml")"
    for _d in "$_dir"/*/; do
        [ -f "$_d/bot.conf" ] || continue
        bot_in_fleet "$(basename "$_d")" "$_declared" || continue
        [ -n "$(bot_conf_get "$_d" TELEGRAM_BOT_HANDLE "")" ] || continue
        _tok="$(resolve_bot_telegram_token "$_d")" || true
        [ -n "$_tok" ] || continue
        if [ "$(_telegram_getme "$_tok" | "$JQ" -r '.ok // false' 2>/dev/null)" = "true" ]; then
            printf '%s' "$_tok"
            return 0
        fi
    done
}

# Resolve a delivery token before the checks run so record_and_alert can deliver;
# skip when the env already carries one (bot-session callers keep their own). The
# target chat-id itself is resolved just below.
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    _dtok="$(resolve_delivery_token)" || true
    if [ -n "${_dtok:-}" ]; then
        export TELEGRAM_BOT_TOKEN="$_dtok"
        # Wording is a tested contract (test_creds_check_telegram.py): this
        # breadcrumb firing iff a token was exported is the only observable
        # that distinguishes a dropped empty-token guard.
        log "alert delivery token resolved for scheduled Telegram alerts"
    fi
fi

# Chat id: resolve via the shared fleet-alert resolver so creds-check honors the
# same override → composed-env → bot.conf-scan precedence as fleet-pulse
# escalation and _emit_fleet_signal (previously it saw only the composed env,
# ignoring the FLEET_PULSE_ESCALATION_CHAT_ID override and the scan). Fleet-scoped
# so one fleet's creds alert never routes to another fleet's channel; the resolver
# returns the composed env unchanged when that is the source, so tg-post still
# sees the same chat id in the common case.
resolve_alert_target "$(resolve_bots_dir "$FLEET_ARG")" fleet
# shellcheck disable=SC2154  # _alert_chat_id is set by resolve_alert_target (sourced lib-common)
[ -n "$_alert_chat_id" ] && export TELEGRAM_GROUP_CHAT_ID="$_alert_chat_id"

# ---------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------

CHECKS=(check_github_pat check_railway_token check_streamable_mcp check_telegram_tokens)

for fn in "${CHECKS[@]}"; do
    "$fn" || log "$fn raised (non-fatal)"
done

log "tick complete (${#CHECKS[@]} checks)"
