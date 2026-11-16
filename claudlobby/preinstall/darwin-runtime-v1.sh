#!/bin/bash
# Fixed Mac15+ Apple Silicon runtime preparation; no Apple Python/Git stubs.
# prepare/status/discard: one bounded canonical newline-terminated ASCII frame, never a shell API.
# Unknown attempts remain retained. Explicit discard is exact-owned and finite.
set -euo pipefail
umask 077
unset BASH_ENV ENV CDPATH PYTHONPATH PYTHONHOME
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=C

# BEGIN DARWIN EXISTING PYTHON SHELL
# Reuse this exact block in installer/guide; it consumes no standard input.
DARWIN_RUNTIME_PROFILE=darwin15-arm64-py31216-git2561-tmux38-jq182-v1
DARWIN_RUNTIME_PYTHON_SHA=54788b15120aa4f534da3f59b59f46658a9bfdc3d3d9c8b3af1459002e307c90
darwin_clean_loader_environment() {
    local selector
    for selector in "${!DYLD_@}" "${!LD_@}"; do
        [ -z "$selector" ] || unset "$selector" || return 6
    done
    unset PYTHONPATH PYTHONHOME BASH_ENV ENV
}
darwin_native() {
    local path="$1" actual="$1" facts mode
    if [ "$path" = /usr/bin/tar ] && [ -L "$path" ]; then
        [ "$(/usr/bin/readlink "$path" </dev/null)" = bsdtar ] || [ "$(/usr/bin/readlink "$path" </dev/null)" = /usr/bin/bsdtar ] || return 6
        actual=/usr/bin/bsdtar
    else
        [ ! -L "$path" ] || return 6
    fi
    [ -f "$actual" ] && [ -x "$actual" ] || return 6
    facts=$(/usr/bin/stat -L -f '%u:%Lp' "$actual" </dev/null) || return 6
    case "$facts" in 0:*) ;; *) return 6 ;; esac
    mode=${facts#*:}
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 8#022) == 0 )) || return 6
    /usr/bin/codesign --verify --strict -R '=anchor apple' "$actual" </dev/null >/dev/null 2>&1 || return 6
}
darwin_hash() {
    local output
    output=$(/usr/bin/openssl dgst -sha256 "$1" </dev/null) || return 6
    output=${output##* }
    [[ "$output" =~ ^[0-9a-f]{64}$ ]] || return 6
    printf '%s' "$output"
}
darwin_identity() {
    local expected_uid="$1" expected_account="${2-}" record key value native_uid native_account major
    darwin_clean_loader_environment || return 6
    for value in /usr/bin/stat /usr/bin/codesign /usr/bin/id /usr/bin/dscl /usr/bin/uname /usr/bin/sw_vers /usr/bin/openssl /usr/bin/false /usr/bin/env; do
        darwin_native "$value" || return 6
    done
    [ "$(/usr/bin/uname -s </dev/null)" = Darwin ] && [ "$(/usr/bin/uname -m </dev/null)" = arm64 ] || return 6
    major=$(/usr/bin/sw_vers -productVersion </dev/null) || return 6; major=${major%%.*}
    [[ "$major" =~ ^[0-9]{1,3}$ ]] && [ "$major" -ge 15 ] || return 6
    native_uid=$(/usr/bin/id -u </dev/null) || return 6; [ "$native_uid" != 0 ] && [ "$native_uid" = "$(/usr/bin/id -ru </dev/null)" ] && [ "$native_uid" = "$expected_uid" ] || return 6
    native_account=$(/usr/bin/id -un </dev/null) || return 6
    [[ "$native_account" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,63}$ ]] || return 6
    [ -z "$expected_account" ] || [ "$native_account" = "$expected_account" ] || return 6
    DARWIN_RUNTIME_HOME=; DARWIN_RUNTIME_UID=; DARWIN_RUNTIME_ACCOUNT="$native_account"
    record=$(/usr/bin/dscl . -read "/Users/$native_account" NFSHomeDirectory UniqueID </dev/null) || return 6
    while IFS=' ' read -r key value; do
        case "$key" in NFSHomeDirectory:) DARWIN_RUNTIME_HOME="$value" ;; UniqueID:) DARWIN_RUNTIME_UID="$value" ;; *) return 6 ;; esac
    done <<< "$record"
    [ "$DARWIN_RUNTIME_UID" = "$native_uid" ] || return 6
    [[ "$DARWIN_RUNTIME_HOME" =~ ^/[A-Za-z0-9_./-]+$ ]] && [ "$DARWIN_RUNTIME_HOME" != / ] || return 6
    case "$DARWIN_RUNTIME_HOME/" in */../*|*/./*|*//*) return 6 ;; esac
    [ -d "$DARWIN_RUNTIME_HOME" ] && [ ! -L "$DARWIN_RUNTIME_HOME" ] || return 6
    [ "$(/usr/bin/stat -f %u "$DARWIN_RUNTIME_HOME" </dev/null)" = "$native_uid" ] || return 6
    local ancestor="$DARWIN_RUNTIME_HOME" facts perms
    while [ "$ancestor" != / ]; do
        [ -d "$ancestor" ] && [ ! -L "$ancestor" ] || return 6
        facts=$(/usr/bin/stat -f '%u:%Lp' "$ancestor" </dev/null) || return 6
        case "$facts" in "$native_uid":*|0:*) ;; *) return 6 ;; esac
        perms=${facts#*:}; [[ "$perms" =~ ^[0-7]{3,4}$ ]] && (( (8#$perms & 8#022) == 0 )) || return 6
        ancestor=${ancestor%/*}; [ -n "$ancestor" ] || ancestor=/
    done
    DARWIN_RUNTIME_BASE="$DARWIN_RUNTIME_HOME/.local/share/claudlobby-browser/runtime"
    DARWIN_RUNTIME_ROOT="$DARWIN_RUNTIME_BASE/profile"
}
darwin_python_identity() {
    local parent="$DARWIN_RUNTIME_PYTHON" facts mode
    while [ "$parent" != / ]; do
        [ ! -L "$parent" ] || return 6
        facts=$(/usr/bin/stat -f '%u:%Lp' "$parent" </dev/null) || return 6
        case "$facts" in "$DARWIN_RUNTIME_UID":*|0:*) ;; *) return 6 ;; esac
        mode=${facts#*:}; [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 8#022) == 0 )) || return 6
        parent=${parent%/*}; [ -n "$parent" ] || parent=/
    done
    [ -f "$DARWIN_RUNTIME_PYTHON" ] && [ -x "$DARWIN_RUNTIME_PYTHON" ] || return 6
    [ "$(/usr/bin/stat -f %l "$DARWIN_RUNTIME_PYTHON" </dev/null)" = 1 ] || return 6
    [ "$(darwin_hash "$DARWIN_RUNTIME_PYTHON")" = "$DARWIN_RUNTIME_PYTHON_SHA" ] || return 6
}
darwin_existing_python() {
    darwin_identity "$1" "${2-}" || return 6
    DARWIN_RUNTIME_PYTHON="$DARWIN_RUNTIME_ROOT/python/bin/python3.12"
    darwin_python_identity || return 6
    [ -f "$DARWIN_RUNTIME_ROOT/receipt.json" ] && [ ! -L "$DARWIN_RUNTIME_ROOT/receipt.json" ] || return 6
}
# END DARWIN EXISTING PYTHON SHELL

# Fixed shell bootstrap constants are mechanically compared with canonical policy.
DARWIN_RUNTIME_POLICY_SHA=4401ff732de0cff93c22f81fbba5af2c3878dfaba6d4fda6533689640abf475c
DARWIN_RUNTIME_PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz
DARWIN_RUNTIME_PYTHON_BYTES=25371130
DARWIN_RUNTIME_ARCHIVE_SHA=e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f

darwin_bootstrap_record() {
    printf '{"binding":{"account":"%s","arch":"arm64","home":"%s","policy_sha256":"%s","profile_id":"%s","uid":%s},"files":{"python.tar.gz":[%s,%s]},"phase":"bootstrap","request_id":"%s","schema_version":1,"stage_device":%s,"stage_inode":%s}' "$account" "$DARWIN_RUNTIME_HOME" "$DARWIN_RUNTIME_POLICY_SHA" "$DARWIN_RUNTIME_PROFILE" "$uid" "$archive_device" "$archive_inode" "$request_id" "$device" "$inode"
}

darwin_lock() {
    local facts descriptor_facts path_identity rc=0
    darwin_native /usr/bin/lockf || refuse unsafe_native_tool
    lock="$DARWIN_RUNTIME_BASE/transition.lock"
    [ -f "$lock" ] && [ ! -L "$lock" ] || refuse unsafe_lock
    facts=$(/usr/bin/stat -f '%u:%Lp:%l:%z' "$lock") || refuse unsafe_lock
    [ "$facts" = "$uid:600:1:0" ] || refuse unsafe_lock
    path_identity=$(/usr/bin/stat -f '%d:%i' "$lock") || refuse lock_identity_changed
    exec 9<> "$lock" || refuse unsafe_lock
    # fdesc reports a virtual device on Darwin. Compare its inode/metadata,
    # keep the path's original device+inode stable, then let Python fstat bind it.
    descriptor_facts=$(/usr/bin/stat -L -f '%HT:%i:%u:%Lp:%l:%z' /dev/fd/9) || refuse lock_identity_changed
    facts=$(/usr/bin/stat -f '%HT:%i:%u:%Lp:%l:%z' "$lock") || refuse lock_identity_changed
    [ "$descriptor_facts" = "$facts" ] || refuse lock_identity_changed
    [ "${facts%%:*}" = 'Regular File' ] || refuse unsafe_lock
    [ "$(/usr/bin/stat -f '%d:%i' "$lock")" = "$path_identity" ] || refuse lock_identity_changed
    /usr/bin/lockf -s -t 0 9 || rc=$?
    case "$rc" in 0) ;; 75) refuse preparation_busy ;; *) refuse unsupported_native_lock ;; esac
    [ "$(/usr/bin/stat -f '%d:%i' "$lock")" = "$path_identity" ] || refuse lock_identity_changed
}

# Only the exact original archive-only bootstrap can be discarded without Python.
# Partial extraction, killed record writes and any foreign names remain retained.
darwin_discard_early() {
    local parent="$DARWIN_RUNTIME_BASE" facts retained
    for tool in /usr/bin/cmp /bin/rm /bin/rmdir; do darwin_native "$tool" || refuse unsafe_native_tool; done
    while [ "$parent" != "$DARWIN_RUNTIME_HOME" ]; do
        [ -d "$parent" ] && [ ! -L "$parent" ] || refuse unsafe_directory
        facts=$(/usr/bin/stat -f '%u:%Lp' "$parent") || refuse unsafe_directory
        [ "${facts%%:*}" = "$uid" ] || refuse unsafe_directory
        (( (8#${facts#*:} & 8#022) == 0 )) || refuse unsafe_directory
        parent=${parent%/*}
    done
    [ "$(/usr/bin/stat -f %Lp "$DARWIN_RUNTIME_BASE")" = 700 ] || refuse unsafe_directory
    darwin_lock
    stage="$DARWIN_RUNTIME_BASE/stage-$request_id"
    for retained in "$DARWIN_RUNTIME_BASE"/* "$DARWIN_RUNTIME_BASE"/.[!.]* "$DARWIN_RUNTIME_BASE"/..?*; do
        [ -e "$retained" ] || [ -L "$retained" ] || continue
        case "$retained" in "$lock"|"$DARWIN_RUNTIME_BASE/attempt.json"|"$stage") ;; *) refuse foreign_runtime_content ;; esac
    done
    [ -d "$stage" ] && [ ! -L "$stage" ] || refuse original_attempt_required
    facts=$(/usr/bin/stat -f '%u:%Lp' "$stage") || refuse stage_identity_changed
    [ "$facts" = "$uid:700" ] || refuse stage_identity_changed
    for retained in "$stage"/* "$stage"/.[!.]* "$stage"/..?*; do
        [ -e "$retained" ] || [ -L "$retained" ] || continue
        [ "$retained" = "$stage/python.tar.gz" ] || refuse bootstrap_not_admitted
    done
    [ -f "$stage/python.tar.gz" ] && [ ! -L "$stage/python.tar.gz" ] || refuse unknown_stage_content
    facts=$(/usr/bin/stat -f '%u:%Lp:%l:%z' "$stage/python.tar.gz") || refuse unknown_stage_content
    case "$facts" in "$uid:600:1:"*) ;; *) refuse unknown_stage_content ;; esac
    [ "${facts##*:}" -le "$DARWIN_RUNTIME_PYTHON_BYTES" ] || refuse unknown_stage_content
    [ -f "$DARWIN_RUNTIME_BASE/attempt.json" ] && [ ! -L "$DARWIN_RUNTIME_BASE/attempt.json" ] || refuse original_attempt_required
    facts=$(/usr/bin/stat -f '%u:%Lp:%l:%z' "$DARWIN_RUNTIME_BASE/attempt.json") || refuse attempt_changed
    case "$facts" in "$uid:600:1:"*) ;; *) refuse attempt_changed ;; esac
    [ "${facts##*:}" -le 2048 ] || refuse attempt_changed
    device=$(/usr/bin/stat -f %d "$stage") || refuse stage_identity_changed
    inode=$(/usr/bin/stat -f %i "$stage") || refuse stage_identity_changed
    archive_device=$(/usr/bin/stat -f %d "$stage/python.tar.gz") || refuse unknown_stage_content
    archive_inode=$(/usr/bin/stat -f %i "$stage/python.tar.gz") || refuse unknown_stage_content
    /usr/bin/cmp -s "$DARWIN_RUNTIME_BASE/attempt.json" <(darwin_bootstrap_record) || refuse attempt_changed
    /bin/rm "$stage/python.tar.gz" || refuse discard_unknown
    /bin/rmdir "$stage" || refuse discard_unknown
    /bin/rm "$DARWIN_RUNTIME_BASE/attempt.json" || refuse discard_unknown
    printf '{"result":{"installed_profile_unchanged":true,"request_id":"%s","state":"discarded"},"schema_version":1}\n' "$request_id"
    exit 0
}

refuse() { printf '{"code":"%s","schema_version":1,"state":"unavailable"}\n' "$1"; exit 6; }
for inherited in "${!BOT_@}" "${!CLAUDLOBBY_@}"; do
    [ -z "$inherited" ] || refuse inherited_context_refused
done
[ "$#" = 1 ] || refuse invalid_request
case "$1" in prepare|status|discard) verb="$1" ;; *) refuse invalid_operation ;; esac
request=; IFS= read -r -t 5 request || refuse invalid_request
[ "${#request}" -le 2048 ] || refuse invalid_request
# Bash3.2 returns1 for EOF and timeout: this is extra-record detection,
# not proof of EOF. One frame is parsed; close input before any native child.
rc=0; IFS= read -r -t 1 extra || rc=$?
[ "$rc" = 1 ] && [ -z "${extra-}" ] || refuse invalid_request
exec 0</dev/null
pattern='^\{"expected_account":"([A-Za-z_][A-Za-z0-9._-]{0,63})","expected_uid":([1-9][0-9]{0,9}),"request_id":"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})","schema_version":1\}$'
[[ "$request" =~ $pattern ]] || refuse invalid_request
account=${BASH_REMATCH[1]}; uid=${BASH_REMATCH[2]}; request_id=${BASH_REMATCH[3]}
mode=existing
if ! darwin_existing_python "$uid" "$account"; then
    # Missing/changed installed state is never overwritten or adopted.
    darwin_identity "$uid" "$account" || refuse unsupported_darwin_profile
    if [ "$verb" = status ]; then
        parent="$DARWIN_RUNTIME_BASE"
        while [ "$parent" != "$DARWIN_RUNTIME_HOME" ]; do
            if [ -e "$parent" ] || [ -L "$parent" ]; then
                [ -d "$parent" ] && [ ! -L "$parent" ] || refuse unsafe_directory
                facts=$(/usr/bin/stat -f '%u:%Lp' "$parent") || refuse unsafe_directory
                [ "${facts%%:*}" = "$uid" ] && (( (8#${facts#*:} & 8#022) == 0 )) || refuse unsafe_directory
            fi
            parent=${parent%/*}
        done
        [ ! -e "$DARWIN_RUNTIME_ROOT" ] && [ ! -L "$DARWIN_RUNTIME_ROOT" ] || refuse runtime_not_admitted
        [ ! -e "$DARWIN_RUNTIME_BASE/attempt.json" ] && [ ! -L "$DARWIN_RUNTIME_BASE/attempt.json" ] || refuse original_attempt_retained
        printf '{"result":{"never_replay":true,"request_id":"%s","state":"absent"},"schema_version":1}\n' "$request_id"
        exit 0
    fi
    [ ! -e "$DARWIN_RUNTIME_ROOT" ] && [ ! -L "$DARWIN_RUNTIME_ROOT" ] || refuse runtime_not_admitted
    if [ "$verb" = discard ]; then
        # Only execute the exact original staged interpreter; canonical policy
        # validates the original request/stage inode and closed partial tree.
        [ -f "$DARWIN_RUNTIME_BASE/attempt.json" ] && [ ! -L "$DARWIN_RUNTIME_BASE/attempt.json" ] || refuse original_attempt_required
        DARWIN_RUNTIME_PYTHON="$DARWIN_RUNTIME_BASE/stage-$request_id/profile/python/bin/python3.12"
        if [ ! -e "$DARWIN_RUNTIME_BASE/stage-$request_id/profile" ] && [ ! -L "$DARWIN_RUNTIME_BASE/stage-$request_id/profile" ]; then darwin_discard_early; fi
        darwin_python_identity || refuse bootstrap_not_admitted
        mode=existing
    else
    for tool in /bin/mkdir /usr/bin/lockf /usr/bin/curl /usr/bin/tar; do darwin_native "$tool" || refuse unsafe_native_tool; done
    parent="$DARWIN_RUNTIME_HOME"
    for suffix in .local share claudlobby-browser runtime; do
        parent="$parent/$suffix"
        if [ ! -e "$parent" ] && [ ! -L "$parent" ]; then /bin/mkdir -m 700 "$parent" || refuse directory_conflict; fi
        [ -d "$parent" ] && [ ! -L "$parent" ] || refuse unsafe_directory
        facts=$(/usr/bin/stat -f '%u:%Lp' "$parent" </dev/null) || refuse unsafe_directory
        [ "${facts%%:*}" = "$uid" ] || refuse unsafe_directory
        perms=${facts#*:}; (( (8#$perms & 8#022) == 0 )) || refuse unsafe_directory
    done
    [ "$(/usr/bin/stat -f %Lp "$DARWIN_RUNTIME_BASE")" = 700 ] || refuse unsafe_directory
    lock="$DARWIN_RUNTIME_BASE/transition.lock"
    if [ ! -e "$lock" ] && [ ! -L "$lock" ]; then (set -C; : > "$lock") || refuse lock_conflict; fi
    darwin_lock
    [ ! -e "$DARWIN_RUNTIME_BASE/attempt.json" ] && [ ! -L "$DARWIN_RUNTIME_BASE/attempt.json" ] || refuse original_attempt_retained
    for retained in "$DARWIN_RUNTIME_BASE"/* "$DARWIN_RUNTIME_BASE"/.[!.]* "$DARWIN_RUNTIME_BASE"/..?*; do
        [ -e "$retained" ] || [ -L "$retained" ] || continue
        [ "$retained" = "$lock" ] || refuse foreign_runtime_content
    done
    stage="$DARWIN_RUNTIME_BASE/stage-$request_id"
    /bin/mkdir -m 700 "$stage" || refuse stage_conflict
    device=$(/usr/bin/stat -f %d "$stage") || refuse stage_identity_changed
    inode=$(/usr/bin/stat -f %i "$stage") || refuse stage_identity_changed
    (set -C; : > "$stage/python.tar.gz") || refuse stage_conflict
    archive_device=$(/usr/bin/stat -f %d "$stage/python.tar.gz") || refuse unknown_stage_content
    archive_inode=$(/usr/bin/stat -f %i "$stage/python.tar.gz") || refuse unknown_stage_content
    # Before the first child write, retain exact attempt and registered inode.
    (set -C; darwin_bootstrap_record > "$DARWIN_RUNTIME_BASE/attempt.json") || refuse attempt_conflict
    /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$DARWIN_RUNTIME_HOME" /usr/bin/curl -q --silent --show-error --fail --location --max-redirs 3 --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 15 --max-time 300 --retry 0 --max-filesize "$DARWIN_RUNTIME_PYTHON_BYTES" --output "$stage/python.tar.gz" "$DARWIN_RUNTIME_PYTHON_URL" || refuse bootstrap_download_unknown
    [ "$(/usr/bin/stat -f %z "$stage/python.tar.gz")" = "$DARWIN_RUNTIME_PYTHON_BYTES" ] && [ "$(darwin_hash "$stage/python.tar.gz")" = "$DARWIN_RUNTIME_ARCHIVE_SHA" ] || refuse artifact_mismatch
    /bin/mkdir -m 700 "$stage/profile" || refuse stage_conflict
    # SHA-bound known archive census was statically reviewed; destination is new.
    (umask 022; /usr/bin/tar --no-same-owner --no-same-permissions --keep-old-files --safe-writes -xzf "$stage/python.tar.gz" -C "$stage/profile") || refuse bootstrap_extraction_unknown
    DARWIN_RUNTIME_PYTHON="$stage/profile/python/bin/python3.12"
    [ ! -L "$DARWIN_RUNTIME_PYTHON" ] && [ -f "$DARWIN_RUNTIME_PYTHON" ] && [ "$(darwin_hash "$DARWIN_RUNTIME_PYTHON")" = "$DARWIN_RUNTIME_PYTHON_SHA" ] || refuse bootstrap_python_changed
    mode=bootstrap
    fi
fi
# Completed-profile prepare/discard also needs the same stable inherited lock.
if [ "$verb" != status ] && [ "$mode" = existing ]; then
    darwin_lock
fi
# Exact Python executable checked before loading; canonical tree admission follows.
# Shared libraries/stdlib load before that full check; no hostile-tree guarantee.
exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$DARWIN_RUNTIME_HOME" LC_ALL=C "$DARWIN_RUNTIME_PYTHON" -I -S -B - "$verb" "$request" "$mode" <<'CANONICAL_DARWIN_RUNTIME'
# BEGIN CANONICAL DARWIN RUNTIME PYTHON
"""One source-pinned Apple Silicon macOS15 tool profile; no package manager.

Read-only admission is shared with the preinstall entry through exact embedding.
Only the explicit preinstall prepare/discard operations write the fixed namespace.
Installation schema1, services, provider profiles and personal tools are untouched.
"""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import posixpath
import pwd
import re
import shutil
import ssl
import stat
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
import uuid

PROFILE_ID = "darwin15-arm64-py31216-git2561-tmux38-jq182-v1"
LIMIT = 16384
BUDGET = 900
_INHERITED_LOCK_FD = None
ALIASES = {"python3": "../python/bin/python3.12", "git": "../git/bin/git",
           "tmux": "../tmux/bin/tmux", "jq": "../jq/bin/jq"}
CA_MEMBER = "lib/python3.12/site-packages/pip/_vendor/certifi/cacert.pem"
# BEGIN FIXED DESCRIPTOR
_DESCRIPTOR = {
    "artifacts": {
        "python": {
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz",
            "sha256": "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
            "bytes": 25371130,
            "members": 1700,
            "unpacked_bytes": 66987437,
            "members_sha256": "75d048f97433b041ef51659aa5a2c0545ff440f6143588bdbde2a680a6770887",
            "tree_sha256": "03ae59f570105cc7292b30cdeff01877747cee805513b24296d0faa003738a46"
        },
        "git": {
            "url": "https://github.com/desktop/dugite-native/releases/download/v2.56.1/dugite-native-v2.56.1-238326e-macOS-arm64.tar.gz",
            "sha256": "a882ec6882416b57d212a22bac845ae27944f96cf73453a864f6a12c17863a79",
            "bytes": 63505330,
            "members": 488,
            "unpacked_bytes": 157153052,
            "members_sha256": "6f1be15ddb8afc34b88c6159281e7c93b7f5eb258a43d23307f86c72d6b9801b",
            "tree_sha256": "fcabc6bc008eceb0afcc3b168f678590f9f168acb506cbd90093e2867800e5bf"
        },
        "tmux": {
            "url": "https://github.com/tmux/tmux-builds/releases/download/v3.8/tmux-3.8-macos-arm64.tar.gz",
            "sha256": "ec926bbf28ddf55d0dc7c9791d43e03e251ddab392d763f7fbb38d4b8408d051",
            "bytes": 725788,
            "members": 1,
            "unpacked_bytes": 1877512,
            "members_sha256": "0281fcbee97a4faef725d304ab271c61e0ba0c82d8d5b343fb44f5557288973b",
            "tree_sha256": "b3217e73158027cf09d6ca3ecfc1a217bdb90b967d6fd06209ccb25c8d0ac843"
        },
        "jq": {
            "url": "https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-macos-arm64",
            "sha256": "2d75340ba57a4b4b4c8708a21c2dc8e958a48aaa8bba13b27f77f6e4c0eca07e",
            "bytes": 841504,
            "members": 0,
            "unpacked_bytes": 0,
            "members_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "tree_sha256": "260946517edd7da43b09a0a1473a828196af646613914a04c7840439115d4940"
        }
    },
    "python_executable_sha256": "54788b15120aa4f534da3f59b59f46658a9bfdc3d3d9c8b3af1459002e307c90",
    "ca_sha256": "2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b"
}
# END FIXED DESCRIPTOR


class RuntimeRefusal(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise RuntimeRefusal(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _policy():
    return _sha(_canonical({"profile_id": PROFILE_ID, "architecture": "arm64", "minimum_macos": 15,
                           "descriptor": _DESCRIPTOR, "aliases": ALIASES, "file_modes": [0o555, 0o444], "profile_mode": 0o700,
                           "git_helpers": "builtin-links-http-notice-closed-v2", "bytecode_writes": False, "receipt_schema": 1,
                           "root": "runtime/profile", "paths": ["python/bin/python3.12", "git/bin/git", "git/libexec/git-core", "tmux/bin/tmux", "jq/bin/jq", "ca-bundle.pem"]}))


@dataclass(frozen=True)
class RuntimeProfile:
    uid: int
    account: str
    home: Path
    root: Path
    bin: Path
    python: Path
    git: Path
    git_exec_path: Path
    tmux: Path
    jq: Path
    ca_bundle: Path
    policy_sha256: str


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_request(operation, value):
    _require(operation in ("prepare", "status", "discard"), "invalid_operation")
    _require(type(value) is dict and set(value) == {"schema_version", "request_id", "expected_uid", "expected_account"}, "invalid_request")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1 and _uuid(value["request_id"]), "invalid_request")
    _require(type(value["expected_uid"]) is int and 0 < value["expected_uid"] < 2**32 - 1, "invalid_request")
    _require(type(value["expected_account"]) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]{0,63}", value["expected_account"]), "invalid_request")
    return value


def _parents(path, uid):
    for parent in reversed(path.parents):
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022, "unsafe_ancestry")


def _identity(*, expected_uid=None, expected_account=None):
    _require(platform.system() == "Darwin" and platform.machine() == "arm64", "unsupported_native_architecture")
    release = platform.mac_ver()[0].split(".")
    _require(release and release[0].isdigit() and int(release[0]) >= 15, "macos15_required")
    uid = os.getuid()
    _require(uid > 0 and uid == os.geteuid() and os.getgid() == os.getegid(), "ordinary_account_required")
    account = pwd.getpwuid(uid)
    _require((expected_uid is None or uid == expected_uid) and
             (expected_account is None or account.pw_name == expected_account), "account_changed")
    home = Path(account.pw_dir)
    _require(home.is_absolute() and str(home) != "/" and str(home.resolve(strict=True)) == str(home)
             and re.fullmatch(r"/[A-Za-z0-9_./-]+", str(home)), "unsupported_account_home")
    _parents(home, uid)
    info = home.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022, "unsafe_account_home")
    return RuntimeProfile(uid, account.pw_name, home, home, home, home, home, home, home, home, home, _policy())


def _base(identity):
    return identity.home / ".local/share/claudlobby-browser/runtime"


def _profile(identity):
    root = _base(identity) / "profile"
    return RuntimeProfile(identity.uid, identity.account, identity.home, root, root / "bin",
                          root / "python/bin/python3.12", root / "git/bin/git", root / "git/libexec/git-core",
                          root / "tmux/bin/tmux", root / "jq/bin/jq", root / "ca-bundle.pem", _policy())


def _directory(path, uid, mode=None, create=False):
    if create:
        try:
            path.mkdir(mode=mode or 0o700)
            os.chmod(path, mode or 0o700, follow_symlinks=False)
        except FileExistsError:
            pass
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022
             and (mode is None or stat.S_IMODE(info.st_mode) == mode), "unsafe_directory")
    return info


def _read(path, uid, limit=LIMIT, mode=0o600):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "unsafe_file")
        raw = os.read(fd, limit + 1)
        _require(len(raw) <= limit, "oversize_file")
        return raw
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_field")
        result[key] = value
    return result


def _load(path, uid, *, mode=0o600):
    try:
        return json.loads(_read(path, uid, mode=mode), object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise RuntimeRefusal("invalid_retained_metadata") from None


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path, value, uid, *, new=False):
    raw = _canonical(value)
    _require(len(raw) <= LIMIT, "metadata_oversize")
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
    else:
        _read(path, uid)
        fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    _sync(path.parent)


def _binding(identity):
    return {"uid": identity.uid, "account": identity.account, "home": str(identity.home), "arch": "arm64",
            "profile_id": PROFILE_ID, "policy_sha256": _policy()}


def _tree(path, uid, *, staged=False):
    _directory(path, uid, 0o755 if staged else 0o555)
    records = []
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in sorted(directories + files):
            child = Path(directory) / name
            relative = child.relative_to(path).as_posix(); info = child.lstat()
            _require(info.st_uid == uid, "foreign_tool_tree")
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(child)
                _safe_link(relative, target)
                records.append({"name": relative, "type": "symlink", "link": target})
                if name in directories:
                    directories.remove(name)
            elif stat.S_ISDIR(info.st_mode):
                _require(stat.S_IMODE(info.st_mode) == (0o755 if staged else 0o555), "unsafe_tool_mode")
                records.append({"name": relative, "type": "directory", "mode": 0o555})
            else:
                mode = stat.S_IMODE(info.st_mode)
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and mode in ((0o644, 0o755) if staged else (0o444, 0o555)), "unsafe_tool_file")
                raw = _read(child, uid, limit=128 * 1024 * 1024, mode=mode)
                records.append({"name": relative, "type": "file", "mode": mode & ~0o222, "sha256": _sha(raw), "bytes": len(raw)})
    return _sha(_canonical(sorted(records, key=lambda row: row["name"])))


def _safe_name(name):
    _require(name and not name.startswith("/") and "\\" not in name and
             all(part not in ("", ".", "..") for part in name.split("/")), "unsafe_archive_name")
    return name


def _safe_link(name, target):
    _require(target and not target.startswith("/") and "\\" not in target, "unsafe_archive_link")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    _require(resolved not in (".", "..") and not resolved.startswith("../"), "unsafe_archive_link")
    return resolved


def _selection(member, kind):
    name = member.name.removeprefix("./").rstrip("/")
    if kind == "python":
        return name[7:] if name.startswith("python/") else None
    if kind == "tmux":
        return "bin/tmux" if name == "tmux" else None
    if kind == "git":
        if name in ("bin/git", "libexec/git-core/git", "libexec/git-core/git-remote-http", "libexec/git-core/NOTICE", "COPYING", "LICENSE", "LICENSE.txt") or name.startswith("share/git-core/templates/"):
            return name
        if name.startswith("libexec/git-core/") and member.issym() and member.linkname in ("git", "../../bin/git", "git-remote-http"):
            return name
    return None


def _inventory(archive, descriptor):
    members = archive.getmembers(); records = []; names = set(); links = {}
    _require(len(members) <= descriptor["members"] + 1, "archive_member_drift")
    for member in members:
        name = member.name.removeprefix("./").rstrip("/")
        if name in ("", "."):
            _require(member.isdir(), "unsafe_archive_name"); continue
        _safe_name(name)
        _require(name not in names and not member.mode & 0o7000, "archive_member_drift")
        names.add(name)
        kind = "directory" if member.isdir() else "file" if member.isfile() else "symlink" if member.issym() else None
        _require(kind is not None, "unsafe_archive_type")
        if member.issym():
            links[name] = _safe_link(name, member.linkname)
        records.append({"name": name, "type": kind, "mode": member.mode, "bytes": member.size, "link": member.linkname})
    _require(len(records) == descriptor["members"] and sum(row["bytes"] for row in records) == descriptor["unpacked_bytes"]
             and _sha(_canonical(sorted(records, key=lambda row: row["name"]))) == descriptor["members_sha256"], "archive_member_drift")
    for name in names:
        _require(not any(str(parent) in links for parent in PurePosixPath(name).parents), "archive_symlink_parent")
    for name in links:
        seen = {name}; target = links[name]
        while target in links:
            _require(target not in seen, "archive_link_cycle"); seen.add(target); target = links[target]
        _require(target in names, "archive_link_missing")
    return members


def _extract(path, destination, kind, descriptor, uid):
    _require(_sha(_read(path, uid, descriptor["bytes"])) == descriptor["sha256"], "artifact_mismatch")
    _directory(destination, uid, 0o755, create=True)
    with tarfile.open(path, mode="r:gz") as archive:
        members = _inventory(archive, descriptor)
        selected = {_selection(member, kind): member for member in members if _selection(member, kind)}
        for name, member in selected.items():
            if not member.issym():
                continue
            seen = {name}; target = _safe_link(name, member.linkname)
            while target in selected and selected[target].issym():
                _require(target not in seen, "archive_link_cycle")
                seen.add(target); target = _safe_link(target, selected[target].linkname)
            _require(target in selected, "selected_archive_link_missing")
        links = []
        for member in members:
            relative = _selection(member, kind)
            if not relative:
                continue
            _safe_name(relative); target = destination / relative
            for parent in reversed(target.relative_to(destination).parents):
                if str(parent) != ".":
                    _directory(destination / parent, uid, 0o755, create=True)
            if member.isdir():
                _directory(target, uid, 0o755, create=True)
            elif member.issym():
                _safe_link(relative, member.linkname); links.append((target, member.linkname))
            else:
                mode = 0o755 if member.mode & 0o111 else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
                with archive.extractfile(member) as source, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush(); os.fsync(output.fileno())
                os.chmod(target, mode, follow_symlinks=False)
        for path, link in links:
            path.symlink_to(link)
    _require(_tree(destination, uid, staged=True) == descriptor["tree_sha256"], "extracted_tree_mismatch")


def _verify_profile(profile, *, staged=False):
    _parents(profile.root, profile.uid)
    _directory(profile.root, profile.uid, 0o700)
    _require({p.name for p in profile.root.iterdir()} == {"python", "git", "tmux", "jq", "bin", "ca-bundle.pem", "receipt.json"}, "foreign_profile_content")
    receipt = _load(profile.root / "receipt.json", profile.uid, mode=0o600 if staged else 0o444)
    _require(type(receipt) is dict and set(receipt) == {"schema_version", "binding", "request_id", "state"}
             and type(receipt["schema_version"]) is int and receipt["schema_version"] == 1 and receipt["binding"] == _binding(profile)
             and receipt["state"] == "ready" and _uuid(receipt["request_id"]), "runtime_receipt_changed")
    for kind, descriptor in _DESCRIPTOR["artifacts"].items():
        _require(_tree(profile.root / kind, profile.uid, staged=staged) == descriptor["tree_sha256"], "runtime_tree_changed")
    _require(_sha(_read(profile.ca_bundle, profile.uid, 1024 * 1024, 0o644 if staged else 0o444)) == _DESCRIPTOR["ca_sha256"], "runtime_ca_changed")
    _directory(profile.bin, profile.uid, 0o755 if staged else 0o555)
    _require({p.name for p in profile.bin.iterdir()} == set(ALIASES), "runtime_alias_changed")
    for name, target in ALIASES.items():
        info = (profile.bin / name).lstat()
        _require(stat.S_ISLNK(info.st_mode) and info.st_uid == profile.uid and os.readlink(profile.bin / name) == target, "runtime_alias_changed")
    return receipt


def _seal_profile(profile):
    # Only the complete, exact staged profile can cross to immutable modes.
    _verify_profile(profile, staged=True)
    # Darwin requires write permission on a directory moved across parents.
    # Keep only this private container0700; every component directory is0555.
    for directory, directories, files in os.walk(profile.root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name; info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, 0o555 if info.st_mode & 0o111 else 0o444, follow_symlinks=False)
        path = Path(directory)
        os.chmod(path, 0o700 if path == profile.root else 0o555, follow_symlinks=False)
    _verify_profile(profile)


def admit_profile(*, expected_uid=None, expected_account=None):
    identity = _identity(expected_uid=expected_uid, expected_account=expected_account)
    profile = _profile(identity)
    _require(profile.root.exists() or profile.root.is_symlink(), "runtime_absent")
    _parents(profile.root, profile.uid)
    _verify_profile(profile)
    return profile


def environment(profile):
    # Callers first gate the exact derived browser installation family.
    _verify_profile(profile)
    return {"HOME": str(profile.home), "PATH": str(profile.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "CLAUDLOBBY_NATIVE_PYTHON": str(profile.python), "TMUX_BIN": str(profile.tmux),
            "GIT_EXEC_PATH": str(profile.git_exec_path), "GIT_TEMPLATE_DIR": str(profile.root / "git/share/git-core/templates"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false", "GIT_SSL_CAINFO": str(profile.ca_bundle),
            "SSL_CERT_FILE": str(profile.ca_bundle), "PIP_CONFIG_FILE": "/dev/null", "PIP_NO_INPUT": "1", "PLANE_EMIT_DISABLED": "1"}


def git_argv(profile, *arguments):
    _verify_profile(profile)
    return [str(profile.git), *arguments]


def _attempt(base, identity):
    value = _load(base / "attempt.json", identity.uid)
    _require(type(value) is dict and set(value) == {"schema_version", "binding", "request_id", "stage_device", "stage_inode", "phase", "files"}
             and type(value["schema_version"]) is int and value["schema_version"] == 1 and value["binding"] == _binding(identity) and _uuid(value["request_id"])
             and value["phase"] in ("bootstrap", "preparing", "ready", "failed") and type(value["files"]) is dict, "attempt_changed")
    _require(all(type(value[key]) is int and value[key] > 0 for key in ("stage_device", "stage_inode")), "attempt_changed")
    for name, inode in value["files"].items():
        _require(name in {"python.tar.gz", "git.tar.gz", "tmux.tar.gz", "jq.bin"} and type(inode) is list and len(inode) == 2
                 and all(type(part) is int and part > 0 for part in inode), "attempt_changed")
    return value


def status(value):
    validate_request("status", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    profile = _profile(identity); base = _base(identity)
    if profile.root.exists() or profile.root.is_symlink():
        receipt = _verify_profile(profile)
        return {"state": "ready", "profile_id": PROFILE_ID, "policy_sha256": _policy(), "request_id": value["request_id"],
                "completion_request_id": receipt["request_id"], "never_replay": True}
    if not base.exists():
        return {"state": "absent", "request_id": value["request_id"], "never_replay": True}
    _directory(base, identity.uid, 0o700)
    if (base / "attempt.json").exists() or (base / "attempt.json").is_symlink():
        attempt = _attempt(base, identity)
        return {"state": "unknown", "request_id": attempt["request_id"], "phase": attempt["phase"], "never_replay": True}
    return {"state": "absent", "request_id": value["request_id"], "never_replay": True}


def _lock(base, uid):
    path = base / "transition.lock"
    inherited = os.fstat(_INHERITED_LOCK_FD) if _INHERITED_LOCK_FD is not None else None
    if inherited is not None:
        actual = path.lstat()
        _require((inherited.st_dev, inherited.st_ino) == (actual.st_dev, actual.st_ino), "lock_identity_changed")
        fd = os.dup(_INHERITED_LOCK_FD)
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 0, "unsafe_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd); raise


def _stage(base, attempt, uid):
    stage = base / ("stage-" + attempt["request_id"])
    info = _directory(stage, uid, 0o700)
    _require((info.st_dev, info.st_ino) == (attempt["stage_device"], attempt["stage_inode"]), "stage_identity_changed")
    return stage


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _require(urllib.parse.urlsplit(newurl).scheme == "https", "insecure_artifact_redirect")
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(kind, stage, attempt, base, identity, deadline):
    descriptor = _DESCRIPTOR["artifacts"][kind]
    path = stage / (kind + (".bin" if kind == "jq" else ".tar.gz"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    attempt["files"][path.name] = [info.st_dev, info.st_ino]
    _save(base / "attempt.json", attempt, identity.uid)
    try:
        context = ssl.create_default_context(cafile=stage / "profile/python" / CA_MEMBER)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _HTTPSRedirect())
        remaining = deadline - time.monotonic(); _require(remaining > 0, "preparation_deadline")
        with os.fdopen(fd, "wb") as output, opener.open(descriptor["url"], timeout=min(30, remaining)) as response:
            total = 0; digest = hashlib.sha256()
            while True:
                _require(time.monotonic() < deadline, "preparation_deadline")
                chunk = response.read1(min(65536, descriptor["bytes"] + 1 - total))
                if not chunk:
                    break
                total += len(chunk); _require(total <= descriptor["bytes"], "artifact_oversize")
                output.write(chunk); digest.update(chunk)
            output.flush(); os.fsync(output.fileno())
        _require(total == descriptor["bytes"] and digest.hexdigest() == descriptor["sha256"], "artifact_mismatch")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return path


def _publish(stage, destination):
    # Darwin renamex_np(RENAME_EXCL): never replace even an empty foreign dir.
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(stage), os.fsencode(destination), 0x4) != 0:
        raise OSError(ctypes.get_errno(), "exclusive runtime publication failed")


def prepare(value, *, bootstrap=False):
    validate_request("prepare", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    profile = _profile(identity); base = _base(identity)
    if profile.root.exists() or profile.root.is_symlink():
        return status(value)
    _require(bootstrap, "bootstrap_entry_required")
    _parents(base, identity.uid)
    _directory(base, identity.uid, 0o700)
    lock = _lock(base, identity.uid)
    try:
        attempt = _attempt(base, identity)
        _require(attempt["request_id"] == value["request_id"] and attempt["phase"] == "bootstrap", "original_attempt_required")
        stage = _stage(base, attempt, identity.uid); work = stage / "profile"
        _directory(work, identity.uid, 0o700)
        _require(_tree(work / "python", identity.uid, staged=True) == _DESCRIPTOR["artifacts"]["python"]["tree_sha256"], "bootstrap_python_changed")
        admitted = True
        attempt["phase"] = "preparing"; _save(base / "attempt.json", attempt, identity.uid)
        deadline = time.monotonic() + BUDGET
        for kind in ("git", "tmux", "jq"):
            archive = _download(kind, stage, attempt, base, identity, deadline)
            if kind == "jq":
                destination = work / "jq"; _directory(destination, identity.uid, 0o755, create=True)
                _directory(destination / "bin", identity.uid, 0o755, create=True)
                raw = _read(archive, identity.uid, _DESCRIPTOR["artifacts"][kind]["bytes"])
                _require(_sha(raw) == _DESCRIPTOR["artifacts"][kind]["sha256"], "artifact_mismatch")
                fd = os.open(destination / "bin/jq", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
                os.fchmod(fd, 0o755)
                with os.fdopen(fd, "wb") as out:
                    out.write(raw); out.flush(); os.fsync(out.fileno())
            else:
                _extract(archive, work / kind, kind, _DESCRIPTOR["artifacts"][kind], identity.uid)
        _directory(work / "bin", identity.uid, 0o755, create=True)
        for name, target in ALIASES.items():
            (work / "bin" / name).symlink_to(target)
        ca = _read(work / "python" / CA_MEMBER, identity.uid, 1024 * 1024, 0o644)
        _require(_sha(ca) == _DESCRIPTOR["ca_sha256"], "runtime_ca_changed")
        fd = os.open(work / "ca-bundle.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as out:
            out.write(ca); out.flush(); os.fsync(out.fileno())
        receipt = {"schema_version": 1, "binding": _binding(identity), "request_id": value["request_id"], "state": "ready"}
        _save(work / "receipt.json", receipt, identity.uid, new=True)
        staged_profile = RuntimeProfile(profile.uid, profile.account, profile.home, work, work / "bin", work / "python/bin/python3.12",
                                        work / "git/bin/git", work / "git/libexec/git-core", work / "tmux/bin/tmux", work / "jq/bin/jq",
                                        work / "ca-bundle.pem", _policy())
        _seal_profile(staged_profile)
        _require(time.monotonic() < deadline, "preparation_deadline")
        _publish(work, profile.root); _sync(base)
        attempt["phase"] = "ready"; _save(base / "attempt.json", attempt, identity.uid)
        return status(value)
    except Exception:
        if locals().get("admitted") and attempt.get("phase") != "ready":
            attempt["phase"] = "failed"; _save(base / "attempt.json", attempt, identity.uid)
        raise
    finally:
        os.close(lock)  # Never LOCK_UN: inherited holders share one description.


def _owned_partial(component, archive_path, kind, descriptor, uid):
    _require(_sha(_read(archive_path, uid, descriptor["bytes"])) == descriptor["sha256"], "unknown_stage_content")
    _require(stat.S_IMODE(_directory(component, uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
    with tarfile.open(archive_path, "r:gz") as archive:
        expected = {}
        for member in _inventory(archive, descriptor):
            name = _selection(member, kind)
            if not name:
                continue
            for parent in PurePosixPath(name).parents:
                if str(parent) != ".":
                    expected.setdefault(str(parent), ("directory", 0o755, None))
            expected[name] = ("directory", 0o755, None) if member.isdir() else ("symlink", 0, member.linkname) if member.issym() else ("file", 0o755 if member.mode & 0o111 else 0o644, member)
        for directory, directories, files in os.walk(component, followlinks=False):
            for name in directories + files:
                path = Path(directory) / name; relative = path.relative_to(component).as_posix(); info = path.lstat()
                _require(relative in expected and info.st_uid == uid, "unknown_stage_content")
                kind_expected, mode, value = expected[relative]
                if stat.S_ISLNK(info.st_mode):
                    _require(kind_expected == "symlink" and info.st_nlink == 1 and os.readlink(path) == value, "unknown_stage_content")
                    if name in directories:
                        directories.remove(name)
                elif stat.S_ISDIR(info.st_mode):
                    _require(kind_expected == "directory" and stat.S_IMODE(info.st_mode) in (mode, mode & ~0o222), "unknown_stage_content")
                else:
                    _require(kind_expected == "file" and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                             and stat.S_IMODE(info.st_mode) in (mode, mode & ~0o222) and info.st_size <= value.size, "unknown_stage_content")
                    # An interrupted known write may be a prefix, never foreign data.
                    actual = _read(path, uid, value.size, stat.S_IMODE(info.st_mode))
                    _require(archive.extractfile(value).read(len(actual)) == actual, "unknown_stage_content")


def discard(value):
    validate_request("discard", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    base = _base(identity); _parents(base, identity.uid); _directory(base, identity.uid, 0o700)
    lock = _lock(base, identity.uid)
    try:
        attempt = _attempt(base, identity)
        _require(attempt["request_id"] == value["request_id"], "original_attempt_required")
        profile = _profile(identity)
        if profile.root.exists() or profile.root.is_symlink():
            _verify_profile(profile)
        stage = _stage(base, attempt, identity.uid)
        allowed = set(attempt["files"]) | {"profile"}
        _require({p.name for p in stage.iterdir()} <= allowed, "unknown_stage_content")
        for name, expected in attempt["files"].items():
            _require(name in {"python.tar.gz", "git.tar.gz", "tmux.tar.gz", "jq.bin"}, "attempt_changed")
            path = stage / name
            if path.exists() or path.is_symlink():
                info = path.lstat(); kind = name.split(".", 1)[0]
                _require([info.st_dev, info.st_ino] == expected and stat.S_ISREG(info.st_mode) and info.st_uid == identity.uid
                         and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                         and info.st_size <= _DESCRIPTOR["artifacts"][kind]["bytes"], "unknown_stage_content")
        work = stage / "profile"
        if work.exists() or work.is_symlink():
            _directory(work, identity.uid, 0o700)
            _require({p.name for p in work.iterdir()} <= set(_DESCRIPTOR["artifacts"]) | {"bin", "ca-bundle.pem", "receipt.json"}, "unknown_stage_content")
            for kind in _DESCRIPTOR["artifacts"]:
                path = work / kind
                if not (path.exists() or path.is_symlink()):
                    continue
                if kind == "jq":
                    _require(stat.S_IMODE(_directory(path, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                    _require({p.name for p in path.iterdir()} <= {"bin"}, "unknown_stage_content")
                    binary_dir = path / "bin"
                    if binary_dir.exists() or binary_dir.is_symlink():
                        _require(stat.S_IMODE(_directory(binary_dir, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                        _require({p.name for p in binary_dir.iterdir()} <= {"jq"}, "unknown_stage_content")
                        binary = binary_dir / "jq"
                        if binary.exists() or binary.is_symlink():
                            expected_raw = _read(stage / "jq.bin", identity.uid, _DESCRIPTOR["artifacts"][kind]["bytes"])
                            mode = stat.S_IMODE(binary.lstat().st_mode)
                            _require(mode in (0o755, 0o555) and _sha(expected_raw) == _DESCRIPTOR["artifacts"][kind]["sha256"]
                                     and expected_raw.startswith(_read(binary, identity.uid, len(expected_raw), mode)), "unknown_stage_content")
                else:
                    _owned_partial(path, stage / (kind + ".tar.gz"), kind, _DESCRIPTOR["artifacts"][kind], identity.uid)
            aliases = work / "bin"
            if aliases.exists() or aliases.is_symlink():
                _require(stat.S_IMODE(_directory(aliases, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                for path in aliases.iterdir():
                    info = path.lstat()
                    _require(path.name in ALIASES and stat.S_ISLNK(info.st_mode) and info.st_uid == identity.uid
                             and info.st_nlink == 1 and os.readlink(path) == ALIASES[path.name], "unknown_stage_content")
            ca = work / "ca-bundle.pem"
            if ca.exists() or ca.is_symlink():
                source_ca = work / "python" / CA_MEMBER
                source_mode = stat.S_IMODE(source_ca.lstat().st_mode); mode = stat.S_IMODE(ca.lstat().st_mode)
                _require(source_mode in (0o644, 0o444) and mode in (0o644, 0o444), "unknown_stage_content")
                expected_ca = _read(source_ca, identity.uid, 1024 * 1024, source_mode)
                _require(_sha(expected_ca) == _DESCRIPTOR["ca_sha256"] and expected_ca.startswith(_read(ca, identity.uid, len(expected_ca), mode)), "unknown_stage_content")
            receipt = work / "receipt.json"
            if receipt.exists() or receipt.is_symlink():
                expected_receipt = _canonical({"schema_version": 1, "binding": _binding(identity), "request_id": value["request_id"], "state": "ready"})
                mode = stat.S_IMODE(receipt.lstat().st_mode)
                _require(mode in (0o600, 0o444) and expected_receipt.startswith(_read(receipt, identity.uid, mode=mode)), "unknown_stage_content")
        # Lock acquisition proves no surviving admitted descendant owns the stage.
        # Only exact inode-bound known content was admitted above; no generic reset.
        if work.exists():
            # All names, inodes, bytes and modes were admitted above. Restore
            # directory write permission only in this owned disposable stage.
            for directory, directories, files in os.walk(work, followlinks=False):
                os.chmod(directory, 0o700 if Path(directory) == work else 0o755, follow_symlinks=False)
            # Keep the running interpreter until all other work is removed.
            # Interruption during its final removal still requires preservation.
            for path in work.iterdir():
                if path.name != "python":
                    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
            if (work / "python").exists():
                shutil.rmtree(work / "python")
            work.rmdir()
        for path in stage.iterdir():
            path.unlink()
        stage.rmdir(); (base / "attempt.json").unlink(); _sync(base)
        return {"state": "discarded", "request_id": value["request_id"], "installed_profile_unchanged": True}
    finally:
        os.close(lock)


def main():
    global _INHERITED_LOCK_FD
    try:
        _require(len(sys.argv) in (3, 4), "invalid_request")
        operation = sys.argv[1]; raw = sys.argv[2].encode("ascii")
        _require(len(raw) <= 2048, "invalid_request")
        value = json.loads(raw, object_pairs_hook=_unique)
        validate_request(operation, value)
        _require(raw == _canonical(value), "invalid_request")
        if operation != "status":
            _INHERITED_LOCK_FD = 9
        result = status(value) if operation == "status" else discard(value) if operation == "discard" else prepare(value, bootstrap=len(sys.argv) == 4 and sys.argv[3] == "bootstrap")
        sys.stdout.write(_canonical({"schema_version": 1, "result": result}).decode("ascii") + "\n")
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, RuntimeRefusal) else "runtime_unavailable"
        sys.stdout.write(_canonical({"schema_version": 1, "state": "unavailable", "code": code}).decode("ascii") + "\n")
        return 6


if __name__ == "__main__":
    sys.exit(main())
# END CANONICAL DARWIN RUNTIME PYTHON
CANONICAL_DARWIN_RUNTIME
