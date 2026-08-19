#!/bin/bash
# rehearse-env-cascade.sh — #1226 stage 5 canary.
#
# Proves the .env tier cascade on a THROWAWAY bot driven through REAL
# spin-up-bot.sh / spin-down-bot.sh cycles, and — the assertion that matters —
# that the COMPOSITOR'S ANSWER MATCHES THE RUNTIME'S. A tooling/runtime split
# about where a credential comes from is the whole defect (#1214/#1226): the
# runtime cascaded four tiers while every tool above it read one, and nothing
# ever compared them.
#
#   CLAUDLOBBY_ROOT=<checkout> bash lib/rehearse-env-cascade.sh
#
# What it proves:
#   1. a var resolves from EACH of the four tiers independently;
#   2. most-specific-wins where two tiers both hold a value;
#   3. an EMPTY assignment at a more specific tier wins — the #1213 shape;
#   4. the bot SURVIVES spin-down + spin-up with resolution intact;
#   5. `claudlobby env-register` agrees with the runtime on every case above.
#
# SAFETY, and it is structural rather than careful:
#   * everything happens inside a disposable EXPORTED tree (git archive), never
#     the checkout and never a fleet directory that outlives the run;
#   * HOME is redirected to a fake home, so the host tier under test is ours
#     and the operator's real ~/.env is never read or written;
#   * --purge is guarded by _assert_disposable, which refuses any path not
#     under the export root. A --purge aimed at a production bot is the one
#     mistake this harness could make that could not be undone.
#
# BOUNDARY, stated because it bounds the claim: the bot boots with a STUBBED
# `claude` binary (the validate-bot-change.sh convention). That makes this a
# proof about env RESOLUTION and LIFECYCLE, not about Claude Code consuming the
# vars — lib/boot-strand-sampler.sh is the instrument for real boots.
set -uo pipefail

CLAUDLOBBY_ROOT="${CLAUDLOBBY_ROOT:?set CLAUDLOBBY_ROOT to the checkout under test}"
SRC_ROOT="$CLAUDLOBBY_ROOT"
FLEET=envcascade-rehearsal
BOT=canary
PREFIX=com.envcascade.rehearsal
WORK="$(mktemp -d)"
EXPORT_ROOT="$WORK/root"
FAKE_HOME="$WORK/home"
PASS=0; FAIL=0

[ "$(uname -s)" = "Linux" ] || { echo "Linux/systemd only" >&2; exit 2; }

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }

# --purge is irreversible. Refuse anything outside the disposable export root —
# a check on the PATH, not on the operator remembering which bot they meant.
_assert_disposable() {
    case "$1" in
        "$EXPORT_ROOT"/*) : ;;
        *) echo "REFUSING --purge on '$1' — not under the disposable export root" >&2
           exit 3 ;;
    esac
}

cleanup() {
    systemctl --user disable --now "$PREFIX.$BOT.service" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$PREFIX.$BOT.service" 2>/dev/null || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user reset-failed >/dev/null 2>&1 || true
    tmux -L "${SOCKET:-$PREFIX.$BOT}" kill-server >/dev/null 2>&1 || true
    # start-bot.sh and its stagger sleep survive a kill-server and were seen
    # accumulating across runs ("left-over process ... in control group").
    pkill -f "start-bot.sh $BOT_DIR" >/dev/null 2>&1 || true
    pkill -f "$WORK/bin/claude" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------- export tree
say "== exporting a disposable tree from the checkout (git archive) =="
git -C "$SRC_ROOT" archive --format=tar HEAD 2>/dev/null | (mkdir -p "$EXPORT_ROOT" && tar -x -C "$EXPORT_ROOT") \
    || { echo "git archive failed" >&2; exit 2; }
mkdir -p "$FAKE_HOME" "$EXPORT_ROOT/local/$FLEET"
# start-bot.sh takes a lock under $HOME/.claude before it does anything else, so
# a bare fake home makes the unit die at status=1 before writing .tmux-env — and
# the failure surfaces only in the journal, not in the harness output. Seed the
# config dir (and an onboarding marker, so no first-run prompt can block the
# stub) rather than discovering this again. No credential is copied: the stub
# binary authenticates against nothing.
mkdir -p "$FAKE_HOME/.claude"
printf '{}\n' > "$FAKE_HOME/.claude/settings.json"
printf '{"hasCompletedOnboarding":true,"lastOnboardingVersion":"0.0.0"}\n' > "$FAKE_HOME/.claude.json"
# The compositor under test is the EXPORTED one, never an editable install that
# might be a different commit — a green run against a stale package is a pass
# that tested nothing (naked-bot-observe.py's _assert_compositor lesson).
PYBIN="$SRC_ROOT/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"

cat > "$EXPORT_ROOT/local/$FLEET/fleet.yaml" <<YAML
fleet:
  name: $FLEET
  service_prefix: $PREFIX
  defaults:
    expertise: [software-engineering]
  bots:
    $BOT:
      expertise: [software-engineering]
      # A REAL contract var, so the shipped credential tooling has something to
      # answer about. Without it creds-reconcile has no declarations and returns
      # a clean bill about nothing.
      mcp: [github]
YAML

# --------------------------------------------------------------- the tiers
# One var per tier, so "resolves from EACH tier independently" is a real claim
# rather than one var observed four times. Plus two contest vars.
say "== writing the four tiers =="
printf 'export CANARY_HOST=from_host\nexport CANARY_CONTEST=from_host\nexport CANARY_BLANK=real_value_at_host\nexport CANARY_GUARDED=real_value_at_host\nexport GITHUB_PAT=host_tier_pat_value\n' > "$FAKE_HOME/.env"
printf 'export CANARY_ROOT=from_root\nexport CANARY_CONTEST=from_root\n'  > "$EXPORT_ROOT/.env"
# CANARY_GUARDED is a PRISTINE stub written BEFORE generate, with a real value
# upstream — the shape the scaffolder's provided_upstream guard exists to
# neutralise. CANARY_BLANK is written AFTER generate (below), so it is an empty
# assignment generate never saw and the runtime must honour. Two different
# claims that a single var would have conflated: the first run of this harness
# asserted empty-wins on a var generate had already commented out, and the
# "failure" was the guard working.
printf 'export CANARY_FLEET=from_fleet\nexport CANARY_CONTEST=from_fleet\nexport CANARY_GUARDED=\n' > "$EXPORT_ROOT/local/$FLEET/.env"
BOT_DIR="$EXPORT_ROOT/local/$FLEET/runtime/bots/$BOT"

say "== generate =="
( cd "$EXPORT_ROOT" && HOME="$FAKE_HOME" CLAUDLOBBY_ROOT="$EXPORT_ROOT" \
    "$PYBIN" -m claudlobby --fleet "$FLEET" generate ) >"$WORK/generate.log" 2>&1 \
    || { echo "generate failed:"; tail -20 "$WORK/generate.log"; exit 2; }
[ -d "$BOT_DIR" ] || { echo "bot dir not composed at $BOT_DIR" >&2; exit 2; }
printf 'export CANARY_BOT=from_bot\nexport CANARY_CONTEST=from_bot\n' >> "$BOT_DIR/.env"
# Written after generate: an operator's own empty assignment, which the
# scaffolder must not have seen and the runtime must honour.
printf 'export CANARY_BLANK=\n' >> "$EXPORT_ROOT/local/$FLEET/.env"

# The service unit is enrolled with the REAL user systemd manager, which
# resolves unit paths from ITS OWN environment — a fake HOME cannot redirect an
# already-running manager. So the unit goes where the manager looks, and the
# fake home is injected into the unit so the BOT PROCESS gets it. Without this
# the canary would read the operator's real ~/.env as its host tier, which is
# both wrong and the one file this harness must never touch.
# Stub `claude` so the real start path runs without a real session. Created
# before the unit is rewritten to point at it.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/claude" <<'STUB'
#!/bin/bash
while :; do sleep 3600; done
STUB
chmod 755 "$WORK/bin/claude"

UNIT="$(ls "$BOT_DIR"/*.service 2>/dev/null | head -1)"
[ -n "$UNIT" ] || { echo "no composed .service unit" >&2; exit 2; }
printf 'Environment=HOME=%s\n' "$FAKE_HOME" >> "$WORK/unit-extra"
sed -i "/^Environment=CLAUDLOBBY_ROOT=/a Environment=HOME=$FAKE_HOME" "$UNIT"
# The systemd service does NOT inherit the harness's PATH, so the stub `claude`
# must be named in the unit or the real binary would be launched — a real
# session, real auth, real spend, from a test.
# Inserted AFTER a known [Service] key, never appended at EOF: the composed unit
# ends with [Install], and systemd silently ignores Environment= there
# ("Unknown key 'Environment' in section [Install]"). The append looked correct,
# logged nothing the harness read, and left the REAL claude binary first on the
# unit's PATH — a test that would have opened a real session.
if grep -q '^Environment=PATH=' "$UNIT"; then
    sed -i "s|^Environment=PATH=|Environment=PATH=$WORK/bin:|" "$UNIT"
else
    sed -i "/^Environment=CLAUDLOBBY_ROOT=/a Environment=PATH=$WORK/bin:/usr/local/bin:/usr/bin:/bin" "$UNIT"
fi
# Assert it landed in [Service]. Nothing else in this harness notices if it did
# not, and the failure mode is "quietly ran the real thing".
awk '/^\[Install\]/{ins=1} /^Environment=PATH=/{if(ins) bad=1} END{exit bad?1:0}' "$UNIT" \
    || { echo "PATH injected into [Install] where systemd ignores it" >&2; exit 2; }
grep -q "^Environment=PATH=$WORK/bin:" "$UNIT" || { echo "failed to inject stub PATH" >&2; exit 2; }
grep -q "^Environment=HOME=$FAKE_HOME" "$UNIT" || { echo "failed to inject HOME into unit" >&2; exit 2; }
SOCKET="$(sed -n 's/^ *export *BOT_SERVICE=//p;s/^ *BOT_SERVICE=//p' "$BOT_DIR/bot.conf" | tr -d '\"'"'"' ' | head -1)"
[ -n "$SOCKET" ] || SOCKET="$PREFIX.$BOT"

# ------------------------------------------------------------------ spin up
# NOTE the REAL $HOME here, deliberately. install-bot-systemd.sh copies the unit
# to $HOME/.config/systemd/user and then asks the user systemd MANAGER to enable
# it — and that manager resolves unit paths from its OWN environment, fixed when
# it started. A fake HOME therefore files the unit somewhere the manager will
# never look ("Unit file ... does not exist"), which is what the first two runs
# of this harness hit. The isolation that actually matters is on the BOT
# PROCESS, and it comes from the Environment=HOME= line injected into the unit
# above. Unit goes where the manager looks; the bot still reads the fake home.
run_spinup() {
    CLAUDLOBBY_ROOT="$EXPORT_ROOT" FLEET_NAME="$FLEET" \
    PATH="$WORK/bin:$PATH" \
        bash "$EXPORT_ROOT/lib/spin-up-bot.sh" "$BOT_DIR" >>"$WORK/spinup.log" 2>&1
}

# The RUNTIME's answer: source the .tmux-env the real start path wrote, exactly
# as the tmux session's shell does, and print name=value. Never the compositor.
runtime_env() {
    HOME="$FAKE_HOME" bash -c '
        set -a; . "$1" >/dev/null 2>&1; set +a
        for v in CANARY_HOST CANARY_ROOT CANARY_FLEET CANARY_BOT CANARY_CONTEST CANARY_BLANK CANARY_GUARDED; do
            printf "%s=%s\n" "$v" "${!v-<UNSET>}"
        done' _ "$BOT_DIR/.tmux-env"
}

# Poll, never a fixed sleep: the composed unit carries an ExecStartPre boot
# stagger, so a fixed wait can assert on a bot that has not been launched yet
# and read "not up" as a failure (#1050's shape, one layer down).
await_session() {
    local i
    for i in $(seq 1 "${1:-120}"); do
        tmux -L "$SOCKET" has-session -t "$BOT" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

say "== spin-up #1 (real spin-up-bot.sh: enroll + start) =="
run_spinup
if await_session 120; then
    ok "bot session is live on its own tmux socket"
else
    bad "bot session did not come up"
    tail -8 "$WORK/spinup.log"
    say "  --- unit journal (why it did not start) ---"
    journalctl --user -u "$PREFIX.$BOT.service" --no-pager -n 12 2>/dev/null | sed 's/^/      /'

fi
[ -f "$BOT_DIR/.tmux-env" ] && ok ".tmux-env written by the real start path" \
    || bad ".tmux-env missing — nothing to assert resolution against"

# Assert the isolation held. Without this, a canary that fell back to the real
# ~/.env could still pass every check below by coincidence, and the one file
# this harness must never read would have been read.
if [ -f "$BOT_DIR/.tmux-env" ] && grep -qF "$FAKE_HOME/.env" "$BOT_DIR/.tmux-env"; then
    ok "host tier under test is the fake home (real ~/.env never sourced)"
else
    bad "ISOLATION BREACH: .tmux-env does not source $FAKE_HOME/.env"
    grep -n '^\. ' "$BOT_DIR/.tmux-env" 2>/dev/null | sed 's/^/      /'
fi

say "== 1-3: resolution, precedence, and the empty-wins case =="
RUNTIME="$(runtime_env)"
printf '%s\n' "$RUNTIME" | sed 's/^/    runtime: /'
check() {  # check <VAR> <expected>
    local got; got="$(printf '%s\n' "$RUNTIME" | sed -n "s/^$1=//p")"
    [ "$got" = "$2" ] && ok "$1 resolved '$2'" || bad "$1: expected '$2', runtime gave '$got'"
}
check CANARY_HOST    from_host
check CANARY_ROOT    from_root
check CANARY_FLEET   from_fleet
check CANARY_BOT     from_bot
check CANARY_CONTEST from_bot      # all four tiers set it; most specific wins
check CANARY_BLANK   ""            # operator's own empty at fleet beats host's value
# The other half, and it is the SCAFFOLDER's promise rather than the cascade's:
# a PRISTINE stub written before generate, with a real value upstream, is
# commented out by provided_upstream — so the upstream value survives. Asserting
# only the first case would let a regression in that guard pass unseen.
check CANARY_GUARDED real_value_at_host

say "== 5: does the COMPOSITOR agree with the runtime? =="
REG="$( cd "$EXPORT_ROOT" && HOME="$FAKE_HOME" CLAUDLOBBY_ROOT="$EXPORT_ROOT" \
        "$PYBIN" -m claudlobby --fleet "$FLEET" env-register --bot "$BOT" --json 2>"$WORK/reg.err" )"
if [ -z "$REG" ]; then
    bad "env-register produced nothing"; tail -5 "$WORK/reg.err"
else
    printf '%s' "$REG" > "$WORK/reg.json"
    TOOL="$(HOME="$FAKE_HOME" "$PYBIN" - "$WORK/reg.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for t in d["tiers"]:
    print(f'tier {t["tier"]}={t["state"]}')
PY
)"
    printf '%s\n' "$TOOL" | sed 's/^/    tooling: /'
    for t in host root fleet bot; do
        printf '%s\n' "$TOOL" | grep -qx "tier $t=present" \
            && ok "tooling sees the $t tier present, as the runtime did" \
            || bad "tooling disagrees about the $t tier"
    done
fi

# The strongest form: ask the compositor to resolve the same six vars through
# its own cascade and diff against the runtime's answer, byte for byte.
TOOLVALS="$( cd "$EXPORT_ROOT" && HOME="$FAKE_HOME" CLAUDLOBBY_ROOT="$EXPORT_ROOT" "$PYBIN" - <<PY
import os, sys
sys.path.insert(0, "$EXPORT_ROOT")
from claudlobby.paths import Paths
p = Paths(root=__import__("pathlib").Path("$EXPORT_ROOT"),
          fleet_dir=__import__("pathlib").Path("$EXPORT_ROOT/local/$FLEET"))
res = p.env_resolved("$BOT")
for v in ("CANARY_HOST","CANARY_ROOT","CANARY_FLEET","CANARY_BOT","CANARY_CONTEST","CANARY_BLANK","CANARY_GUARDED"):
    r = res.get(v)
    print(f"{v}=" + ("<UNSET>" if r is None else r.value))
PY
)"
if [ "$TOOLVALS" = "$RUNTIME" ]; then
    ok "compositor and runtime agree on all seven vars, byte for byte"
else
    bad "TOOLING/RUNTIME SPLIT:"; diff <(printf '%s\n' "$RUNTIME") <(printf '%s\n' "$TOOLVALS") | sed 's/^/      /'
fi

say "== 5b: does the SHIPPED credential tooling agree too? =="
# The whole point of the tooling-agrees-with-runtime assertion is that it covers
# TOOLING, not just the door this change happens to have added. The first
# version of this harness compared env_resolved() only — which, since that door
# had no other callers, meant the canary was the sole thing exercising it while
# creds-reconcile went untouched. A harness that reaches for the new API instead
# of the shipped one always reads clean: it is testing what it just built.
#
# GITHUB_PAT exists ONLY at the host tier here. The runtime resolves it (asserted
# below against .tmux-env); any tool that enumerates fewer than four tiers calls
# it missing.
RT_PAT="$(HOME="$FAKE_HOME" bash -c 'set -a; . "$1" >/dev/null 2>&1; set +a; printf %s "${GITHUB_PAT-<UNSET>}"' _ "$BOT_DIR/.tmux-env")"
[ "$RT_PAT" = "host_tier_pat_value" ] \
    && ok "runtime resolves GITHUB_PAT from the host tier" \
    || bad "runtime did not resolve GITHUB_PAT from host (got '$RT_PAT')"

CREDS="$( cd "$EXPORT_ROOT" && HOME="$FAKE_HOME" CLAUDLOBBY_ROOT="$EXPORT_ROOT" \
    "$PYBIN" -m claudlobby --fleet "$FLEET" creds-reconcile 2>&1 )"
printf '%s\n' "$CREDS" | grep -i 'GITHUB_PAT' | sed 's/^/    creds: /'
if printf '%s\n' "$CREDS" | grep -i 'GITHUB_PAT' | grep -qiE 'FAIL|no value|missing'; then
    bad "TOOLING/RUNTIME SPLIT: creds-reconcile calls GITHUB_PAT missing while the runtime resolves it from the host tier"
else
    ok "creds-reconcile agrees the host-tier GITHUB_PAT is present"
fi

# ------------------------------------------------- 4: survive down + up
say "== 4: spin-down (real) then spin-up, resolution must be intact =="
say "   SPINDOWN_RECEIPT_ENABLED=${SPINDOWN_RECEIPT_ENABLED:-<unset — dormant, no receipt expected>}"
CLAUDLOBBY_ROOT="$EXPORT_ROOT" FLEET_NAME="$FLEET" \
    bash "$EXPORT_ROOT/lib/spin-down-bot.sh" --reason "#1226 cascade rehearsal" \
    "$BOT_DIR" >>"$WORK/spindown.log" 2>&1
sleep 2
if tmux -L "$SOCKET" has-session -t "$BOT" 2>/dev/null; then
    bad "session still alive after spin-down"
else
    ok "spin-down reaped the session"
fi

run_spinup
await_session 120 || bad "bot did not come back up after spin-down"
AFTER="$(runtime_env)"
if [ "$AFTER" = "$RUNTIME" ]; then
    ok "resolution identical after a full spin-down / spin-up cycle"
else
    bad "resolution changed across the cycle:"; diff <(printf '%s\n' "$RUNTIME") <(printf '%s\n' "$AFTER") | sed 's/^/      /'
fi

# --------------------------------------------------------- purge, guarded
say "== teardown: --purge on the throwaway (path-guarded) =="
_assert_disposable "$BOT_DIR"
CLAUDLOBBY_ROOT="$EXPORT_ROOT" FLEET_NAME="$FLEET" \
    bash "$EXPORT_ROOT/lib/spin-down-bot.sh" --purge --reason "#1226 rehearsal teardown" \
    "$BOT_DIR" >>"$WORK/spindown.log" 2>&1
[ -d "$BOT_DIR" ] && bad "--purge left the bot dir behind" || ok "--purge removed the throwaway bot dir"

say ""
say "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
