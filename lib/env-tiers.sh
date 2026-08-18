#!/usr/bin/env bash
# env-tiers.sh — the .env tier cascade, as a standalone query door.
#
# Prints the four tiers in runtime sourcing order, least specific first, one
# TAB-separated row each:
#
#     <tier>\t<path>\t<present|absent|unresolved>
#
# There is no logic here. The order and the row format live in ONE place,
# `env_tier_rows` in lib-common.sh, which is also what start-bot.sh sources at
# boot. This file exists so a non-bash consumer — the Python compositor — can
# ask the runtime its own question instead of keeping a second copy of the
# answer (#1214 / #1226). If the compositor and the runtime each owned this
# order they would drift, which is this repo's most reliable failure mode.
#
# Usage: env-tiers.sh [bot_dir] [fleet_name]
#   CLAUDLOBBY_ROOT, HOME and FLEET_NAME are read from the environment exactly
#   as start-bot.sh reads them, so the answer is the runtime's answer.
set -uo pipefail
LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$LIB_DIR/lib-common.sh"
env_tier_rows "${1:-}" "${2:-}"
