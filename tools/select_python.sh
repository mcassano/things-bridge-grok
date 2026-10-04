#!/bin/bash
# Select one interpreter for installer work and the compiled FDA wrapper.
# Sourced by the installers and tools/test.sh; defines functions only.

_things_python_path() {
    local candidate="$1"
    [[ -n "$candidate" ]] || return 1
    if [[ "$candidate" == */* ]]; then
        printf '%s\n' "$candidate"
    else
        command -v "$candidate" 2>/dev/null
    fi
}

_things_python_is_supported() {
    local candidate="$1"
    [[ -x "$candidate" ]] &&
        "$candidate" -c 'import os, sys; raise SystemExit(sys.version_info < (3, 9) or os.open not in os.supports_dir_fd)' 2>/dev/null
}

# A hardened install bakes the interpreter path into a root-owned wrapper, so
# the interpreter and every parent directory must be root-owned and not
# group/world-writable; otherwise a same-user process could swap it.
hardened_python_is_trusted() {
    local current="$1"
    local mode
    local owner

    [[ "$current" == /* && -f "$current" && ! -L "$current" ]] || return 1
    while :; do
        [[ ! -L "$current" ]] || return 1
        owner="$(/usr/bin/stat -f '%u' "$current" 2>/dev/null)" || return 1
        mode="$(/usr/bin/stat -f '%Lp' "$current" 2>/dev/null)" || return 1
        [[ "$owner" == "0" && -n "$mode" && "$mode" != *[!0-7]* ]] || return 1
        (( (8#$mode & 0022) == 0 )) || return 1
        [[ "$current" == "/" ]] && break
        current="${current%/*}"
        [[ -n "$current" ]] || current="/"
    done
}

# Usage: find_supported_python <require_trusted: 0|1>
# Honors THINGS_PYTHON (must be absolute) and fails closed when it is set but
# unsupported.
find_supported_python() {
    local require_trusted="${1:-0}"
    local candidate
    local resolved

    if [[ "${THINGS_PYTHON+x}" == "x" ]]; then
        [[ "$THINGS_PYTHON" == /* ]] || return 1
        resolved="$(_things_python_path "$THINGS_PYTHON")" || return 1
        if [[ "$require_trusted" == "1" ]]; then
            hardened_python_is_trusted "$resolved" || return 1
        fi
        _things_python_is_supported "$resolved" || return 1
        printf '%s\n' "$resolved"
        return 0
    fi

    for candidate in /usr/bin/python3 \
        python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        resolved="$(_things_python_path "$candidate")" || continue
        if [[ "$require_trusted" == "1" ]] &&
            ! hardened_python_is_trusted "$resolved"; then
            continue
        fi
        if _things_python_is_supported "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}
