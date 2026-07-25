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
# question about it needs no tool). The bot must retrieve it via a GATED tool —
# Bash(cat ...) — so a clean return proves a non-base composed grant was honored.
SENTINEL="FRESHBOX_OK_7F3A2B"

# --- preconditions (skip, do not fail, when a heavy dep is absent) ------------
for dep in "$CLAUDE_BIN" jq python3 claudron; do
  command -v "$dep" >/dev/null 2>&1 || { printf 'SKIP: %s not found\n' "$dep"; exit 2; }
done
[ -f "$HOST_CREDS" ] || { printf 'SKIP: no host auth at ~/.claude/.credentials.json to seed\n'; exit 2; }
# A timeout(1)/gtimeout to bound the real boot: GNU ships timeout, macOS+coreutils
# gtimeout, stock macOS neither. Skip cleanly when none resolves rather than dying
# mid-boot with a bare command-not-found. _TIMEOUT_BIN is lib-common resolving both.
[ -n "$_TIMEOUT_BIN" ] || { printf 'SKIP: no timeout(1)/gtimeout to bound the boot\n'; exit 2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claudlobby-freshbox.XXXXXX")"
CONFIG_DIR="$ROOT/fbconfig"           # the fresh, empty per-bot CLAUDE_CONFIG_DIR
BOT="fbgate"
BOT_DIR="$ROOT/runtime/bots/$BOT"
TRANSCRIPT="$ROOT/boot.jsonl"

cleanup() {
  _lc_cleanup  # lib-common sets its own EXIT trap + tmpdir; ours overrides it.
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
check_no_match() {  # check_no_match "<desc>" <file> <grep-ERE> ; passes iff the pattern is ABSENT
  if grep -qiE "$3" "$2" 2>/dev/null; then check "$1" "no"; else check "$1" "yes"; fi
}
json_lines() {  # keep only JSONL objects; drop the non-JSON advisory/banner prose
  # claude -p prints e.g. "Ignoring N permissions.allow entries ... not trusted" as
  # a bare text line — feeding that to jq is a hard parse error (rc 4), which would
  # false-read as "no result". Every stream-json record is an object starting with {.
  grep '^{' "$1" 2>/dev/null
}
# These capture jq OUTPUT and test it, rather than relying on `jq -e`'s exit code:
# over a JSONL stream `-e` reflects only the last input, so a match on a non-final
# record (e.g. a mid-stream tool_use, with the result line last) mis-reads as absent.
reached_clean_result() {  # rc 0 iff the result record is non-error
  [ "$(json_lines "$1" | jq -rc 'select(.type=="result") | .is_error' 2>/dev/null | tail -1)" = "false" ]
}
result_has_sentinel() {  # rc 0 iff the final result text carries the probe token
  json_lines "$1" | jq -rc 'select(.type=="result") | .result // ""' 2>/dev/null |
    grep -qF "$SENTINEL"
}
bash_tool_used() {  # rc 0 iff the transcript contains a Bash tool_use (a gated, non-base tool)
  [ -n "$(json_lines "$1" |
    jq -rc 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use" and .name=="Bash") | .name' \
      2>/dev/null)" ]
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

# A throwaway vault the fresh bot is wired to → its session loop (L2) defaults
# on, so the composer installs the three engine hooks + the narrow verb grants.
# The SessionStart hook pulls (no remote → offline fail-open) then recalls;
# CONVENTIONS is the always-injected layer, so a sentinel there is the runtime
# proof the recall brief reached the session (not merely that a hook is registered).
VAULT="$ROOT/vault"
RECALL_SENTINEL="RECALL_OK_5C1D9E"
mkdir -p "$VAULT/_shared/knowledge"
printf '# Conventions\n\n%s — fleet knowledge marker.\n' "$RECALL_SENTINEL" \
  > "$VAULT/_shared/CONVENTIONS.md"
cat > "$VAULT/_shared/knowledge/seed.md" <<NOTE
---
type: knowledge
title: freshbox seed note
tags: [freshbox]
---
A durable finding the recall brief can surface.
NOTE

# plugins omitted → the bot inherits DEFAULT_PLUGINS (claudna + superpowers),
# exactly like a real bot; the scoped code-review expertise keeps it non-allow_all.
# claudron_vault_path wires the L2 loop (default on → hooks + narrow grants).
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
      claudron_vault_path: $VAULT
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
composed_cfg="$(bot_conf_get "$BOT_DIR" CLAUDE_CONFIG_DIR "")"
check "composer emits an active CLAUDE_CONFIG_DIR at the fresh dir" \
  "$([ "$composed_cfg" = "$CONFIG_DIR" ] && echo yes || echo no)"

printf '%s\n' "$SENTINEL" > "$BOT_DIR/probe.txt"

# --- the #645 Tier-B seam: seed auth + trust BEFORE first contact -------------
seed_auth() {  # seed_auth <config_dir>
  # The credential-file drop (#645 Fork F1) — self-refreshing, and it preserves
  # native mcp__claude_ai_* connectors a strict scope would drop.
  cp "$HOST_CREDS" "$1/.credentials.json"
  chmod 600 "$1/.credentials.json"
}
seed_auth_and_trust() {  # seed_auth_and_trust <config_dir> <project_cwd>
  local cfg="$1" cwd="$2" ver
  seed_auth "$cfg"
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

# --- L2: the composed session-loop wiring (static — proves composition) -------
# The vault-wired bot must carry the three engine hooks + the four narrow verb
# grants, and — the ratified BLOCKER — NO Bash(claudron *) wildcard and no
# promote grant, so it cannot self-promote (curation stays human-gated).
loop_cmds="$(jq -r '[.hooks.SessionStart[]?.hooks[]?.command,
                     .hooks.PreCompact[]?.hooks[]?.command,
                     .hooks.SessionEnd[]?.hooks[]?.command] | join(" ")' "$SETTINGS")"
has_all_hooks="yes"
for suffix in 'hook session-start' 'hook pre-compact' 'hook session-end'; do
  printf '%s' "$loop_cmds" | grep -qF "$suffix" || has_all_hooks="no"
done
check "composed settings carry the three claudron session-loop hooks" "$has_all_hooks"

allow_json="$(jq -c '.permissions.allow // []' "$SETTINGS")"
grants_ok="yes"
for verb in lookup recall capture status; do
  printf '%s' "$allow_json" | jq -e --arg g "Bash(claudron $verb *)" \
    'any(.[]; . == $g)' >/dev/null 2>&1 || grants_ok="no"
done
check "composed settings carry the four narrow claudron verb grants" "$grants_ok"

check_no_match "no Bash(claudron *) wildcard in composed settings" \
  "$SETTINGS" 'Bash\(claudron \*\)'
promote_denied="yes"
printf '%s' "$allow_json" | jq -e \
  'any(.[]; . == "Bash" or . == "Bash(claudron *)" or (tostring | test("claudron +promote")))' \
  >/dev/null 2>&1 && promote_denied="no"
check "vault-wired bot cannot run claudron promote (no promote grant, no wildcard)" \
  "$promote_denied"

# --- boot: claude -p on the fresh, seeded, composed config --------------------
# The prompt has the bot retrieve a token from a non-auto-loaded file via a
# tool call, proving the boot operates end-to-end (not a no-op). NOTE: headless
# `claude -p` auto-runs tools — the allow-list does not gate execution here (a
# tool fires whether or not settings.local.json is honored), so tool success is
# a functional-boot signal, NOT proof the allow was honored. The real "settings
# honored?" signal is the untrusted-workspace advisory below.
# Two asks in one boot: retrieve the probe token via a GATED Bash tool (proves a
# non-base composed grant is honored end-to-end), and echo any RECALL_ token from
# the injected SessionStart brief (proves the recall brief reached the session).
BOOT_PROMPT="Use the Bash tool to run exactly this command: cat probe.txt. Then reply with the token it prints, and on a second line, any token starting with RECALL_ that appears anywhere in your session context (write NONE if there is none). Do not use the Read tool."
boot() {  # boot <config_dir> <out_file>
  # CLAUDRON_VAULT_PATH is exported into the boot so the SessionStart hook resolves
  # the vault (start-bot.sh sources it from bot.conf in production; set directly here).
  ( cd "$BOT_DIR" && CLAUDE_CONFIG_DIR="$1" CLAUDRON_VAULT_PATH="$VAULT" \
      with_timeout "$BOOT_TIMEOUT" "$CLAUDE_BIN" -p \
      "$BOOT_PROMPT" \
      --output-format stream-json --verbose --model claude-haiku-4-5-20251001 \
      > "$2" 2>&1 ) || return $?
}

# Claude Code logs this on an UNTRUSTED workspace and drops every composed
# settings.local.json allow — the deterministic signal that the trust seed is
# load-bearing (documentation/decisions/permissions-model.md).
ADVISORY_RE='has not been trusted|Ignoring [0-9]+ permissions'

printf 'booting on the fresh CONFIG_DIR ...\n'
boot_rc=0
boot "$CONFIG_DIR" "$TRANSCRIPT" || boot_rc=$?

clean_result="no"; reached_clean_result "$TRANSCRIPT" && clean_result="yes"
check "reaches a clean (non-error) result on the fresh CONFIG_DIR (rc=$boot_rc, no hang within ${BOOT_TIMEOUT}s)" "$clean_result"

# The whole composed settings.local.json (allows AND skip-flags) is HONORED — no
# untrusted-workspace advisory. This is what proves the trust seed took: an
# untrusted workspace drops the entire file, so no-advisory == composed config
# active. (Directly answers #648/rajan: the settings.local.json skip-flags take
# effect only on a trusted workspace, which the seed provides.)
check_no_match "composed settings.local.json honored — no untrusted-workspace advisory (allows + skip-flags active)" \
  "$TRANSCRIPT" "$ADVISORY_RE"

# The bot fired a tool and returned the token from a non-auto-loaded file → it
# operates end-to-end on the fresh CONFIG_DIR (not a no-op boot).
tool_worked="no"
if bash_tool_used "$TRANSCRIPT" && result_has_sentinel "$TRANSCRIPT"; then tool_worked="yes"; fi
check "the bot ran a tool and returned the probe token (operates end-to-end on the fresh config)" "$tool_worked"

# L2: the SessionStart hook injected the recall brief. CONVENTIONS is the
# always-loaded layer, so its sentinel surfacing in the model's reply proves the
# brief reached the session — not merely that the hook entry is registered.
brief_injected="no"
if json_lines "$TRANSCRIPT" | jq -rc 'select(.type=="result") | .result // ""' 2>/dev/null | grep -qF "$RECALL_SENTINEL"; then
  brief_injected="yes"
fi
check "SessionStart injected the recall brief (CONVENTIONS sentinel observed in the reply)" "$brief_injected"

# auth wall / onboarding wizard would appear if a seed were missing.
check_no_match "no auth wall (credential drop authenticated the fresh dir)" \
  "$TRANSCRIPT" 'not logged in|please run /login|invalid api key'
check_no_match "no onboarding/trust wizard (trust seed cleared it)" \
  "$TRANSCRIPT" 'choose the text style|hasTrustDialog|do you trust this folder'

# no permission prompt / missing-perm — the skip-flag no-hang claim, empirically.
# Anchored to structured tool_result denials (json_lines drops the advisory prose).
perm_blocked="no"
if json_lines "$TRANSCRIPT" | jq -rc 'select(.type=="user") | .message.content[]? | select(.type=="tool_result" and .is_error==true) | (.content | tostring)' 2>/dev/null | grep -qiE 'permission|not allowed|requires approval|denied'; then
  perm_blocked="yes"
fi
check "zero permission prompts / missing-perm failures (skip-flags + allow-list held)" \
  "$([ "$perm_blocked" = no ] && echo yes || echo no)"

# --- transcript tool-set ⊆ composed allow-list --------------------------------
used_tools=()
while IFS= read -r t; do
  [ -n "$t" ] && used_tools+=("$t")
done < <(json_lines "$TRANSCRIPT" | jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | .name' 2>/dev/null | sort -u)
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

# --- teeth: strip the trust seed → the whole composed settings.local.json inert -
# The REAL functional divergence (not a jq artifact): with the trust key stripped,
# Claude Code refuses to honor settings.local.json and logs the untrusted-workspace
# advisory, dropping every composed allow AND skip-flag. (A no-trust boot still
# completes — base tools work and headless -p auto-runs tools — so the load-bearing
# proof is the advisory appearing ONLY without the seed, NOT "no clean result";
# that earlier claim was a jq parse failure on the advisory line.) The advisory
# text itself ("Ignoring N permissions.allow entries ... not trusted") is the
# functional signal that the composed config is provably being dropped.
MUT_DIR="$ROOT/fbconfig-notrust"
MUT_OUT="$ROOT/boot-notrust.jsonl"
mkdir -p "$MUT_DIR"
seed_auth "$MUT_DIR"  # auth seeded, trust NOT seeded (no .claude.json).
boot "$MUT_DIR" "$MUT_OUT" || true
notrust_advisory="no"; grep -qiE "$ADVISORY_RE" "$MUT_OUT" 2>/dev/null && notrust_advisory="yes"
seeded_advisory="no"; grep -qiE "$ADVISORY_RE" "$TRANSCRIPT" 2>/dev/null && seeded_advisory="yes"
# teeth pass = the composed config is honored ONLY with the seed: the no-trust boot
# logs the untrusted advisory (composed settings dropped) that the seeded one does
# not. Mutation-tested (seed both → no divergence → this goes RED).
teeth="no"
if [ "$notrust_advisory" = yes ] && [ "$seeded_advisory" = no ]; then teeth="yes"; fi
check "trust-seed teeth: no-trust boot drops the whole composed settings.local.json (untrusted advisory present only without the seed)" "$teeth"

# --- verdict ------------------------------------------------------------------
printf '\nfreshbox-boot-gate: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
printf 'freshbox self-containment: the scoped bot booted clean on an empty CONFIG_DIR with only composed config.\n'
exit 0
