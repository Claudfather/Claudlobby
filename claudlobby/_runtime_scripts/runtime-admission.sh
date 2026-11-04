# Private startup/watchdog boundary. Source after lib-common and bot.conf.
# Git decisions live in the copied stdlib launcher.
# stop/diagnostic doors and per-tool hooks do not source this adapter.
native_admission() {
    local operation="${1:?native_admission requires an operation}"
    case "${CLAUDLOBBY_ROOT:-}:${BOT_DIR:-}:${CLAUDLOBBY_CLI:-}:${LIB_DIR:-}" in
        /*:/*:/*:/*) ;;
        *) echo "native admission: explicit root, bot, CLI and native paths required" >&2; return 7 ;;
    esac
    _NATIVE_ADMISSION_ROOT="$CLAUDLOBBY_ROOT"
    _NATIVE_ADMISSION_PID="$$"
    _NATIVE_ADMISSION_SUBSHELL="$BASH_SUBSHELL"
    if [ -z "${CLAUDLOBBY_INSTALLATION:-}" ] || [ "${CLAUDLOBBY_RELEASE_ID+x}" = x ] \
            || [ "${CLAUDLOBBY_ARTIFACT_ID+x}" = x ]; then
        echo "native admission: managed Git installation identity required" >&2
        return 7
    fi
    if [ ! -x "$CLAUDLOBBY_CLI" ]; then
        echo "native admission: stable installation CLI unavailable" >&2
        return 7
    fi
    # The updater owns creation; this shell retains the open description.
    exec 9<"$_NATIVE_ADMISSION_ROOT/state/runtime-use.lock" || return 7
    local git_rc
    if "$CLAUDLOBBY_CLI" --root "$_NATIVE_ADMISSION_ROOT" _native-shell acquire \
        9 "$_NATIVE_ADMISSION_PID" "$operation" "$BOT_DIR" "${BOT_SERVICE:-}" "${FLEET_NAME:-}" "$LIB_DIR"; then
        :
    else
        git_rc=$?
        exec 9<&-
        return "$git_rc"
    fi
    if _NATIVE_ADMISSION_PYTHON="$("$CLAUDLOBBY_CLI" --root "$_NATIVE_ADMISSION_ROOT" _native-shell python 9)"; then
        :
    else
        git_rc=$?
        native_admission_cleanup
        return "$git_rc"
    fi
    trap 'native_admission_cleanup; _lc_cleanup' EXIT
    return 0
}

native_admission_cleanup() {
    [ "$BASH_SUBSHELL" = "$_NATIVE_ADMISSION_SUBSHELL" ] || return 0
    "$CLAUDLOBBY_CLI" --root "$_NATIVE_ADMISSION_ROOT" _native-shell release \
        9 "$_NATIVE_ADMISSION_PID" || \
        echo "native admission: lock cleanup failed; runtime exclusion may remain held" >&2
    exec 9<&-
}
