#!/usr/bin/env bash
# Tests for gitguardian_api.sh and triage.py. Hermetic: a stand-in curl on PATH
# records each request and replays a synthetic answer from fixtures/, and a
# throwaway git repository made here holds the bytes triage reads. No network
# and no real token: the token is random and made at run time, and so is every
# secret, so this file holds no token-shaped string.
#
# Run:  bash test.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SH="$DIR/gitguardian_api.sh"
FIX="$DIR/fixtures"
pass=0; fail=0
ok() { printf 'ok   - %s\n' "$1"; pass=$((pass + 1)); }
no() { printf 'FAIL - %s\n' "$1"; fail=$((fail + 1)); }

# Captured output is matched with a bash pattern ([[ $x == *text* ]]), never
# piped to `grep -q`, for the reason printify/test.sh gives: under pipefail a
# SIGPIPE can flip a check whose match was found.

unset GITGUARDIAN_API_KEY GITGUARDIAN_API_URL
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
rnd() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
TOKEN="t$(rnd 16)"
MARK="body-$(rnd 6)"

# The stand-in records call N's argv and stdin under $GG_LOG, then replays
# answer N of $GG_ANSWERS: N.exit to fail as curl does with no answer (28 is a
# timeout), or N.code, N.headers and N.body, framed as --dump-header - and
# --write-out frame them.
mkdir -p "$T/bin"
cat > "$T/bin/curl" <<'STUB'
#!/usr/bin/env bash
n=$(( $(cat "$GG_LOG/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$GG_LOG/calls"
printf '%s\n' "$@" > "$GG_LOG/argv.$n"
cat > "$GG_LOG/stdin.$n"
url=; prev=
for a in "$@"; do
  if [ "$prev" = --url ]; then url=$a; fi
  prev=$a
done
printf '%s\n' "$url" > "$GG_LOG/url.$n"
a="$GG_ANSWERS/$n"
if [ -f "$a.exit" ]; then
  printf '\n000'
  exit "$(cat "$a.exit")"
fi
code=$(cat "$a.code")
printf 'HTTP/2 %s\r\n' "$code"
if [ -f "$a.headers" ]; then
  while IFS= read -r h; do printf '%s\r\n' "$h"; done < "$a.headers"
fi
printf '\r\n'
cat "$a.body"
printf '\n%s' "$code"
STUB
chmod +x "$T/bin/curl"

# answer CASE N CODE BODY [HEADER]: the stand-in's answer N in CASE.
answer() {
  mkdir -p "$T/answers/$1"
  printf '%s' "$3" > "$T/answers/$1/$2.code"
  printf '%s' "$4" > "$T/answers/$1/$2.body"
  if [ $# -ge 5 ]; then printf '%s\n' "$5" > "$T/answers/$1/$2.headers"; fi
}
# fail_at CASE N EXIT: answer N in CASE fails as curl does, with status EXIT.
fail_at() {
  mkdir -p "$T/answers/$1"
  printf '%s' "$3" > "$T/answers/$1/$2.exit"
}

# run CASE ARGS...: the helper with the stand-in and the token, against the
# answers of CASE, plus any NAME=VALUE in WITH, and started by the words in VIA
# (bash -x, say) when there are any. Sets out, err and rc; the log of its
# requests is $T/log.
WITH=()
VIA=()
run() {
  local case=$1
  shift
  rm -rf "$T/log"
  mkdir -p "$T/log" "$T/answers/$case"
  out=$(env GG_LOG="$T/log" GG_ANSWERS="$T/answers/$case" PATH="$T/bin:$PATH" \
    GITGUARDIAN_API_KEY="$TOKEN" ${WITH[@]+"${WITH[@]}"} ${VIA[@]+"${VIA[@]}"} "$SH" "$@" 2>"$T/err")
  rc=$?
  err=$(cat "$T/err")
  WITH=()
  VIA=()
}
calls() { cat "$T/log/calls" 2>/dev/null || echo 0; }

# token_kept: each request got the token on stdin, as its header line, and no
# argv, stdout or stderr holds it. A run that made no request fails it, so it
# cannot pass vacuously.
token_kept() {
  local n i
  n=$(calls)
  [ "$n" -ge 1 ] || return 1
  [[ $out != *"$TOKEN"* && $err != *"$TOKEN"* ]] || return 1
  i=1
  while [ "$i" -le "$n" ]; do
    [[ $(cat "$T/log/argv.$i") != *"$TOKEN"* ]] || return 1
    [[ $(cat "$T/log/stdin.$i") == *"header = \"Authorization: Token $TOKEN\""* ]] || return 1
    i=$((i + 1))
  done
  return 0
}

# argv_is N URL: request N gave curl exactly these arguments, in this order, as
# the stand-in recorded them, one to a line: -q first (curl reads no ~/.curlrc
# only then), the fixed flags, and --url with URL last. Nothing may be added.
argv_is() {
  local got want
  got=$(cat "$T/log/argv.$1" 2>/dev/null; printf x)
  want=$(printf '%s\n' -q --config - --silent --proto =https --max-redirs 0 \
    --connect-timeout 10 --max-time 30 --dump-header - \
    --write-out '\n%{http_code}' --url "$2"; printf x)
  [ "$got" = "$want" ]
}

# --- status: readiness, in one request ---

# 1. an unset or empty key: missing, and no request
rm -rf "$T/log"; mkdir -p "$T/log"
out=$(env -u GITGUARDIAN_API_KEY GG_LOG="$T/log" GG_ANSWERS="$T/answers/none" \
  PATH="$T/bin:$PATH" "$SH" status 2>&1); rc=$?
if [ "$rc" -eq 2 ] && [[ $out == missing:* ]] \
   && [[ $out == *"config explain GITGUARDIAN_API_KEY --bot"* ]] && [ "$(calls)" -eq 0 ]; then
  ok "status: an unset key reads missing, exit 2, names config explain, sends no request"
else no "status: unset key (rc=$rc): $out"; fi

WITH=(GITGUARDIAN_API_KEY=)
run none status
if [ "$rc" -eq 2 ] && [[ $out == missing:* ]] && [ "$(calls)" -eq 0 ]; then
  ok "status: an empty key reads missing, exit 2, with no request"
else no "status: empty key (rc=$rc): $out"; fi

# 2. HTTP 200 with a list: reachable, and the token on stdin only
answer reachable 1 200 "[{\"id\": 1, \"note\": \"$MARK\"}]"
run reachable status
if [ "$rc" -eq 0 ] && [[ $out == reachable:* ]] && [[ $out == *"writes: none"* ]] \
   && [ "$(cat "$T/log/url.1")" = "https://api.gitguardian.com/v1/incidents/secrets?per_page=1" ] \
   && [[ $out != *"$MARK"* ]] && token_kept; then
  ok "status: HTTP 200 with a list reads reachable, exit 0; the token rides stdin only"
else no "status: reachable (rc=$rc): $out"; fi

# 3. the request is a plain GET: the header from --config -, https only, and
#    no flag that sets a method or sends a body
argv=$'\n'"$(cat "$T/log/argv.1" 2>/dev/null)"$'\n'
if [[ $argv == *$'\n--config\n-\n'* && $argv == *$'\n--proto\n=https\n'* ]] \
   && [[ $argv != *$'\n-X\n'* && $argv != *$'\n--request\n'* && $argv != *$'\n-d\n'* ]] \
   && [[ $argv != *$'\n--data'* && $argv != *$'\n--json\n'* && $argv != *$'\n-F\n'* ]]; then
  ok "status: curl reads its header from --config -, https only, with no method or body flag"
else no "status: curl argv:$argv"; fi

#    and the whole argv is pinned, for the checks above name only the flags a
#    write would use: -q, the fixed flags, --url and the URL, and nothing more
if argv_is 1 "https://api.gitguardian.com/v1/incidents/secrets?per_page=1"; then
  ok "status: curl gets -q first, the fixed flags and the URL, and no flag more"
else no "status: curl argv:$argv"; fi

# 4. every other answer: its word and exit, a fixed sentence, never the body
for spec in 401:rejected:3 403:insufficient_scope:4 429:no_answer:5 503:no_answer:5 418:unknown:6; do
  code=${spec%%:*}; word=${spec#*:}; want=${word#*:}; word=${word%%:*}
  answer "s$code" 1 "$code" "{\"detail\": \"$MARK\"}"
  run "s$code" status
  if [ "$rc" -eq "$want" ] && [[ $out == "$word: "* ]] && [[ $out$err != *"$MARK"* ]] && token_kept; then
    ok "status: HTTP $code reads $word, exit $want, and prints no body"
  else no "status: HTTP $code (rc=$rc): $out"; fi
done

fail_at timeout 1 28
run timeout status
if [ "$rc" -eq 5 ] && [[ $out == "no_answer: "*"timed out"* ]] && token_kept; then
  ok "status: a timeout reads no_answer, exit 5"
else no "status: timeout (rc=$rc): $out"; fi

answer notlist 1 200 "{\"detail\": \"$MARK\"}"
run notlist status
if [ "$rc" -eq 6 ] && [[ $out == "unknown: "* ]] && [[ $out != *"$MARK"* ]]; then
  ok "status: HTTP 200 without a list reads unknown, exit 6"
else no "status: HTTP 200 without a list (rc=$rc): $out"; fi

# 5. the host: the EU one when set, and nothing but GitGuardian's two
answer eu 1 200 '[]'
WITH=(GITGUARDIAN_API_URL=https://api.eu1.gitguardian.com/)
run eu status
if [ "$rc" -eq 0 ] && [ "$(cat "$T/log/url.1")" = "https://api.eu1.gitguardian.com/v1/incidents/secrets?per_page=1" ]; then
  ok "status: GITGUARDIAN_API_URL picks the EU host"
else no "status: EU host (rc=$rc): $out"; fi

for base in https://api.gitguardian.com.example http://api.gitguardian.com https://example.invalid; do
  WITH=(GITGUARDIAN_API_URL="$base")
  run none status
  if [ "$rc" -eq 7 ] && [[ $out == refused:* ]] && [ "$(calls)" -eq 0 ]; then
    ok "status: GITGUARDIAN_API_URL=$base is refused with no request"
  else no "status: GITGUARDIAN_API_URL=$base (rc=$rc): $out"; fi
done

# 6. a key no token could be is refused before it reaches a header
WITH=(GITGUARDIAN_API_KEY="$TOKEN x")
run none status
if [ "$rc" -eq 7 ] && [[ $out == refused:* ]] && [ "$(calls)" -eq 0 ] && [[ $out != *"$TOKEN"* ]]; then
  ok "status: a key holding a space is refused with no request"
else no "status: a key holding a space (rc=$rc): $out"; fi

# 7. a shell trace must not print the token: bash -x, or SHELLOPTS=xtrace in the
#    environment, traces the line that writes the header unless the helper turns
#    tracing off first. A trace on stderr shows tracing was on, and the word
#    shows the run still ran, so neither check passes by running nothing.
answer trace 1 200 '[]'
VIA=(bash -x)
run trace status
if [ "$rc" -eq 0 ] && [[ $out == reachable:* ]] && [ -n "$err" ] && token_kept; then
  ok "status: under bash -x the trace prints no token, and the run reads reachable"
else no "status: bash -x (rc=$rc, ${#err} bytes of trace): $out"; fi

WITH=(SHELLOPTS=xtrace)
VIA=(bash)
run trace status
if [ "$rc" -eq 0 ] && [[ $out == reachable:* ]] && [ -n "$err" ] && token_kept; then
  ok "status: an exported SHELLOPTS=xtrace prints no token, and the run reads reachable"
else no "status: SHELLOPTS=xtrace (rc=$rc, ${#err} bytes of trace): $out"; fi

# --- writes: refused before any request, whatever the token's scopes ---

for args in "ignore 101 --reason false_positive" "resolve 101 --secret-revoked" "reopen 101"; do
  # shellcheck disable=SC2086
  run none $args
  if [ "$rc" -eq 7 ] && [[ $out == refused:* ]] && [ "$(calls)" -eq 0 ]; then
    ok "${args%% *}: refused, exit 7, with no request"
  else no "${args%% *} (rc=$rc): $out"; fi
done

# gg_get itself, sourced: each write route, and each route or host off its
# lists, is refused before curl runs. The last check sends a listed route, so
# the refusals are not refusing everything.
chokepoint() {
  rm -rf "$T/log"; mkdir -p "$T/log"
  (
    export GG_LOG="$T/log" GG_ANSWERS="$T/answers/$2" PATH="$T/bin:$PATH" GITGUARDIAN_API_KEY="$TOKEN"
    # shellcheck source=/dev/null
    source "$SH"
    set +e
    gg_base
    gg_get "$1"
  ) >/dev/null 2>&1
}
for url in \
  https://api.gitguardian.com/v1/incidents/secrets/101/ignore \
  https://api.gitguardian.com/v1/incidents/secrets/101/resolve \
  https://api.gitguardian.com/v1/incidents/secrets/101/reopen \
  https://api.gitguardian.com/v1/incidents/secrets/101/assign \
  https://api.gitguardian.com/v1/incidents/secrets/101/share \
  https://api.gitguardian.com/v1/incidents/secrets/101/notes \
  https://api.gitguardian.com/v1/secrets/101 \
  https://api.gitguardian.com/v1/occurrences/secrets/1001/content \
  https://api.gitguardian.com/v1/scan \
  https://api.gitguardian.com/v1/api_tokens/self \
  "https://api.gitguardian.com/v1/incidents/secrets?per_page=1&value=1" \
  https://api.gitguardian.com.example/v1/incidents/secrets \
  http://api.gitguardian.com/v1/incidents/secrets; do
  chokepoint "$url" none; r=$?
  if [ "$r" -eq 7 ] && [ "$(calls)" -eq 0 ]; then
    ok "gg_get refuses ${url#https://api.gitguardian.com} before any request"
  else no "gg_get $url (status $r, $(calls) requests)"; fi
done
answer listed 1 200 '[]'
chokepoint "https://api.gitguardian.com/v1/incidents/secrets?per_page=1" listed; r=$?
if [ "$r" -eq 0 ] && [ "$(calls)" -eq 1 ]; then
  ok "gg_get sends a listed route"
else no "gg_get on a listed route (status $r, $(calls) requests)"; fi

# --- incidents: the open ones, page by page ---

p1=$(cat "$FIX/incidents-page-1.json")
p2=$(cat "$FIX/incidents-page-2.json")
p3=$(cat "$FIX/incidents-assigned.json")
answer pages 1 200 "$p1" 'link: <https://api.gitguardian.com/v1/incidents/secrets?cursor=cD0x&per_page=100>; rel="next"'
answer pages 2 200 "$p2" 'link: <https://api.gitguardian.com/v1/incidents/secrets?cursor=cD0w&per_page=100>; rel="prev"'
answer pages 3 200 "$p3"
run pages incidents
lines=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
if [ "$rc" -eq 0 ] && [ "$(calls)" -eq 3 ] \
   && [ "$(cat "$T/log/url.1")" = "https://api.gitguardian.com/v1/incidents/secrets?status=TRIGGERED&per_page=100" ] \
   && [ "$(cat "$T/log/url.2")" = "https://api.gitguardian.com/v1/incidents/secrets?cursor=cD0x&per_page=100" ] \
   && [ "$(cat "$T/log/url.3")" = "https://api.gitguardian.com/v1/incidents/secrets?status=ASSIGNED&per_page=100" ] \
   && [[ $out == *"incident=201 status=TRIGGERED validity=valid severity=critical occurrences=2 url=https://dashboard.gitguardian.com/workspace/1/incidents/201 detector=Generic High Entropy Secret"* ]] \
   && [[ $out == *"incident=203 "* && $out == *"incident=301 status=ASSIGNED"* ]] \
   && [[ $out == *"open incidents: 4" ]] && [ "$lines" -eq 5 ] && token_kept; then
  ok "incidents: TRIGGERED then ASSIGNED, following the next link to the last page"
else no "incidents: pages (rc=$rc, $(calls) requests, $lines lines): $out"; fi

if argv_is 1 "https://api.gitguardian.com/v1/incidents/secrets?status=TRIGGERED&per_page=100" \
   && argv_is 2 "https://api.gitguardian.com/v1/incidents/secrets?cursor=cD0x&per_page=100" \
   && argv_is 3 "https://api.gitguardian.com/v1/incidents/secrets?status=ASSIGNED&per_page=100"; then
  ok "incidents: each of the three requests gets -q first, the fixed flags and its URL, and no flag more"
else no "incidents: curl argv: $(tr '\n' ' ' < "$T/log/argv.1")"; fi

if [[ $out == *"detector=A name with a line break"* && $out != *fixture-secret-hash* && $out != *fixture-hmsl-hash* ]]; then
  ok "incidents: a line break in a field stays on its line, and no hash is printed"
else no "incidents: fields: $out"; fi

answer linkwrite 1 200 "$p1" 'link: <https://api.gitguardian.com/v1/incidents/secrets/201/ignore>; rel="next"'
run linkwrite incidents
if [ "$rc" -eq 7 ] && [ "$(calls)" -eq 1 ] && [[ $out == *"refused: "* ]]; then
  ok "incidents: a next link to a write route is refused before a second request"
else no "incidents: a next link to a write route (rc=$rc, $(calls) requests): $out"; fi

answer linkhost 1 200 "$p1" 'link: <https://example.invalid/v1/incidents/secrets?cursor=cD0x>; rel="next"'
run linkhost incidents
if [ "$rc" -eq 7 ] && [ "$(calls)" -eq 1 ]; then
  ok "incidents: a next link to another host is refused before a second request"
else no "incidents: a next link to another host (rc=$rc, $(calls) requests): $out"; fi

answer listfail 1 401 "{\"detail\": \"$MARK\"}"
run listfail incidents
if [ "$rc" -eq 3 ] && [[ $out == rejected:* ]] && [[ $out != *"$MARK"* ]]; then
  ok "incidents: a 401 reads rejected, exit 3"
else no "incidents: 401 (rc=$rc): $out"; fi

# --- incident ID ---

answer one 1 200 "$(cat "$FIX/incident.json")"
run one incident 101
if [ "$rc" -eq 0 ] && [ "$(cat "$T/log/url.1")" = "https://api.gitguardian.com/v1/incidents/secrets/101" ] \
   && [ "$out" = "incident=101 status=TRIGGERED validity=no_checker severity=high occurrences=1 url=https://dashboard.gitguardian.com/workspace/1/incidents/101 detector=GitHub Access Token" ] \
   && token_kept; then
  ok "incident: one line of state and link, with no repository, path or hash"
else no "incident (rc=$rc): $out"; fi

for bad in 1/ignore 1x "" -1; do
  run none incident "$bad"
  if [ "$rc" -eq 64 ] && [ "$(calls)" -eq 0 ]; then
    ok "incident: ID '$bad' is a usage error, with no request"
  else no "incident '$bad' (rc=$rc): $out"; fi
done

run none bogus
if [ "$rc" -eq 64 ] && [ "$(calls)" -eq 0 ]; then ok "an unknown verb is a usage error"
else no "an unknown verb (rc=$rc): $out"; fi

run none help
if [ "$rc" -eq 0 ] && [[ $out == *"Usage:"* && $out == *"triage ID"* ]] && [ "$(calls)" -eq 0 ]; then
  ok "help prints the usage, with no request"
else no "help (rc=$rc): $out"; fi

# --- triage: evidence words over a throwaway repository ---

if ! python3 -I -c 'import hashlib; hashlib.scrypt' >/dev/null 2>&1; then
  printf 'skip - triage: no python3 with hashlib.scrypt here\n'
else
  # hmsl VALUE: the HasMySecretLeaked hash, computed here on its own
  hmsl() {
    printf '%s' "$1" | python3 -I -c 'import hashlib, sys
print(hashlib.scrypt(sys.stdin.buffer.read(), salt=hashlib.sha256(b"GitGuardian").digest(), n=2048, r=8, p=1, dklen=32).hex())'
  }
  REPO="$T/repo"
  git init -q "$REPO"
  mkdir -p "$REPO/tests/fixtures" "$REPO/src" "$REPO/docs"
  PH="ghp_$(printf '%036d' 0 | tr 0 x)"
  KEY="k$(rnd 20)"
  OURS="o$(rnd 20)"
  printf 'token = "%s"\n' "$PH" > "$REPO/tests/fixtures/conf.txt"
  printf 'import os\nKEY = "%s"  # set at deploy\n' "$KEY" > "$REPO/src/app.py"
  printf '# Setup\n\npass: %s\n' "$OURS" > "$REPO/docs/setup.md"
  gitc() { git -C "$REPO" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false "$@"; }
  gitc add -A
  gitc commit -qm one
  SHA1=$(git -C "$REPO" rev-parse HEAD)
  printf 'import os\n' > "$REPO/src/app.py"
  gitc commit -qam two
  SHA2=$(git -C "$REPO" rev-parse HEAD)

  # incident CASE VALIDITY SECRET PATH LINE [SHA [CHANGE]]: fixtures/incident.json
  # as the answer in CASE, its one match as long as SECRET, on LINE of PATH.
  incident() {
    answer "$1" 1 200 "$(jq --arg v "$2" --arg h "$(hmsl "$3")" --arg p "$4" \
      --argjson l "$5" --argjson n "${#3}" --arg s "${6:-$SHA1}" --arg c "${7:-addition}" '
        .validity = $v | .hmsl_hash = $h
        | .occurrences[0] |= (.sha = $s | .filepath = $p | .change_type = $c
            | .matches[0] |= (.indice_end = .indice_start + $n
                | if $c == "deletion" then .pre_line_start = $l | .pre_line_end = $l
                    | .post_line_start = null | .post_line_end = null
                  else .post_line_start = $l | .post_line_end = $l end))' "$FIX/incident.json")"
  }
  seen=
  # verdict CASE WANT LABEL [ARGS...]: triage 101 in CASE prints exactly WANT
  verdict() {
    local case=$1 want=$2 label=$3
    shift 3
    run "$case" triage 101 "$@"
    seen="$seen$out$err"
    if [ "$rc" -eq 0 ] && [ "$out" = "triage: incident=101 $want" ] && token_kept \
       && [ "$(cat "$T/log/url.1")" = "https://api.gitguardian.com/v1/incidents/secrets/101?with_occurrences=100" ]; then
      ok "triage: $label"
    else no "triage: $label (rc=$rc): $out $err"; fi
  }

  incident fp no_checker "$PH" tests/fixtures/conf.txt 1
  verdict fp "class=false_positive validity=no_checker placeholder=yes ours=no path_class=fixture occurrences=1/1" \
    "a placeholder in a fixture reads false_positive" --checkout "$REPO"

  incident live valid "$KEY" src/app.py 2
  verdict live "class=live validity=valid placeholder=no ours=no path_class=source occurrences=1/1" \
    "a valid secret reads live" --checkout "$REPO"

  incident phvalid valid "$PH" tests/fixtures/conf.txt 1
  verdict phvalid "class=live validity=valid placeholder=yes ours=no path_class=fixture occurrences=1/1" \
    "a placeholder GitGuardian reads valid is still live" --checkout "$REPO"

  incident ours invalid "$OURS" docs/setup.md 3
  WITH=(GG_TEST_OURS="$OURS")
  verdict ours "class=live validity=invalid placeholder=no ours=GG_TEST_OURS path_class=docs occurrences=1/1" \
    "a value this session holds reads live and names its variable" --checkout "$REPO"

  # In the C locale Python reads a byte that is not UTF-8 as a lone surrogate,
  # which cannot be hashed. Triage skips that value, the verdict stays the one
  # above, and the variable that does hold the secret, set after it, is still read.
  incident badenv invalid "$OURS" docs/setup.md 3
  WITH=(GG_TEST_BAD=$'\xff' GG_TEST_OURS="$OURS" LC_ALL=C)
  verdict badenv "class=live validity=invalid placeholder=no ours=GG_TEST_OURS path_class=docs occurrences=1/1" \
    "a session value that is not UTF-8 is skipped, and the others are still read" --checkout "$REPO"

  incident review unknown "$KEY" src/app.py 2
  verdict review "class=needs_operator validity=unknown placeholder=no ours=no path_class=source occurrences=1/1" \
    "an unchecked secret that is no placeholder needs the operator" --checkout "$REPO"

  incident deleted invalid "$KEY" src/app.py 2 "$SHA2" deletion
  verdict deleted "class=needs_operator validity=invalid placeholder=no ours=no path_class=source occurrences=1/1" \
    "a deletion is read on the parent side" --checkout "$REPO"

  incident nolocal no_checker "$PH" tests/fixtures/conf.txt 1 "$(rnd 20)"
  verdict nolocal "class=needs_operator validity=no_checker placeholder=cannot_check ours=no path_class=fixture occurrences=1/1 note=no_local_commit" \
    "with no checkout holding the commit, placeholder reads cannot_check" --checkout "$REPO"

  incident nomatch no_checker "${PH}y" tests/fixtures/conf.txt 1
  verdict nomatch "class=needs_operator validity=no_checker placeholder=cannot_check ours=no path_class=fixture occurrences=1/1 note=no_match" \
    "bytes the hash does not confirm are never read as the secret" --checkout "$REPO"

  incident parts no_checker "$PH" tests/fixtures/conf.txt 1
  jq '.occurrences[0].matches += [.occurrences[0].matches[0]]' "$T/answers/parts/1.body" > "$T/parts.json"
  mv "$T/parts.json" "$T/answers/parts/1.body"
  verdict parts "class=needs_operator validity=no_checker placeholder=cannot_check ours=cannot_check path_class=fixture occurrences=1/1 note=several_parts" \
    "a secret in several parts reads cannot_check" --checkout "$REPO"

  # a matched line longer than triage searches
  printf 'x = "%s%s"\n' "$(printf '%02100d' 0 | tr 0 y)" "$PH" > "$REPO/docs/long.md"
  gitc add docs/long.md
  gitc commit -qm three
  SHA3=$(git -C "$REPO" rev-parse HEAD)
  incident long no_checker "$PH" docs/long.md 1 "$SHA3"
  verdict long "class=needs_operator validity=no_checker placeholder=cannot_check ours=no path_class=docs occurrences=1/1 note=long_line" \
    "a matched line too long to search reads cannot_check" --checkout "$REPO"

  # with no --checkout, triage reads each checkout under ./projects
  mkdir -p "$T/bot/projects"
  ln -s "$REPO" "$T/bot/projects/fixture"
  incident inprojects no_checker "$PH" tests/fixtures/conf.txt 1
  cd "$T/bot" || exit 1
  verdict inprojects "class=false_positive validity=no_checker placeholder=yes ours=no path_class=fixture occurrences=1/1" \
    "finds the commit under projects/ with no --checkout"
  cd "$DIR" || exit 1

  if [[ $seen != *"$PH"* && $seen != *"$KEY"* && $seen != *"$OURS"* ]] \
     && [[ $seen != *"$SHA1"* && $seen != *"$SHA2"* && $seen != *"$SHA3"* ]] \
     && [[ $seen != *conf.txt* && $seen != *app.py* && $seen != *setup.md* && $seen != *long.md* && $seen != *fixture-repo* ]] \
     && [[ $seen != *"$(hmsl "$KEY")"* && $seen != *fixture-secret-hash* ]]; then
    ok "triage: no output names a secret, path, commit, repository or hash"
  else no "triage: an output named a secret, path, commit, repository or hash"; fi
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
