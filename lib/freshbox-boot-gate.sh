#!/usr/bin/env bash
# freshbox-boot-gate.sh — #644 P4, the real-boot half of the fresh-box
# self-containment gate (Fork F4(c): static superset + real-boot, both).
#
# Boots a SCOPED bot on a genuinely fresh, EMPTY CLAUDE_CONFIG_DIR carrying only
# its composed config, and proves the composed permissions actually hold at
# runtime: the bot reaches a result without hitting the auth wall or the
# onboarding/trust wizard, exercises its tools with zero permission prompts and
# zero missing-perm failures, and every tool it calls is covered by the composed
# allow-list. This is the empirical counterpart to the static `claudlobby
# freshbox` gate — a bare no-prompt run only proves absence for the tools that
# happen to fire, so the transcript is asserted against the composed allow-list.
#
# Design (each pins a review finding):
#   - SCOPED bot, never allow_all (Risk R3) — an allow_all bot passes trivially.
#   - Trust seeded BEFORE first contact: a fresh CONFIG_DIR needs
#     projects["<cwd>"].hasTrustDialogAccepted:true in .claude.json or the
#     composed settings.local.json allows silently no-op
#     (documentation/decisions/permissions-model.md; #645 P0-S2 finding).
#   - skip-flag isolation (#648 / rajan): NO user-tier settings.json skip-flags
#     are seeded, so a clean headless completion is attributable to the composed
#     project-tier settings.local.json flags — not start-bot's user-tier jq hack.
#   - Seeding is factored into seed_auth_and_trust() — the Tier-B seam #645 owns
#     and productionizes ("one harness, two assertion packs"); this file owns the
#     composed-perms assertions + boot wiring.
#
# Gated job, not a per-PR blocker (Risk R4): owner branden; cadence = composer /
# library PRs + nightly once #645 wires auth into CI. Deps: claude binary, jq,
# and real auth (~/.claude/.credentials.json). Missing any → exit 2 (the pytest
# wrapper skips on the same deps, mirroring the skipif contract).
#
# Exit: 0 all assertions passed · 1 an assertion failed · 2 precondition/dep missing.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "$LIB_DIR")"
# shellcheck source=/dev/null
. "$LIB_DIR/lib-common.sh"

# Compose with the claudlobby that ships this harness (the checkout under test),
# never a stale global install — overridable for CI.
CLAUDLOBBY_SRC="${CLAUDLOBBY_SRC:-$SRC_ROOT}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
HOST_CREDS="${HOME}/.claude/.credentials.json"
BOOT_TIMEOUT="${FRESHBOX_BOOT_TIMEOUT:-180}"
# A token in a NON-auto-loaded file (CLAUDE.md is auto-loaded into context, so a
# question about it needs no tool). Reading probe.txt forces a real Read tool
# call; echoing the token back proves the tool fired AND was permitted.
SENTINEL="FRESHBOX_OK_7F3A2B"

# --- preconditions (skip, do not fail, when a heavy dep is absent) ------------
for dep in "$CLAUDE_BIN" jq python3; do
  command -v "$dep" >/dev/null 2>&1 || { printf 'SKIP: %s not found\n' "$dep"; exit 2; }
done
[ -f "$HOST_CREDS" ] || { printf 'SKIP: no host auth at ~/.claude/.credentials.json to seed\n'; exit 2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-freshbox.XXXXXX")"
CONFIG_DIR="$ROOT/fbconfig"           # the fresh, empty per-bot CLAUDE_CONFIG_DIR
BOT="fbgate"
BOT_DIR="$ROOT/runtime/bots/$BOT"
TRANSCRIPT="$ROOT/boot.jsonl"

cleanup() {
  if [ -n "${FRESHBOX_KEEP:-}" ]; then printf 'kept artifacts: %s\n' "$ROOT"; return; fi
  rm -rf "$ROOT" 2>/dev/null || true
}
trap cleanup EXIT

pass=0
fail=0
check() {  # check "<desc>" "<yes|no>"
  if [ "$2" = "yes" ]; then
    printf 'PASS: %s\n' "$1"; pass=$((pass + 1))
  else
    printf 'FAIL: %s\n' "$1"; fail=$((fail + 1))
  fi
}

# --- compose a scoped freshbox bot into the isolated root ---------------------
# Symlink the real library/templates so a throwaway root-mode generate resolves
# expertise/skills/guardrails; the freshbox account pins CLAUDE_CONFIG_DIR active
# at the empty dir (the exact #644 F1(b) per-bot-config mechanism).
# Paths.detect requires library/ + lib/ at the root; templates/ feeds generate.
ln -s "$CLAUDLOBBY_SRC/library" "$ROOT/library"
ln -s "$CLAUDLOBBY_SRC/lib" "$ROOT/lib"
ln -s "$CLAUDLOBBY_SRC/templates" "$ROOT/templates"
mkdir -p "$CONFIG_DIR"

# plugins omitted → the bot inherits DEFAULT_PLUGINS (claudna + superpowers),
# exactly like a real bot; the scoped code-review expertise keeps it non-allow_all.
cat > "$ROOT/fleet.yaml" <<YAML
fleet:
  name: freshbox-gate
  service_prefix: fbgate
  accounts:
    default: ~/.claude
    freshbox: $CONFIG_DIR
  bots:
    $BOT:
      name: $BOT
      account: freshbox
      expertise:
        - code-review
      telegram:
        handle: fbgate_bot
YAML

printf 'composing scoped freshbox bot with %s ...\n' "$CLAUDLOBBY_SRC"
CLAUDLOBBY_ROOT="$ROOT" PYTHONPATH="$CLAUDLOBBY_SRC" python3 -m claudlobby generate >/dev/null

SETTINGS="$BOT_DIR/.claude/settings.local.json"
[ -f "$SETTINGS" ] || { printf 'ERROR: compose produced no %s\n' "$SETTINGS"; exit 1; }

# The composer must have pointed the bot at the fresh CONFIG_DIR (freshbox account).
composed_cfg="$(grep -E '^CLAUDE_CONFIG_DIR=' "$BOT_DIR/bot.conf" | head -1 | cut -d= -f2- | tr -d "'\"")"
check "composer emits an active CLAUDE_CONFIG_DIR at the fresh dir" \
  "$([ "$composed_cfg" = "$CONFIG_DIR" ] && echo yes || echo no)"

printf '%s\n' "$SENTINEL" > "$BOT_DIR/probe.txt"

# --- the #645 Tier-B seam: seed auth + trust BEFORE first contact -------------
seed_auth_and_trust() {  # seed_auth_and_trust <config_dir> <project_cwd>
  local cfg="$1" cwd="$2" ver
  # (1) auth: the credential-file drop (#645 Fork F1) — self-refreshing, and it
  #     preserves native mcp__claude_ai_* connectors a strict scope would drop.
  cp "$HOST_CREDS" "$cfg/.credentials.json"
  chmod 600 "$cfg/.credentials.json"
  # (2) trust + onboarding: without projects[cwd].hasTrustDialogAccepted the
  #     composed settings.local.json allows are silently ignored, and a fresh
  #     dir otherwise drops a headless boot into the interactive wizard.
  ver="$("$CLAUDE_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  jq -n --arg cwd "$cwd" --arg ver "${ver:-0.0.0}" '{
    hasCompletedOnboarding: true,
    lastOnboardingVersion: $ver,
    projects: { ($cwd): { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true } }
  }' > "$cfg/.claude.json"
}
seed_auth_and_trust "$CONFIG_DIR" "$BOT_DIR"

# skip-flag isolation (rajan/#648): prove NO user-tier settings.json skip-flags
# exist, so a clean completion below is attributable to the composed
# project-tier settings.local.json flags — not start-bot's user-tier jq hack.
check "composed settings.local.json carries the skip-flags" \
  "$(jq -e '.skipAutoPermissionPrompt==true and .skipDangerousModePermissionPrompt==true' "$SETTINGS" >/dev/null 2>&1 && echo yes || echo no)"
check "no user-tier settings.json skip-flags seeded (isolates the project-tier flags)" \
  "$([ ! -f "$CONFIG_DIR/settings.json" ] && echo yes || echo no)"

# --- boot: claude -p on the fresh, seeded, composed config --------------------
# The prompt exercises only allow-listed tools (Read is a base tool). A bare
# no-prompt run proves absence only for the tools that fire, so the transcript is
# checked against the allow-list below; OAuth / unfired grants are asserted
# statically by `claudlobby freshbox`.
BOOT_PROMPT="Read the file probe.txt in the current directory and reply with only the exact token it contains. Do not run any shell commands."
boot() {  # boot <config_dir> <out_file>
  ( cd "$BOT_DIR" && CLAUDE_CONFIG_DIR="$1" timeout "$BOOT_TIMEOUT" "$CLAUDE_BIN" -p \
      "$BOOT_PROMPT" \
      --output-format stream-json --verbose --model claude-haiku-4-5-20251001 \
      > "$2" 2>&1 ) || return $?
}

printf 'booting on the fresh CONFIG_DIR ...\n'
boot_rc=0
boot "$CONFIG_DIR" "$TRANSCRIPT" || boot_rc=$?

# Clean completion is the authoritative signal — a headless boot blocked on the
# auth wall, the onboarding wizard, or a permission prompt cannot reach a
# non-error result. is_error is a structured field, immune to the ambient
# CLAUDE.md text the verbose transcript echoes.
clean_result="no"
if jq -e 'select(.type=="result") | .is_error == false' "$TRANSCRIPT" >/dev/null 2>&1; then
  clean_result="yes"
fi
check "reaches a clean (non-error) result on the fresh CONFIG_DIR (rc=$boot_rc, no hang within ${BOOT_TIMEOUT}s)" "$clean_result"

# The forced Read fired AND returned content → the tool was permitted and worked
# (not merely absent). The token only exists in probe.txt, never auto-loaded.
read_worked="no"
jq -e --arg s "$SENTINEL" 'select(.type=="result") | (.result // "") | contains($s)' "$TRANSCRIPT" >/dev/null 2>&1 && read_worked="yes"
check "forced Read tool fired and returned the probe token (allow-list permitted it)" "$read_worked"

# auth wall / onboarding wizard would appear if a seed were missing (structural
# markers, not ambient prose).
hit_login="no"; grep -qiE 'not logged in|please run /login|invalid api key' "$TRANSCRIPT" 2>/dev/null && hit_login="yes"
check "no auth wall (credential drop authenticated the fresh dir)" \
  "$([ "$hit_login" = no ] && echo yes || echo no)"
hit_wizard="no"; grep -qiE 'choose the text style|hasTrustDialog|do you trust this folder' "$TRANSCRIPT" 2>/dev/null && hit_wizard="yes"
check "no onboarding/trust wizard (trust seed cleared it)" \
  "$([ "$hit_wizard" = no ] && echo yes || echo no)"

# no permission prompt / missing-perm — the skip-flag no-hang claim, empirically.
# Anchored to structured tool_result denials, not ambient CLAUDE.md text.
perm_blocked="no"
if jq -e 'select(.type=="user") | .message.content[]? | select(.type=="tool_result") | ((.is_error == true) and ((.content | tostring) | test("permission|not allowed|requires approval|denied"; "i")))' "$TRANSCRIPT" >/dev/null 2>&1; then
  perm_blocked="yes"
fi
check "zero permission prompts / missing-perm failures (skip-flags + allow-list held)" \
  "$([ "$perm_blocked" = no ] && echo yes || echo no)"

# --- transcript tool-set ⊆ composed allow-list --------------------------------
used_tools=()
while IFS= read -r t; do
  [ -n "$t" ] && used_tools+=("$t")
done < <(jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' "$TRANSCRIPT" 2>/dev/null | sort -u)
allow_json="$(jq -c '.permissions.allow // []' "$SETTINGS")"
covered="yes"; uncovered=""
for t in "${used_tools[@]:-}"; do
  [ -z "$t" ] && continue
  # covered if the allow-list has the bare tool name or any "<Tool>(...)" scope.
  if ! printf '%s' "$allow_json" | jq -e --arg t "$t" 'any(.[]; . == $t or startswith($t + "("))' >/dev/null 2>&1; then
    covered="no"; uncovered="$uncovered $t"
  fi
done
check "transcript tool-set ⊆ composed allow-list (used:${used_tools[*]:-none}${uncovered:+ uncovered:$uncovered})" "$covered"

# --- teeth: strip the trust seed → the run must NOT cleanly complete ----------
# Proves the pre-seed-before-first-contact requirement is real (not vacuous): a
# fresh CONFIG_DIR without the trust key does not reach a clean result the same
# way, so the passing run above is attributable to the seed.
MUT_DIR="$ROOT/fbconfig-notrust"
MUT_OUT="$ROOT/boot-notrust.jsonl"
mkdir -p "$MUT_DIR"
cp "$HOST_CREDS" "$MUT_DIR/.credentials.json"; chmod 600 "$MUT_DIR/.credentials.json"
# auth seeded, trust NOT seeded (no .claude.json).
( cd "$BOT_DIR" && CLAUDE_CONFIG_DIR="$MUT_DIR" timeout "$BOOT_TIMEOUT" "$CLAUDE_BIN" -p \
    "$BOOT_PROMPT" \
    --output-format stream-json --verbose --model claude-haiku-4-5-20251001 \
    > "$MUT_OUT" 2>&1 ) || true
mut_result="no"
if grep -q '"type":"result"' "$MUT_OUT" 2>/dev/null &&
   jq -e 'select(.type=="result") | .is_error == false' "$MUT_OUT" >/dev/null 2>&1; then
  mut_result="yes"
fi
mut_wizard="no"; grep -qiE 'do you trust|welcome to claude code|hasTrustDialog' "$MUT_OUT" 2>/dev/null && mut_wizard="yes"
# teeth pass = the stripped run diverged: it either hit the wizard or did NOT
# reach a clean result. If a no-trust boot completes identically to the seeded
# one, the seed is not what makes the gate pass and the assertions above prove
# nothing — that is the regression this teeth-check exists to catch.
teeth="no"
if [ "$mut_wizard" = yes ] || [ "$mut_result" = no ]; then teeth="yes"; fi
check "trust-seed teeth: a no-trust boot diverges (wizard or no clean result)" "$teeth"

# --- verdict ------------------------------------------------------------------
printf '\nfreshbox-boot-gate: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
printf 'freshbox self-containment: the scoped bot booted clean on an empty CONFIG_DIR with only composed config.\n'
exit 0
