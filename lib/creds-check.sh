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
# Telegram: every bot with a TELEGRAM_BOT_HANDLE in its bot.conf gets a
# per-bot getMe validation (token resolved from the tiered .env the same
# way bridge_state resolves it). Empty-resolving or wrong-bot tokens FAIL.
#
# To probe additional fleet-specific MCPs, copy `check_streamable_mcp`
# below into a fleet overlay script and pass per-MCP env var names.

set -euo pipefail

# Composed fleet timers pass the fleet name positionally on ExecStart
# (composer contract — same as fleet-pulse/log-rotate-fleet). Used by the
# per-bot Telegram check to enumerate the right fleet; other checks are
# fleet-agnostic.
FLEET_ARG="${1:-}"

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
    # deaf with no credential signal. Resolve each token exactly the way
    # bridge_state does (bot.conf names the var; value shell-sourced from
    # the tiered .env, so quoting cannot mislead), then getMe-validate.
    # One getMe per channel bot per daily tick; the token rides a curl
    # config file, never argv; only ok/error_code is ever recorded.
    local bots_dir
    bots_dir="$(resolve_bots_dir "$FLEET_ARG")"
    [ -d "$bots_dir" ] || return 0

    local d bot handle token resp okflag username errcode
    for d in "$bots_dir"/*/; do
        [ -f "$d/bot.conf" ] || continue
        bot="$(basename "$d")"
        handle="$(bot_conf_get "$d" TELEGRAM_BOT_HANDLE "")" || true
        [ -n "$handle" ] || continue  # not a channel bot

        # Subshell resolution — never touch this script's env.
        token="$(
            load_bot_conf "$d" >/dev/null 2>&1 || true
            BOT_DIR="$d"
            source_env_tiered 2>/dev/null || true
            _te="${TELEGRAM_TOKEN_ENV_NAME:-}"
            if [ -n "$_te" ]; then printf '%s' "${!_te:-}"; fi
        )" || true

        if [ -z "$token" ]; then
            # Configured for Telegram but no credential reaches it: an
            # outage, not a skip — alert.
            record_and_alert "telegram_$bot" "fail" \
                "token resolves empty across env tiers (var named in bot.conf)"
            continue
        fi

        local url_cfg curl_err_file
        url_cfg=$(safe_mktemp)
        curl_err_file=$(safe_mktemp)
        printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$token" > "$url_cfg"
        resp="$("$CURL" -sS --max-time 10 --config "$url_cfg" 2>"$curl_err_file")" \
            || resp=""
        rm -f "$url_cfg"
        if [ -z "$resp" ]; then
            record_and_alert "telegram_$bot" "fail" \
                "getMe no response ($(head -c 80 "$curl_err_file"))"
            continue
        fi

        okflag="$(printf '%s' "$resp" | "$JQ" -r '.ok // false' 2>/dev/null)" || okflag=false
        if [ "$okflag" != "true" ]; then
            errcode="$(printf '%s' "$resp" | "$JQ" -r '.error_code // "?"' 2>/dev/null)" || errcode="?"
            record_and_alert "telegram_$bot" "fail" "getMe error_code=$errcode"
            continue
        fi

        username="$(printf '%s' "$resp" | "$JQ" -r '.result.username // ""' 2>/dev/null)" || username=""
        if [ -n "$username" ] && [ "$(printf '%s' "$username" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$handle" | tr '[:upper:]' '[:lower:]')" ]; then
            # Valid token for the WRONG bot — cross-wired .env.
            record_and_alert "telegram_$bot" "fail" \
                "getMe answers @$username but bot.conf handle is @$handle (cross-wired token)"
            continue
        fi

        record_and_alert "telegram_$bot" "ok" "getMe ok (@$username)"
    done
}

# ---------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------

CHECKS=(check_github_pat check_railway_token check_streamable_mcp check_telegram_tokens)

for fn in "${CHECKS[@]}"; do
    "$fn" || log "$fn raised (non-fatal)"
done

log "tick complete (${#CHECKS[@]} checks)"
