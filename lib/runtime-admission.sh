# Private startup/watchdog boundary. Source after lib-common and bot.conf.
# Semantic decisions live in the selected release's stdlib admission module;
# stop/diagnostic doors and per-tool hooks do not source this adapter.
native_admission() {
    local operation="${1:?native_admission requires an operation}"
    case "${CLAUDLOBBY_ROOT:-}:${BOT_DIR:-}:${CLAUDLOBBY_CLI:-}:${LIB_DIR:-}" in
        /*:/*:/*:/*) ;;
        *) echo "native admission: explicit root, bot, CLI and native paths required" >&2; return 7 ;;
    esac
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
    if ! "$_NATIVE_ADMISSION_PYTHON" -I -B -m claudlobby.runtime_admission acquire \
        9 "$_NATIVE_ADMISSION_PID" "$_NATIVE_ADMISSION_ROOT" "$operation" "$BOT_DIR" \
        "$CLAUDLOBBY_RELEASE_ID" "$CLAUDLOBBY_CLI" "$LIB_DIR" "$CLAUDLOBBY_ARTIFACT_ID"; then
        exec 9<&-
        return 7
    fi
    # Explicit flock unlock releases all inherited copies on normal completion.
    # Without it, a detached tmux/emit child could retain the host lock forever.
    # SIGKILL cannot run cleanup; surviving children then conservatively retain
    # admission until their descriptor closes. Subshell EXIT must not unlock it.
    trap 'native_admission_cleanup; _lc_cleanup' EXIT
}

native_admission_cleanup() {
    [ "$BASH_SUBSHELL" = "$_NATIVE_ADMISSION_SUBSHELL" ] || return 0
    "$_NATIVE_ADMISSION_PYTHON" -I -B -m claudlobby.runtime_admission release \
        9 "$_NATIVE_ADMISSION_PID" "$_NATIVE_ADMISSION_ROOT" || \
        echo "native admission: lock cleanup failed; host admission may remain held" >&2
    exec 9<&-
}
