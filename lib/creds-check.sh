#!/bin/bash
# Credential keepalive — pings fleet-critical credentials and alerts on state
# transitions (ok→fail, fail→ok) AND on a state that persists (#1095). Run daily
# by a scheduled service (launchd on macOS, systemd timer on Linux).
#
# Transition-only alerting could not express steady state: a credential that
# failed and STAYED failed alerted once and then went quiet forever, so failure
# duration and alarm volume ran in opposite directions. A persistent fail now
# re-surfaces on a decaying ladder (1, 3, 7 days, then weekly) carrying its AGE,
# and a persistent SKIP re-surfaces from day 3 — a check that can never run is a
# permanent hole, not a skip.
#
# Why: PATs / API tokens / MCP keys silently expire and only fail on
# next use, so an outage surfaces whenever a bot happens to need that
# credential. A daily probe shrinks that window to 24h and routes the
# failure to Telegram so the human sees it before the next bot trips it.
#
# Adding a new provider: drop in a `check_<name>` function that calls
# `record_and_alert <provider> <status> <detail>`, then add it to the
# CHECKS list. Status is "ok" / "fail" / "skip"
# (skip = required env var missing — no transition alert, but re-surfaces
# once it has persisted past day 3; see next_alert_day).
#
# Env vars consulted (sourced from $CLAUDLOBBY_ROOT/.env):
#   GITHUB_PERSONAL_ACCESS_TOKEN  — fleet GitHub PAT
#   RAILWAY_API_TOKEN             — account-wide Railway token
#
# Telegram: every declared bot with a TELEGRAM_BOT_HANDLE gets a per-bot
# getMe validation — see check_telegram_tokens.
#
# There is deliberately NO MCP probe here (#1096). The one that used to live
# here gated on MCP_PROBE_URL, which was set in no fleet config, no bot.conf
# and no .env anywhere -- so it had never run, and recorded `skip` forever. It
# was also structurally near-empty: it probed ONE streamable-HTTP endpoint, and
# 14 of the 15 MCP fragments in library/mcp are stdio, which has no URL to probe
# at all. Fully configured it would have covered one server of fifteen.
#
# Note this is NOT covered by `claudlobby creds-reconcile`, which is static: it
# answers whether a credential is declared, has a value, and has an equipped
# consumer, and never contacts a provider. Nothing here validates that an MCP
# server's credential actually WORKS. That gap is real and predates this
# deletion by the whole life of the probe; removing a check that never ran does
# not widen it, but it should be named rather than left looking covered.

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
# Resolve the .env tier THIS FLEET ACTUALLY READS (#1104).
#
# This line used to be `${CLAUDLOBBY_ENV:-$CLAUDLOBBY_ROOT/.env}` — always the
# ROOT .env, while the script is invoked per-fleet (`creds-check.sh <fleet>`,
# the composed-timer contract just above). Fleet-tier credentials were therefore
# invisible to it by construction, and resolution fell through to whatever was
# ambient. Measured consequence: a VALID Railway token sat in a fleet .env the
# check never opened while the check FAILed against a stale ambient one, and an
# operator learned to disbelieve the checker.
#
# The tier rule is the Python side's `Paths.env_file`, deliberately mirrored
# rather than reinvented: the fleet .env when one exists, the root .env
# otherwise — one or the other, never merged. Matching it is what keeps this
# check and `claudlobby creds-reconcile` describing the same reality; a private
# rule here is how the two would drift into disagreeing about which credential
# is live.
ENV_FILE="${CLAUDLOBBY_ENV:-}"
if [ -z "$ENV_FILE" ] && [ -n "$FLEET_ARG" ]; then
    _cc_fleet_dir="$(resolve_fleet_dir "$FLEET_ARG" 2>/dev/null || true)"
    if [ -n "$_cc_fleet_dir" ] && [ -f "$_cc_fleet_dir/.env" ]; then
        ENV_FILE="$_cc_fleet_dir/.env"
    fi
fi
ENV_FILE="${ENV_FILE:-$CLAUDLOBBY_ROOT/.env}"
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

# Epoch when the CURRENT run of this status began, and how many alerts it has
# already produced. Both default to 0, so a state file written before #1095
# upgrades cleanly: the first tick after the upgrade starts a fresh run rather
# than crashing or back-dating one.
prev_since() {
    "$JQ" -r --arg k "$1" '.[$k].since_epoch // 0' "$STATE"
}

prev_alerts() {
    "$JQ" -r --arg k "$1" '.[$k].alerts // 0' "$STATE"
}

# Day-mark the current run must reach before its NEXT alert (#1095).
#
# Transition-only alerting cannot express steady state: a credential that fails
# and STAYS failed alerts once and is silent forever, so the longer an outage
# lasts the quieter the system gets about it. Re-surfacing on a DECAYING ladder
# keeps the noise-control intent -- a flapping credential still does not spam --
# while making "dead for a week" impossible to miss.
#
# fail: 1, 3, 7 days, then weekly. The first mark is short because a fail that
#   survives one day is already past "transient".
# skip: 3, 7 days, then weekly, and no transition alert at all. A skip on the
#   first tick is usually a provider a fleet has not configured yet, which is
#   not news. A skip that is STILL there after three days is a check that can
#   never run -- a permanent hole wearing a skip's clothing -- and that is news
#   exactly once, then weekly. This is why our own github_pat has been silent:
#   it is a skip, not a fail, so a fail-only fix would not have surfaced it.
next_alert_day() {
    local status="$1" sent="$2"
    case "$status" in
        fail)
            # `sent` counts LADDER re-surfaces only. The ok->fail transition
            # alert is a separate event and deliberately does NOT consume a
            # rung, so both entry paths share one accounting.
            #
            # Merging 0 and 1 into a single due=1 bucket was a real bug (#1169).
            # It is invisible on a fresh transition, which hardcoded sent=1 and
            # so never evaluated 0 at all. But a credential INHERITED already
            # failing with no history genuinely evaluates sent=0 on day 0, earns
            # sent=1 by firing on day 1, and then the merged bucket read sent=1
            # as still-pre-transition and fired AGAIN on day 2 — a ladder of
            # 1,2,3,7 against a documented 1,3,7. Measured on exactly the
            # credential this change was written about, which is in that state.
            case "$sent" in
                0) printf '1' ;;
                1) printf '3' ;;
                2) printf '7' ;;
                *) printf '%s' $(( 7 * (sent - 1) )) ;;
            esac ;;
        skip)
            case "$sent" in
                0) printf '3' ;;
                1) printf '7' ;;
                *) printf '%s' $(( 7 * sent )) ;;
            esac ;;
        *) printf '999999' ;;
    esac
}

# Atomically write one provider's entry to the shared state file. This is the
# critical section run under with_lock, so it must be a command (function), not
# an inline block — with_lock invokes "$@".
_write_state() {
    local provider="$1" status="$2" detail="$3" now="$4" since="${5:-0}" alerts="${6:-0}"
    if "$JQ" --arg k "$provider" --arg s "$status" --arg d "$detail" --arg t "$now" \
        --argjson since "$since" --argjson alerts "$alerts" \
        '.[$k] = {status: $s, detail: $d, ts: $t, since_epoch: $since, alerts: $alerts}' "$STATE" > "$STATE.tmp"; then
        mv "$STATE.tmp" "$STATE"
    else
        # Silent state corruption is the worst failure mode here — the next tick
        # would read a stale prev_status and miss the transition. Surface it loudly.
        rm -f "$STATE.tmp"
        log "STATE WRITE FAILED for $provider — $status will not transition"
    fi
}

record_and_alert() {
    local provider="$1" status="$2" detail="$3"
    local prev now now_epoch since sent days due alert_text=""
    prev="$(prev_status "$provider")"
    now="$(ts)"
    # An epoch is stamped at WRITE time rather than parsed back out of `ts`
    # later: `date -Iseconds` emits an offset form (…-04:00) that iso_to_epoch
    # only parses on GNU date. Reparsing would work on Linux and silently fail
    # on macOS, which is a first-class host here.
    now_epoch="$(date +%s)"

    if [ "$status" = "$prev" ]; then
        since="$(prev_since "$provider")"
        sent="$(prev_alerts "$provider")"
        [ "$since" -gt 0 ] 2>/dev/null || since="$now_epoch"
    else
        # Status changed: a new run starts here.
        since="$now_epoch"
        sent=0
    fi

    days=$(( (now_epoch - since) / 86400 ))

    case "$status" in
        fail)
            if [ "$prev" != "fail" ]; then
                # Transition alert, unchanged. It does NOT advance `sent`: the
                # ladder counts re-surfaces, and hardcoding a rung here is what
                # made the two entry paths disagree (#1169).
                alert_text="creds-check: $provider FAIL — $detail"
            else
                due="$(next_alert_day fail "$sent")"
                if [ "$days" -ge "$due" ]; then
                    # Steady-state re-surface. The AGE is the point: "fail since
                    # yesterday" and "fail since last month" are different
                    # operational facts, and only one of them is an emergency.
                    alert_text="creds-check: $provider STILL FAILING after ${days}d — $detail"
                    sent=$(( sent + 1 ))
                fi
            fi
            ;;
        ok)
            if [ "$prev" = "fail" ]; then
                alert_text="creds-check: $provider RECOVERED"
            fi
            sent=0
            ;;
        skip)
            # No transition alert (unchanged): a skip on the first tick is
            # usually a provider this fleet has not configured, which is not
            # news. A skip that PERSISTS is a check that can never run.
            if [ "$prev" = "skip" ]; then
                due="$(next_alert_day skip "$sent")"
                if [ "$days" -ge "$due" ]; then
                    alert_text="creds-check: $provider has been SKIPPED for ${days}d — $detail. A check that never runs is a permanent hole, not a skip: either configure it or remove the check."
                    sent=$(( sent + 1 ))
                fi
            fi
            ;;
    esac

    # Serialize state-file writes so concurrent ticks can't corrupt the shared
    # file. Portable mutex — flock where present, mkdir spinlock otherwise. A raw
    # flock is not portable: it's a Linux util-linux binary, absent on stock
    # macOS, where the bare call breaks the scheduled run (#709).
    with_lock "$STATE.lock" _write_state "$provider" "$status" "$detail" "$now" "$since" "$sent"

    log "$provider $status ($detail)${days:+ [${days}d in state]}"

    if [ -n "$alert_text" ]; then
        "$TG_POST" "$alert_text" >> "$LOG" 2>&1 || \
            log "tg-post failed for $provider $status"
    fi
}

# ---------------------------------------------------------------------
# Provider checks
# ---------------------------------------------------------------------

# App-auth P4 (#1274): probe a fleet that mints App installation tokens at
# use time instead of holding a static PAT. Reached only when GITHUB_APP_*
# config is present (the gate is in check_github_pat), so an App-less fleet
# never lands here even though the helper ships estate-wide in lib/.
_check_github_app() {
    # Mint helper-DIRECT via the P1 mint CLI (D1: never `git credential fill`,
    # whose pathless context would serve whatever ambient config answers). A
    # mint failure has already emitted an auth_mint_failed event from the
    # helper, so this records the fleet-level verdict without re-alerting it.
    local token
    if ! token="$("$LIB_DIR/mint-github-token.sh" 2>/dev/null)" || [ -z "$token" ]; then
        record_and_alert "github_pat" "fail" \
            "GITHUB_APP_* configured but installation-token mint failed (see auth_mint_failed events)"
        return
    fi
    # Probe an authenticated INSTALLATION endpoint. /user 403s a ghs_ token
    # ('Resource not accessible by integration'), so it cannot be reused here
    # (D13); /installation/repositories requires the token and returns 200 on
    # a healthy install. 401 (bad JWT/mint) and 403 (scope) are distinguished
    # because their remedies differ.
    local code curl_err_file auth_cfg
    curl_err_file=$(safe_mktemp)
    auth_cfg="$(auth_curl_cfg \
        "Authorization: Bearer $token" \
        "Accept: application/vnd.github+json" \
        "X-GitHub-Api-Version: 2022-11-28")"
    code="$("$CURL" -sS -o /dev/null -w '%{http_code}' \
        --config "$auth_cfg" \
        --max-time 10 \
        https://api.github.com/installation/repositories 2>"$curl_err_file")" \
        || code="curl_err($(head -c 120 "$curl_err_file"))"
    case "$code" in
        200) record_and_alert "github_pat" "ok" "App installation token OK (HTTP 200 on /installation/repositories)" ;;
        401) record_and_alert "github_pat" "fail" "App token HTTP 401 — JWT/mint rejected (wrong key, revoked, or clock skew)" ;;
        403) record_and_alert "github_pat" "fail" "App token HTTP 403 — installation lacks repository scope, or a secondary rate-limit" ;;
        *)   record_and_alert "github_pat" "fail" "App token probe: HTTP $code on /installation/repositories" ;;
    esac
}

check_github_pat() {
    local token="${GITHUB_PERSONAL_ACCESS_TOKEN:-${GITHUB_TOKEN:-${GITHUB_PAT:-}}}"
    if [ -z "$token" ]; then
        # No static token — but an App-mode fleet mints one at use time. Gate
        # on CONFIG presence (all three vars), never on the helper file: the
        # helper is a shared-lib/ install, so file-existence would make every
        # App-less fleet attempt a doomed mint. This is the App half of #1213's
        # 'a declared integration is a fail, not a skip' — the fleet declared
        # App auth, so absence of a working token is a fail, not a skip.
        # Boundary: reads only the .env tier (the vars are exported by
        # parse_env_file above), so a manual setup that put GITHUB_APP_* ONLY
        # in the helper's ~/.config/claudlobby/github-app.conf fallback reads
        # as skip here — composed fleets always wire .env, so this bites only
        # hand-configured operator/cron installs.
        if [ -n "${GITHUB_APP_ID:-}" ] && [ -n "${GITHUB_APP_INSTALLATION_ID:-}" ] \
                && [ -n "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]; then
            _check_github_app
            return
        fi
        record_and_alert "github_pat" "skip" "no GITHUB_PERSONAL_ACCESS_TOKEN and no GITHUB_APP_* config"
        return
    fi
    local code
    local curl_err_file auth_cfg
    curl_err_file=$(safe_mktemp)
    # auth_curl_cfg (lib-common, App-auth P1): the one owner of the
    # tokens-never-ride-argv invariant, escaping included.
    auth_cfg="$(auth_curl_cfg \
        "Authorization: Bearer $token" \
        "Accept: application/vnd.github+json")"
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

check_telegram_tokens() {
    # Per-bot Telegram token validation (#502). A channel bot whose token
    # was revoked/regenerated — or resolves EMPTY through the env tiers
    # (#492, an empty later-tier stub shadowing a lower-tier value) — sits
    # deaf with no credential signal. Token resolution is the lib-common
    # SSOT shared with bridge_state (resolve_bot_telegram_token), so this
    # check validates exactly the token the bot runs with. One getMe per
    # channel bot per daily tick; the token rides a curl config file,
    # never argv; only ok/error_code is ever recorded.
    local bots_dir fleet_dir
    bots_dir="$(resolve_bots_dir "$FLEET_ARG")"
    [ -d "$bots_dir" ] || return 0

    # Filter through the declared-bots SSOT (same as fleet-pulse/keepalive-all)
    # so stale/cross-fleet residue dirs never fire false token alerts.
    local declared_bots
    fleet_dir="$(resolve_fleet_dir "$FLEET_ARG")" || fleet_dir="$CLAUDLOBBY_ROOT/local/$FLEET_ARG"
    declared_bots=$(parse_fleet_bots "$fleet_dir/fleet.yaml")

    local d bot key handle token resp okflag username errcode declared_username
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
            # A declared throwaway/canary (EXPECT_NO_TOKEN=1) has no token by
            # design — an expected skip, not an outage. Real channel bots (no
            # marker) still alert below: an empty credential IS a fault.
            if bot_expects_no_token "$d"; then
                record_and_alert "$key" "skip" "EXPECT_NO_TOKEN — no Telegram token by design"
                continue
            fi
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
        # Compare against the DECLARED @username, never TELEGRAM_BOT_HANDLE. The
        # handle is channel identity — a state-dir slug that defaults to bot_id —
        # and a slug is not a username. Measured on a live 9-bot fleet: every bot
        # held a CORRECT, distinct token whose getMe answered @artemis_*_bot while
        # the slug read @<bot_id>, so comparing those declares all nine cross-wired
        # and edge-alerts once per bot, on the channel that must stay trustworthy
        # (#1095). TELEGRAM_BOT_USERNAME is emitted only when a fleet spells the
        # handle out, so empty means UNKNOWN, not mismatched: skip the comparison
        # and let getMe above still prove the token. A fleet that declares one
        # keeps the full cross-wire check, including when it equals the bot_id.
        # See #1097/#1107.
        declared_username="$(bot_conf_get "$d" TELEGRAM_BOT_USERNAME "")"
        if [ -n "$username" ] && [ -n "$declared_username" ] \
           && [ "$(_lc "$username")" != "$(_lc "$declared_username")" ]; then
            # Valid token for the WRONG bot — cross-wired .env.
            record_and_alert "$key" "fail" \
                "getMe answers @$username but bot.conf declares @$declared_username (cross-wired token)"
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
    local _dir _declared _d _tok _fdir
    _dir="$(resolve_bots_dir "$FLEET_ARG")"
    [ -d "$_dir" ] || return 0
    _fdir="$(resolve_fleet_dir "$FLEET_ARG")" || _fdir="$CLAUDLOBBY_ROOT/local/$FLEET_ARG"
    _declared="$(parse_fleet_bots "$_fdir/fleet.yaml")"
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
# Export the resolved state dir too, else record_and_alert falls to tg-post's
# dead default channel exactly when the token scan above found nothing — the
# every-credential-dead case creds-check exists to alert on (see resolve_alert_target).
# shellcheck disable=SC2154  # _alert_state_dir is set by resolve_alert_target (sourced lib-common)
[ -n "$_alert_state_dir" ] && export TELEGRAM_STATE_DIR="$_alert_state_dir"

# ---------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------

CHECKS=(check_github_pat check_railway_token check_telegram_tokens)

for fn in "${CHECKS[@]}"; do
    "$fn" || log "$fn raised (non-fatal)"
done

log "tick complete (${#CHECKS[@]} checks)"
