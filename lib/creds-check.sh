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
# To probe additional fleet-specific MCPs, copy `check_streamable_mcp`
# below into a fleet overlay script and pass per-MCP env var names.

set -u

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:-$HOME/claudlobby}"
ENV_FILE="${CLAUDLOBBY_ENV:-$CLAUDLOBBY_ROOT/.env}"
LOG="${CLAUDLOBBY_CREDS_LOG:-$CLAUDLOBBY_ROOT/lib/creds-check.log}"
STATE="${CLAUDLOBBY_CREDS_STATE:-$CLAUDLOBBY_ROOT/lib/creds-check-state.json}"
TG_POST="$CLAUDLOBBY_ROOT/lib/tg-post.sh"

# Schedulers (launchd / systemd timer) start with a minimal PATH; .env is
# the source of truth for runtime credentials. Source it before any check.
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
fi

JQ="$(command -v jq || echo /opt/homebrew/bin/jq)"
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
    code="$("$CURL" -sS -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $token" \
        -H "Accept: application/vnd.github+json" \
        --max-time 10 \
        https://api.github.com/user 2>/dev/null || echo curl_err)"
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
    local resp_body
    resp_body="$(mktemp -t creds-check-railway)"
    # shellcheck disable=SC2064
    trap "rm -f '$resp_body'" RETURN
    local code
    code="$("$CURL" -sS -o "$resp_body" -w '%{http_code}' \
        -X POST https://backboard.railway.app/graphql/v2 \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        --max-time 10 \
        -d '{"query":"query{me{name}}"}' 2>/dev/null || echo curl_err)"
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
    local auth_header=()
    if [ -n "$token" ]; then
        auth_header=(-H "Authorization: Bearer $token")
    fi
    # Streamable-HTTP MCP requires a JSONRPC POST and Accept covering
    # both application/json and text/event-stream. ``initialize`` is the
    # canonical first-call handshake — auth-touching, side-effect-free,
    # 401 on a bad token. A bare GET returns 406 even when healthy, so
    # the GET shape would generate constant false positives.
    #
    # protocolVersion is pinned: bump in lockstep with the MCP spec the
    # server advertises. A stale pin returns 4xx — surfaces as a real
    # FAIL alert pointing here, the right failure mode.
    local body
    body='{"jsonrpc":"2.0","id":"creds-check","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"claudlobby-creds-check","version":"1.0"}}}'
    local code
    code="$("$CURL" -sS -o /dev/null -w '%{http_code}' \
        -X POST \
        -H "Accept: application/json, text/event-stream" \
        -H "Content-Type: application/json" \
        "${auth_header[@]}" \
        --max-time 10 \
        --data "$body" \
        "$url" 2>/dev/null || echo curl_err)"
    if [ "$code" = "200" ]; then
        record_and_alert "$name" "ok" "HTTP 200"
    else
        record_and_alert "$name" "fail" "HTTP $code on initialize"
    fi
}

# ---------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------

CHECKS=(check_github_pat check_railway_token check_streamable_mcp)

for fn in "${CHECKS[@]}"; do
    "$fn" || log "$fn raised (non-fatal)"
done

log "tick complete (${#CHECKS[@]} checks)"
