#!/bin/bash
# Private context validation for native adapters. Source only; Bash builtins
# keep resident launchers independent of Python imports and host PATH probes.

_claudlobby_require_root() {
    case "${CLAUDLOBBY_ROOT:-}" in
        /*) return 0 ;;
    esac
    printf 'claudlobby: CLAUDLOBBY_ROOT must be an explicit absolute data directory (got %s)\n' \
        "${CLAUDLOBBY_ROOT:-<unset>}" >&2
    return 127
}

_claudlobby_require_cli() {
    case "${CLAUDLOBBY_CLI:-}" in
        /*)
            if [ -f "$CLAUDLOBBY_CLI" ] && [ -x "$CLAUDLOBBY_CLI" ]; then
                return 0
            fi
            ;;
    esac
    printf 'claudlobby: CLAUDLOBBY_CLI must name the composed absolute executable (got %s); check the installed runtime\n' \
        "${CLAUDLOBBY_CLI:-<unset>}" >&2
    return 127
}

# These markers route an already admitted Python call back into its native
# implementation. They never replace public admission or native-unit proof.
_claudlobby_runtime_bound() {
    [ -n "${CLAUDLOBBY_INSTALLATION:-}" ] || [ -n "${CLAUDLOBBY_RELEASE_ID:-}" ]
}

_claudlobby_require_native() {
    _claudlobby_require_root || return 2
    _claudlobby_require_cli || return 2
    if [ ! -d "$CLAUDLOBBY_ROOT" ] || [ "$1" != "${CLAUDLOBBY_NATIVE_DIR:-}" ]; then
        printf 'claudlobby: native implementation differs from its explicit runtime context\n' >&2
        return 2
    fi
    case "${CLAUDLOBBY_NATIVE_DIR:-}" in /*) ;; *) return 2 ;; esac
    if [ -n "${CLAUDLOBBY_INSTALLATION:-}" ]; then
        case "$CLAUDLOBBY_INSTALLATION" in /*) ;; *) return 2 ;; esac
        [ -z "${CLAUDLOBBY_RELEASE_ID:-}" ] || return 2
    fi
}

_claudlobby_parse_runtime_scope() {
    local mode="$1" owner="$2" option value
    shift 2
    _claudlobby_require_native "$owner" || return $?
    _CL_RUNTIME_FLEET="" _CL_RUNTIME_FLEET_ROOT="" _CL_RUNTIME_BOTS_DIR="" _CL_RUNTIME_DRY_RUN=0
    _CL_RUNTIME_PLUGINS=() _CL_RUNTIME_BOTS=()
    case "${1:-}" in
        --admitted-runtime)
            [ "${CLAUDLOBBY_NATIVE_ADMITTED:-}" = 1 ] && _claudlobby_runtime_bound || {
                printf 'claudlobby: admitted runtime recursion context is missing\n' >&2; return 2;
            }
            shift ;;
        --selected-release)
            [ "$#" -ge 2 ] && [ -n "${CLAUDLOBBY_RELEASE_ID:-}" ] \
                && [ "$2" = "$CLAUDLOBBY_RELEASE_ID" ] \
                && [ -z "${CLAUDLOBBY_INSTALLATION:-}" ] || {
                printf 'claudlobby: selected release recursion context differs\n' >&2; return 2;
            }
            shift 2 ;;
        *) printf 'claudlobby: admitted runtime argument is required\n' >&2; return 2 ;;
    esac
    while [ "$#" -gt 0 ]; do
        option="$1"
        if [ "$mode:$option" = siblings:--dry-run ]; then
            [ "$_CL_RUNTIME_DRY_RUN" -eq 0 ] || return 2
            _CL_RUNTIME_DRY_RUN=1
            shift
            continue
        fi
        case "$mode:$option" in
            reload:--fleet|reload:--bots-dir|reload:--plugin|reload:--bot|credentials:--fleet|credentials:--fleet-root|credentials:--bots-dir|credentials:--bot) ;;
            *) printf 'claudlobby: unsupported admitted runtime option\n' >&2; return 2 ;;
        esac
        [ "$#" -ge 2 ] || { printf 'claudlobby: runtime option has no value\n' >&2; return 2; }
        value="$2"
        case "$option" in
            --fleet) [ -z "$_CL_RUNTIME_FLEET" ] || return 2; _CL_RUNTIME_FLEET="$value" ;;
            --fleet-root) [ -z "$_CL_RUNTIME_FLEET_ROOT" ] || return 2; _CL_RUNTIME_FLEET_ROOT="$value" ;;
            --bots-dir) [ -z "$_CL_RUNTIME_BOTS_DIR" ] || return 2; _CL_RUNTIME_BOTS_DIR="$value" ;;
            --plugin) _CL_RUNTIME_PLUGINS+=("$value") ;;
            --bot)
                [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || return 2
                _CL_RUNTIME_BOTS+=("$value") ;;
        esac
        shift 2
    done
    case "$mode" in
        reload|credentials)
            [ -n "$_CL_RUNTIME_FLEET" ] && [ "$_CL_RUNTIME_FLEET" = "${CLAUDLOBBY_FLEET:-}" ] || return 2
            case "$_CL_RUNTIME_BOTS_DIR" in /*) ;; *) return 2 ;; esac ;;
        runtime|siblings) return 0 ;;
        *) return 2 ;;
    esac
    if [ "$mode" = credentials ]; then
        case "$_CL_RUNTIME_FLEET_ROOT" in /*) ;; *) return 2 ;; esac
        [ "$_CL_RUNTIME_FLEET_ROOT" = "${FLEET_ROOT:-}" ] \
            && [ "$_CL_RUNTIME_BOTS_DIR" = "$_CL_RUNTIME_FLEET_ROOT/runtime/bots" ] \
            && [ -n "${_CL_RUNTIME_BOTS[0]:-}" ] || return 2
    fi
    return 0
}

_claudlobby_require_pulse_runtime() {
    if _claudlobby_runtime_bound; then
        if { [ "${CLAUDLOBBY_NATIVE_ADMITTED:-}" = 1 ] && [ "${CLAUDLOBBY_PRIVATE_PULSE_RUNTIME:-}" = 1 ]; } \
                || { [ -z "${CLAUDLOBBY_INSTALLATION:-}" ] && [ -n "${CLAUDLOBBY_RELEASE_ID:-}" ] \
                     && [ "${CLAUDLOBBY_PRIVATE_PULSE_RELEASE:-}" = "$CLAUDLOBBY_RELEASE_ID" ]; }; then
            _claudlobby_require_native "$1" || return $?
            [ -n "${2:-}" ] && [ "$2" = "${CLAUDLOBBY_FLEET:-}" ] || return 2
            return 0
        fi
        printf 'fleet-pulse: use claudlobby fleet pulse for the current runtime\n' >&2
        return 3
    fi
    return 0
}
