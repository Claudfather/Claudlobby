#!/usr/bin/env bash
# gitguardian_api.sh: read GitGuardian's secret incidents, and send nothing else.
#
# The tool of the gitguardian integration (library/integrations/gitguardian.md),
# used by the secret-incidents protocol. Every request is a GET to one of
# GitGuardian's two SaaS API hosts, on one of the two routes in GG_ROUTES. No
# write route is on that list, so gg_get refuses a write before any request is
# made, whatever scopes the token holds.
#
# Usage:
#   gitguardian_api.sh status          may this token list incidents? (one request)
#   gitguardian_api.sh incidents       the open incidents: TRIGGERED, then ASSIGNED
#   gitguardian_api.sh incident ID     one incident and its dashboard link
#   gitguardian_api.sh triage ID [--checkout DIR]...
#                                      evidence words and a class for one incident
#   gitguardian_api.sh help
# The write verbs ignore, resolve and reopen are refused before any request.
#
# Env: GITGUARDIAN_API_KEY, a personal access token with incidents:read.
#      GITGUARDIAN_API_URL, optional: https://api.gitguardian.com (the default)
#      or, for an EU workspace, https://api.eu1.gitguardian.com.
#
# Each answer starts with one word, and the exit status matches it:
#   0 reachable   2 missing   3 rejected   4 insufficient_scope   5 no_answer
#   6 unknown     7 refused   64 usage     69 absent (curl, jq or python3)
#
# The token reaches curl only as a header line of a config read on stdin
# (--config -), written by the printf builtin, so it is on no argv and in no
# file. No answer prints the token or a response body: each outcome is a fixed
# sentence, and triage (triage.py) prints evidence words only.
set -euo pipefail

GG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GG_US=https://api.gitguardian.com
GG_EU=https://api.eu1.gitguardian.com

# The routes this helper requests, with ID for an incident number. Both are
# GETs: the incident list, and one incident.
GG_ROUTES='/v1/incidents/secrets
/v1/incidents/secrets/ID'
# The query keys those requests carry; a next-page link carries the cursor.
GG_QUERY_KEYS='per_page status cursor with_occurrences'
# How many pages of 100 incidents `incidents` reads for each status.
GG_MAX_PAGES=20

# One incident as one line: its number, state and dashboard link, and nothing
# that locates the secret. Control characters become spaces, so a field cannot
# start a line of its own.
GG_DEF='def clean: tostring | gsub("[[:cntrl:]]"; " ");'
GG_FMT='"incident=\(.id | clean) status=\(.status // "unknown" | clean) validity=\(.validity // "unknown" | clean) severity=\(.severity // "unknown" | clean) occurrences=\(.occurrences_count // 0 | clean) url=\(.gitguardian_url // "" | clean) detector=\(.detector.display_name // .detector.name // "unknown" | clean)"'

say() { printf '%s\n' "$*"; }

usage() {
  printf 'usage: %s\n' "$1" >&2
  exit 64
}

# gg_need PROGRAM...: each one is on PATH, or say which is not and exit 69.
gg_need() {
  local p
  for p in "$@"; do
    if ! command -v "$p" >/dev/null 2>&1; then
      say "absent: this helper needs $p, which is not on PATH."
      exit 69
    fi
  done
}

# gg_base: GG_BASE and GG_HOST from GITGUARDIAN_API_URL, which may name only
# one of the two SaaS API hosts. The URL is an environment value that one
# command can set, so any other value is refused before a request.
gg_base() {
  local url=${GITGUARDIAN_API_URL:-$GG_US}
  url=${url%/}
  case $url in
    "$GG_US"|"$GG_EU") ;;
    *)
      say "refused: GITGUARDIAN_API_URL may only be $GG_US or $GG_EU, so no request was sent."
      exit 7 ;;
  esac
  GG_BASE=$url
  GG_HOST=${url#https://}
}

# gg_key: GITGUARDIAN_API_KEY is set, and holds only characters a token holds,
# so it goes into the header line with nothing to escape.
gg_key() {
  if [ -z "${GITGUARDIAN_API_KEY:-}" ]; then
    say "missing: GITGUARDIAN_API_KEY is unset or empty in this session, so no request was sent."
    say "See which tier lacks or blanks it: claudlobby --json config explain GITGUARDIAN_API_KEY --bot ${BOT_NAME:-<bot>}"
    exit 2
  fi
  case $GITGUARDIAN_API_KEY in
    *[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~+/=-]*)
      say "refused: GITGUARDIAN_API_KEY holds a character no token holds, such as a space, a quote or a line break, so no request was sent."
      exit 7 ;;
  esac
}

gg_ready() {
  gg_need curl jq
  gg_base
  gg_key
}

# gg_id ID: an incident ID is a number, so it cannot carry a route of its own.
gg_id() {
  case $1 in
    ''|*[!0123456789]*) usage "an incident ID is a number" ;;
  esac
}

# gg_allowed URL: succeeds only for a GET this helper sends: the API base, a
# path in GG_ROUTES and query keys from GG_QUERY_KEYS. Every write route fails
# here, before curl runs, and so does a next-page link that names anything else.
gg_allowed() {
  local url=$1 rest path query route pair
  case $url in
    "$GG_BASE"/*) ;;
    *) return 1 ;;
  esac
  case $url in
    *[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/?\&=%._~+-]*) return 1 ;;
  esac
  rest=${url#"$GG_BASE"}
  path=${rest%%\?*}
  query=
  case $rest in
    *\?*) query=${rest#*\?} ;;
  esac
  route=$(printf '%s\n' "$path" | sed -e 's#/[0123456789][0123456789]*$#/ID#')
  case $'\n'"$GG_ROUTES"$'\n' in
    *$'\n'"$route"$'\n'*) ;;
    *) return 1 ;;
  esac
  while [ -n "$query" ]; do
    pair=${query%%\&*}
    case $query in
      *\&*) query=${query#*\&} ;;
      *) query= ;;
    esac
    case " $GG_QUERY_KEYS " in
      *" ${pair%%=*} "*) ;;
      *) return 1 ;;
    esac
  done
  return 0
}

# gg_get URL: one GET, through gg_allowed. Sets GG_CODE (the HTTP status, or
# 000 with no answer), GG_CURL (curl's exit status), GG_HEADERS and GG_BODY.
# Returns 7, with no request made, when gg_allowed refuses URL.
gg_get() {
  local url=$1 out
  gg_allowed "$url" || return 7
  GG_CURL=0
  out=$(printf 'header = "Authorization: Token %s"\n' "$GITGUARDIAN_API_KEY" |
    curl --config - --silent --proto =https --max-redirs 0 \
      --connect-timeout 10 --max-time 30 \
      --dump-header - --write-out '\n%{http_code}' --url "$url" 2>/dev/null) || GG_CURL=$?
  GG_CODE=${out##*$'\n'}
  case $GG_CODE in
    [0123456789][0123456789][0123456789]) ;;
    *) GG_CODE=000 ;;
  esac
  out=${out%$'\n'*}
  # --dump-header puts each response's headers before the body; a proxy's
  # CONNECT answer is a block of its own, so take blocks while one starts.
  GG_HEADERS=
  while :; do
    case $out in
      HTTP/*$'\r\n\r\n'*)
        GG_HEADERS=${out%%$'\r\n\r\n'*}
        out=${out#*$'\r\n\r\n'} ;;
      *) break ;;
    esac
  done
  GG_BODY=$out
  return 0
}

# gg_word: GG_WORD and GG_RC for the answer gg_get recorded.
gg_word() {
  if [ "$GG_CURL" -ne 0 ] || [ "$GG_CODE" = 000 ]; then
    GG_WORD=no_answer GG_RC=5
    return 0
  fi
  case $GG_CODE in
    200) GG_WORD=reachable GG_RC=0 ;;
    401) GG_WORD=rejected GG_RC=3 ;;
    403) GG_WORD=insufficient_scope GG_RC=4 ;;
    429|5??) GG_WORD=no_answer GG_RC=5 ;;
    *) GG_WORD=unknown GG_RC=6 ;;
  esac
}

gg_why() {
  if [ "$GG_CURL" -eq 0 ]; then
    printf 'HTTP %s' "$GG_CODE"
    return 0
  fi
  case $GG_CURL in
    6) printf 'the host name did not resolve' ;;
    7) printf 'no connection' ;;
    28) printf 'timed out' ;;
    35|60) printf 'TLS failed' ;;
    *) printf 'curl exit %s' "$GG_CURL" ;;
  esac
}

# gg_stop: the fixed sentence for an answer other than a 200, and its exit.
gg_stop() {
  case $GG_WORD in
    rejected)
      say "rejected: $GG_HOST answered HTTP 401: it did not accept GITGUARDIAN_API_KEY. A wrong token answers this way, and so does an EU workspace's token sent to the US host (such a workspace sets GITGUARDIAN_API_URL=$GG_EU). Never retry with another token." ;;
    insufficient_scope)
      say "insufficient_scope: $GG_HOST answered HTTP 403: it accepted the token, which may not read incidents. The token needs the incidents:read scope." ;;
    no_answer)
      say "no_answer: no usable answer from $GG_HOST ($(gg_why)). This says nothing about the token." ;;
    *)
      say "unknown: $GG_HOST answered HTTP $GG_CODE, which this helper does not read." ;;
  esac
  exit "$GG_RC"
}

# gg_refused WHAT: a request gg_allowed would not send, and exit 7.
gg_refused() {
  say "refused: $1, so no request was sent. This helper sends only GET requests on GitGuardian's incident list and incident routes."
  exit 7
}

# gg_fetch URL TYPE: gg_get URL, then stop unless the answer is a 200 whose body
# is JSON of TYPE (array or object). Returns 7 when gg_get refused URL.
gg_fetch() {
  local rc=0
  gg_get "$1" || rc=$?
  if [ "$rc" -ne 0 ]; then
    return "$rc"
  fi
  gg_word
  if [ "$GG_WORD" != reachable ]; then
    gg_stop
  fi
  if ! printf '%s' "$GG_BODY" | jq -e --arg t "$2" 'type == $t' >/dev/null 2>&1; then
    say "unknown: $GG_HOST answered HTTP 200 without the JSON this helper reads."
    exit 6
  fi
}

# gg_next: the URL of the Link header's rel="next", or nothing on the last page.
gg_next() {
  local next
  next=$(printf '%s\n' "$GG_HEADERS" | tr -d '\r' |
    sed -n 's/^[Ll][Ii][Nn][Kk]:.*<\([^>]*\)>; *rel="\{0,1\}next"\{0,1\}.*$/\1/p')
  next=${next%%$'\n'*}
  printf '%s' "$next"
}

door_status() {
  local rc=0
  [ $# -eq 0 ] || usage "status"
  gg_ready
  gg_fetch "$GG_BASE/v1/incidents/secrets?per_page=1" array || rc=$?
  [ "$rc" -eq 0 ] || gg_refused "the status request is off its route list"
  say "reachable: $GG_HOST answered HTTP 200 with a list of incidents, so GITGUARDIAN_API_KEY may read them."
  say "writes: none; this helper refuses every write before a request, whatever the token's scopes."
}

door_incidents() {
  local status url pages count total=0 complete=yes rc
  [ $# -eq 0 ] || usage "incidents"
  gg_ready
  for status in TRIGGERED ASSIGNED; do
    url="$GG_BASE/v1/incidents/secrets?status=$status&per_page=100"
    pages=0
    while [ -n "$url" ]; do
      if [ "$pages" -ge "$GG_MAX_PAGES" ]; then
        say "incomplete: read $GG_MAX_PAGES pages of $status incidents and stopped; more remain."
        complete=no
        break
      fi
      rc=0
      gg_fetch "$url" array || rc=$?
      [ "$rc" -eq 0 ] || gg_refused "the next-page link of the $status list names another route or host"
      printf '%s' "$GG_BODY" | jq -r "$GG_DEF .[] | $GG_FMT"
      count=$(printf '%s' "$GG_BODY" | jq 'length')
      total=$((total + count))
      pages=$((pages + 1))
      url=$(gg_next)
    done
  done
  if [ "$complete" = yes ]; then
    say "open incidents: $total"
  else
    say "open incidents: at least $total; the list above is incomplete."
  fi
}

door_incident() {
  local rc=0
  [ $# -eq 1 ] || usage "incident ID"
  gg_id "$1"
  gg_ready
  gg_fetch "$GG_BASE/v1/incidents/secrets/$1" object || rc=$?
  [ "$rc" -eq 0 ] || gg_refused "the incident request is off its route list"
  printf '%s' "$GG_BODY" | jq -r "$GG_DEF $GG_FMT"
}

door_triage() {
  local id rc=0
  local -a dirs
  [ $# -ge 1 ] || usage "triage ID [--checkout DIR]..."
  id=$1
  shift
  gg_id "$id"
  dirs=()
  while [ $# -gt 0 ]; do
    case $1 in
      --checkout)
        [ $# -ge 2 ] || usage "triage ID [--checkout DIR]..."
        dirs+=("$2")
        shift 2 ;;
      *) usage "triage ID [--checkout DIR]..." ;;
    esac
  done
  gg_need python3
  gg_ready
  gg_fetch "$GG_BASE/v1/incidents/secrets/$id?with_occurrences=100" object || rc=$?
  [ "$rc" -eq 0 ] || gg_refused "the triage request is off its route list"
  printf '%s' "$GG_BODY" | python3 -I "$GG_DIR/triage.py" ${dirs[@]+"${dirs[@]}"}
}

door_write() {
  say "refused: $1 changes an incident, and this helper sends no change, whatever the token's scopes, so no request was sent. The operator makes that change in GitGuardian's dashboard (incident ID prints its link)."
  exit 7
}

door_help() {
  awk '/^# Usage:/ { f = 1 } f { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

main() {
  local cmd=${1:-help}
  if [ $# -gt 0 ]; then
    shift
  fi
  case $cmd in
    status) door_status "$@" ;;
    incidents) door_incidents "$@" ;;
    incident) door_incident "$@" ;;
    triage) door_triage "$@" ;;
    ignore|resolve|reopen) door_write "$cmd" ;;
    help|-h|--help) door_help ;;
    *) usage "gitguardian_api.sh status | incidents | incident ID | triage ID [--checkout DIR]... | help" ;;
  esac
}

# Guarded so test.sh can source the functions above and call gg_get directly.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
  exit $?
fi
