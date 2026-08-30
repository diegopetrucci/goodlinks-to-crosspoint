#!/usr/bin/env bash

# This wrapper deliberately does not source .sync.env.  The local file may
# contain only the one non-secret setting parsed below; the token remains an
# inline environment value for the Python child process.
set +x
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || {
    printf '%s\n' 'sync.sh: unable to locate the repository root.' >&2
    exit 1
}
readonly ROOT_DIR
readonly VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
readonly SOURCE_DIR="$ROOT_DIR/src"
readonly SYNC_ENV="$ROOT_DIR/.sync.env"
readonly PASS_ENTRY='goodlinks-crosspoint/goodlinks-token'
readonly CROSSPOINT_HOST='crosspoint.local'

# An inherited token is never used by this wrapper or passed to helper tools.
unset GOODLINKS_TOKEN

fail() {
    printf 'sync.sh: %s\n' "$1" >&2
    exit 1
}

if [[ ! -x "$VENV_PYTHON" ]]; then
    fail 'the repository virtual environment is missing; create .venv and install the project first.'
fi
if [[ ! -f "$SOURCE_DIR/goodlinks_crosspoint/__main__.py" ]]; then
    fail 'the repository Python source tree is missing.'
fi

if [[ ! -e "$SYNC_ENV" ]]; then
    fail 'missing .sync.env; copy .sync.env.example to .sync.env and set GOODLINKS_TAG.'
fi
if [[ -L "$SYNC_ENV" || ! -f "$SYNC_ENV" || ! -r "$SYNC_ENV" ]]; then
    fail '.sync.env must be a readable regular file, not a symlink.'
fi

# Parse a deliberately narrow grammar instead of evaluating shell syntax.
# Values remain data: spaces, Unicode, and shell punctuation are safe when
# passed as the quoted --tag argument below.
GOODLINKS_TAG=''
parse_sync_env() {
    local line value
    local tag_seen=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        case "$line" in
            GOODLINKS_TAG=*)
                if (( tag_seen )); then
                    fail '.sync.env contains duplicate GOODLINKS_TAG settings.'
                fi
                value=${line#GOODLINKS_TAG=}
                if [[ -z "$value" || "$value" =~ ^[[:space:]]*$ ]]; then
                    fail '.sync.env contains a blank GOODLINKS_TAG.'
                fi
                if [[ "$value" =~ ^[[:space:]] || "$value" =~ [[:space:]]$ ]]; then
                    fail '.sync.env contains a GOODLINKS_TAG with surrounding whitespace.'
                fi
                if [[ "$value" =~ [[:cntrl:]] || "$value" == *$'\r'* ]]; then
                    fail '.sync.env contains a GOODLINKS_TAG with control characters.'
                fi
                if [[ "$value" == -* ]]; then
                    fail '.sync.env contains a GOODLINKS_TAG that cannot be passed safely.'
                fi
                GOODLINKS_TAG="$value"
                tag_seen=1
                ;;
            *)
                fail '.sync.env contains an unsupported setting; only GOODLINKS_TAG is allowed.'
                ;;
        esac
    done < "$SYNC_ENV"

    if (( ! tag_seen )); then
        fail '.sync.env must define GOODLINKS_TAG.'
    fi
}
parse_sync_env

if [[ "$(uname -s 2>/dev/null || true)" != 'Darwin' ]]; then
    fail 'CrossPoint sync is supported on macOS only.'
fi

if ! PASS_BIN=$(command -v pass); then
    fail 'the pass executable is missing; install pass and create the documented token entry.'
fi
if ! DSCACHEUTIL_BIN=$(command -v dscacheutil); then
    fail 'the macOS dscacheutil executable is missing; device resolution cannot continue.'
fi

is_private_ipv4() {
    local candidate=$1
    local first second third fourth octet

    if [[ ! "$candidate" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
        return 1
    fi
    IFS=. read -r first second third fourth <<< "$candidate"
    for octet in "$first" "$second" "$third" "$fourth"; do
        if [[ ! "$octet" =~ ^[0-9]{1,3}$ ]]; then
            return 1
        fi
        # Canonical decimal spelling avoids ambiguous zero-padded addresses.
        if [[ "$octet" != 0 && "$octet" == 0* ]]; then
            return 1
        fi
        if (( 10#$octet > 255 )); then
            return 1
        fi
    done

    if (( first == 10 )); then
        return 0
    fi
    if (( first == 172 && second >= 16 && second <= 31 )); then
        return 0
    fi
    if (( first == 192 && second == 168 )); then
        return 0
    fi
    return 1
}

resolve_device() {
    local candidate line resolver_output
    device_address=''

    if ! resolver_output=$("$DSCACHEUTIL_BIN" -q host -a name "$CROSSPOINT_HOST" 2>/dev/null); then
        fail 'unable to resolve CrossPoint with macOS dscacheutil.'
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            'ip_address: '*)
                candidate=${line#ip_address: }
                if is_private_ipv4 "$candidate"; then
                    # Select the first usable private answer and never include
                    # any resolver value in a diagnostic.
                    device_address="$candidate"
                    break
                fi
                ;;
        esac
    done <<< "$resolver_output"

    if [[ -z "$device_address" ]]; then
        fail 'macOS did not return a usable private CrossPoint IPv4 address.'
    fi
    DEVICE_URL="http://$device_address"
}
resolve_device

run_sync() {
    local token
    if ! token=$("$PASS_BIN" show "$PASS_ENTRY" 2>/dev/null); then
        fail 'unable to read the GoodLinks token from pass entry goodlinks-crosspoint/goodlinks-token.'
    fi
    # A pass entry is expected to contain one token line.  Reject malformed
    # multi-line output without displaying any part of the credential.
    if [[ -z "$token" || "$token" == *$'\n'* || "$token" == *$'\r'* ]]; then
        fail 'the GoodLinks pass entry is empty or does not contain one token.'
    fi

    # Keep the credential out of this shell environment and out of argv.  The
    # fixed options make output, tag, and resolved device repository-owned;
    # all caller-supplied optional CLI flags are forwarded before them.
    GOODLINKS_TOKEN="$token" \
        PYTHONPATH="$SOURCE_DIR" \
        "$VENV_PYTHON" -m goodlinks_crosspoint sync \
        "$@" \
        --tag "$GOODLINKS_TAG" \
        --output-dir "$ROOT_DIR/export" \
        --device-url "$DEVICE_URL"
}
run_sync "$@"
