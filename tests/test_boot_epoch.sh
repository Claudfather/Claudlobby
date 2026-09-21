#!/bin/bash
# tests/test_boot_epoch.sh -- resolve_boot_epoch / boot_epoch_from_sysctl / sysctl_bin
# (lib/lib-common.sh). Hermetic: a fake sysctl on PATH, a fake uptime that
# refuses -s (the macOS shape), no /proc reachable through the seams.
#
# Why this suite exists (2026-09-21, live on the primary host): a restarted
# manager logged "PLUGIN update-once unavailable (boot epoch unresolvable)"
# although the same call answered from an ssh shell. Two defects, both here:
#   1. a launchd unit's composed PATH has no /usr/sbin, so bare `sysctl` was
#      never found by a launcher -- rc 1, "unresolvable";
#   2. from a shell that DID find it, the greedy sed `.*sec *= *\([0-9]*\)`
#      captured the USEC field of `{ sec = N, usec = M } ...` -- a 1970 boot,
#      believed. plane-host-probe.sh had already fixed its own copy of that
#      parse; lib-common kept the broken one. Now there is one parser.
set -euo pipefail
PASS=0; FAIL=0; TOTAL=0
assert_eq() {
    TOTAL=$((TOTAL + 1)); local d="$1" e="$2" a="$3"
    if [ "$e" = "$a" ]; then echo "  PASS: $d"; PASS=$((PASS + 1)); else echo "  FAIL: $d (expected '$e', got '$a')"; FAIL=$((FAIL + 1)); fi
}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$HERE/../lib" && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/boot-epoch.XXXXXX")"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/root"
# macOS shape: uptime refuses -s; sysctl prints the live kern.boottime string
# (the primary host's kern.boottime SHAPE; the sec values are faked).
cat > "$T/bin/uptime" <<'FAKE'
#!/bin/bash
echo "uptime: illegal option -- s" >&2; exit 1
FAKE
cat > "$T/bin/sysctl" <<'FAKE'
#!/bin/bash
[ "$1" = "-n" ] && [ "$2" = "kern.boottime" ] || exit 1
# FAKE_BOOTTIME unset -> the default shape; SET (even empty) -> exactly that answer.
# (A ${VAR-default} with a } inside the default terminates at that brace, which
# is how a first version of this fake never printed the shape it claimed.)
if [ "${FAKE_BOOTTIME+set}" = set ]; then printf '%s\n' "$FAKE_BOOTTIME"; else printf '%s\n' '{ sec = 1700000000, usec = 164678 } Tue Nov 14 22:13:20 2023'; fi
FAKE
chmod +x "$T/bin/uptime" "$T/bin/sysctl"
export PATH="$T/bin:$PATH"
export CLAUDLOBBY_ROOT="$T/root"
# shellcheck source=../lib/lib-common.sh
. "$LIB_DIR/lib-common.sh"
set -e
unset CLAUDLOBBY_BOOT_EPOCH

echo "=== boot epoch: the parser ==="
assert_eq "sysctl_bin finds the fake on PATH" "sysctl" "$(sysctl_bin)"
assert_eq "kern.boottime yields SEC, not usec (the greedy-sed bug)" "1700000000" "$(boot_epoch_from_sysctl)"
assert_eq "resolve_boot_epoch on the macOS shape (uptime -s refused) answers from sysctl" "1700000000" "$(resolve_boot_epoch)"
# the live capture's SHAPE, its sec value faked (the usec and the date are not identifiers)
assert_eq "the live capture's shape parses to its sec field" "1700000001" "$(FAKE_BOOTTIME='{ sec = 1700000001, usec = 164678 } Sat Sep 19 17:57:59 2026' boot_epoch_from_sysctl)"
rc=0; FAKE_BOOTTIME='{ usec = 164678 } garbage' boot_epoch_from_sysctl >/dev/null 2>&1 || rc=$?
assert_eq "a first number below 1e9 (the usec class) is REFUSED, rc 1" "1" "$rc"
rc=0; FAKE_BOOTTIME='' boot_epoch_from_sysctl >/dev/null 2>&1 || rc=$?
assert_eq "an empty sysctl answer is rc 1" "1" "$rc"
if [ -r /proc/uptime ]; then
    echo "  SKIP: resolve_boot_epoch with nothing answering -- /proc/uptime is readable here and answers by design"
else
    rc=0; FAKE_BOOTTIME='' resolve_boot_epoch >/dev/null 2>&1 || rc=$?
    assert_eq "resolve_boot_epoch with nothing answering (no uptime -s, empty sysctl, no /proc) is rc 1" "1" "$rc"
fi

echo "=== boot epoch: the seam and the PATH ==="
mkdir -p "$T/elsewhere"; cp "$T/bin/sysctl" "$T/elsewhere/sysctl.real"
assert_eq "SYSCTL_BIN wins over PATH (the test seam, TMUX_BIN's shape)" "$T/elsewhere/sysctl.real" "$(SYSCTL_BIN="$T/elsewhere/sysctl.real" sysctl_bin)"
assert_eq "resolution through the seam still parses" "1700000000" "$(SYSCTL_BIN="$T/elsewhere/sysctl.real" boot_epoch_from_sysctl)"
# a PATH with no sysctl at all: the resolver falls back to /usr/sbin/sysctl when it exists (the launchd PATH shape)
_np="$(PATH="$T/nowhere:/usr/bin:/bin" sysctl_bin 2>/dev/null || echo none)"
case "$_np" in
    /usr/sbin/sysctl) assert_eq "off the launchd PATH, /usr/sbin/sysctl is found by absolute path" "/usr/sbin/sysctl" "$_np" ;;
    sysctl)           assert_eq "this host carries sysctl under /usr/bin (merged /usr): found on PATH" "sysctl" "$_np" ;;
    *)                assert_eq "no sysctl anywhere: rc 1, prints nothing" "none" "$_np" ;;
esac
assert_eq "CLAUDLOBBY_BOOT_EPOCH overrides everything" "1234567890" "$(CLAUDLOBBY_BOOT_EPOCH=1234567890 resolve_boot_epoch)"

echo "=== one parser, no second copy ==="
assert_eq "plane-host-probe.sh reads kern.boottime through the helper, not its own sed" "1" "$(grep -c '^ *_bsec=.*boot_epoch_from_sysctl' "$LIB_DIR/plane-host-probe.sh")"
# the READ, not the mention: a non-comment line that names both the binary and the key, in ANY lib/ file (the helper reads through "$bin")
assert_eq "no bare 'sysctl … kern.boottime' read survives anywhere under lib/ (the helper is the one reader)" "0" "$(grep -nI 'kern\.boottime' "$LIB_DIR"/* 2>/dev/null | grep -v ':[[:space:]]*#' | grep -c 'sysctl' | tr -d ' ')"

echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
