# Private startup/watchdog boundary. Source after lib-common and bot.conf.
# Git decisions live in the copied stdlib launcher; sealed callers retain their adapter.
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
    if [ "${CLAUDLOBBY_INSTALLATION+x}" = x ]; then
        _NATIVE_ADMISSION_GIT=1
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
    fi
    if [ -z "${CLAUDLOBBY_RELEASE_ID:-}" ] || [ -z "${CLAUDLOBBY_ARTIFACT_ID:-}" ]; then
        echo "native admission: composed release and artifact identities required" >&2
        return 7
    fi
    _NATIVE_ADMISSION_PYTHON="${CLAUDLOBBY_CLI%/*}/python"
    _NATIVE_ADMISSION_ROOT="$CLAUDLOBBY_ROOT"
    _NATIVE_ADMISSION_PID="$$"
    _NATIVE_ADMISSION_SUBSHELL="$BASH_SUBSHELL"
    if [ ! -x "$CLAUDLOBBY_CLI" ] || [ ! -x "$_NATIVE_ADMISSION_PYTHON" ]; then
        echo "native admission: selected release interpreter/CLI unavailable" >&2
        return 7
    fi
    # No creation/truncation: the coordinator is the sole lock-file owner.
    exec 9<"$_NATIVE_ADMISSION_ROOT/state/activation.lock" || return 7
    local admission_rc
    if "$_NATIVE_ADMISSION_PYTHON" -I -B -m claudlobby.runtime_admission acquire \
        9 "$_NATIVE_ADMISSION_PID" "$_NATIVE_ADMISSION_ROOT" "$operation" "$BOT_DIR" \
        "$CLAUDLOBBY_RELEASE_ID" "$CLAUDLOBBY_CLI" "$LIB_DIR" "$CLAUDLOBBY_ARTIFACT_ID"; then
        :
    else
        admission_rc=$?
        exec 9<&-
        return "$admission_rc"
    fi
    # Explicit flock unlock releases all inherited copies on normal completion.
    # Without it, a detached tmux/emit child could retain the host lock forever.
    # SIGKILL cannot run cleanup; surviving children then conservatively retain
    # admission until their descriptor closes. Subshell EXIT must not unlock it.
    trap 'native_admission_cleanup; _lc_cleanup' EXIT
}

native_admission_cleanup() {
    [ "$BASH_SUBSHELL" = "$_NATIVE_ADMISSION_SUBSHELL" ] || return 0
    if [ "${_NATIVE_ADMISSION_GIT:-0}" = 1 ]; then
        "$CLAUDLOBBY_CLI" --root "$_NATIVE_ADMISSION_ROOT" _native-shell release \
            9 "$_NATIVE_ADMISSION_PID" || \
            echo "native admission: lock cleanup failed; runtime exclusion may remain held" >&2
        exec 9<&-
        return 0
    fi
    "$_NATIVE_ADMISSION_PYTHON" -I -B -m claudlobby.runtime_admission release \
        9 "$_NATIVE_ADMISSION_PID" "$_NATIVE_ADMISSION_ROOT" || \
        echo "native admission: lock cleanup failed; host admission may remain held" >&2
    exec 9<&-
}
