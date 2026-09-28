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
    printf 'claudlobby: CLAUDLOBBY_CLI must name the composed absolute executable (got %s); recompose with the selected release\n' \
        "${CLAUDLOBBY_CLI:-<unset>}" >&2
    return 127
}
