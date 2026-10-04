#!/bin/bash
# install-hardened.sh — root-owned code, user-owned runtime state.
#
# Installs the worker, write gate, and native binaries under a root-owned
# per-user code root, compiles the wrapper to require root ownership of all
# code it loads, and enforces a root-owned, default-deny read allowlist.
# Requests, responses, logs, and nonces stay in a private user-owned bridge.
# sudo is used only for the root-owned tree.

set -euo pipefail
ORIGINAL_PATH="$PATH"
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

if [[ "$EUID" -eq 0 ]]; then
    echo "Error: run this script as your normal user; it invokes sudo narrowly." >&2
    exit 1
fi
if [[ "$(uname)" != "Darwin" ]]; then
    echo "Error: the hardened installer only runs on macOS." >&2
    exit 1
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PRODUCT_ROOT="/Library/Application Support/GrokBotThings"
USER_ROOT="$PRODUCT_ROOT/users/$UID"
CODE_ROOT="$USER_ROOT/libexec"
CONFIG_ROOT="$USER_ROOT/config"
BRIDGE_ROOT="${GROKBOT_THINGS_BRIDGE:-$HOME/Library/Application Support/GrokBotThings}"
LABEL="com.mcassano.grokbot-things"
PLIST_TEMPLATE="$SOURCE_ROOT/$LABEL.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_SELECTOR="$SOURCE_ROOT/tools/select_python.sh"
ALLOWLIST="$CONFIG_ROOT/allowed_scopes.txt"
CURRENT_USER="$(id -un)"
BUILD_DIR="$(mktemp -d -t grokbot-things-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

if [[ ! -f "$PYTHON_SELECTOR" || -L "$PYTHON_SELECTOR" ]]; then
    echo "Error: missing regular Python selector: $PYTHON_SELECTOR" >&2
    exit 1
fi
# shellcheck source=tools/select_python.sh
source "$PYTHON_SELECTOR"

require_safe_runtime_entry() {
    local path="$1"
    local kind="$2"
    if [[ -L "$path" ]]; then
        echo "Error: refusing symlinked runtime path: $path" >&2
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "directory" && ! -d "$path" ]]; then
        echo "Error: expected a runtime directory: $path" >&2
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "file" && ! -f "$path" ]]; then
        echo "Error: expected a regular runtime file: $path" >&2
        exit 1
    fi
}

for cmd in clang codesign launchctl sudo; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Error: required command not found: $cmd" >&2
        exit 1
    fi
done
if ! xcode-select -p >/dev/null 2>&1; then
    echo "Error: install Xcode Command Line Tools with xcode-select --install" >&2
    exit 1
fi
if ! PYTHON3_PATH="$(find_supported_python 1)"; then
    echo "Error: hardened mode requires a trusted Python 3.9 or newer with dir_fd" >&2
    echo "support whose file and parent directories are root-owned and protected." >&2
    echo "THINGS_PYTHON, when set, must be an absolute trusted path." >&2
    exit 1
fi

for path in \
    "$SOURCE_ROOT/bin/helper.py" \
    "$SOURCE_ROOT/bin/write_gate.py" \
    "$SOURCE_ROOT/bin/things_helper.c" \
    "$SOURCE_ROOT/bin/confirm_things_change.m" \
    "$SOURCE_ROOT/tools/doctor.py" \
    "$SOURCE_ROOT/tools/configure_allowlist.py" \
    "$SOURCE_ROOT/policy/blocked_scopes.txt.template" \
    "$SOURCE_ROOT/policy/allowed_scopes.txt.template" \
    "$SOURCE_ROOT/install-skill.sh" \
    "$PLIST_TEMPLATE"; do
    if [[ ! -f "$path" ]]; then
        echo "Error: missing source file: $path" >&2
        exit 1
    fi
done

# ---- user-owned runtime bridge ------------------------------------------------
for path in "$BRIDGE_ROOT" "$BRIDGE_ROOT/control" "$BRIDGE_ROOT/control/requests" \
    "$BRIDGE_ROOT/control/responses" "$BRIDGE_ROOT/policy"; do
    require_safe_runtime_entry "$path" directory
done
for path in "$BRIDGE_ROOT/control/log.txt" "$BRIDGE_ROOT/policy/blocked_scopes.txt" \
    "$BRIDGE_ROOT/policy/read_policy.txt" "$BRIDGE_ROOT/policy/write_policy.txt"; do
    require_safe_runtime_entry "$path" file
done
mkdir -p "$BRIDGE_ROOT/control/requests" "$BRIDGE_ROOT/control/responses" "$BRIDGE_ROOT/policy"
BRIDGE_ROOT="$(cd "$BRIDGE_ROOT" && pwd -P)"
touch "$BRIDGE_ROOT/control/log.txt"
chmod 700 "$BRIDGE_ROOT" "$BRIDGE_ROOT/control" "$BRIDGE_ROOT/control/requests" \
    "$BRIDGE_ROOT/control/responses" "$BRIDGE_ROOT/policy"
chmod 600 "$BRIDGE_ROOT/control/log.txt"
if [[ ! -f "$BRIDGE_ROOT/policy/blocked_scopes.txt" ]]; then
    cp "$SOURCE_ROOT/policy/blocked_scopes.txt.template" "$BRIDGE_ROOT/policy/blocked_scopes.txt"
fi
if [[ ! -f "$BRIDGE_ROOT/policy/write_policy.txt" ]]; then
    printf 'confirm\n' > "$BRIDGE_ROOT/policy/write_policy.txt"
fi
# Informational only: the hardened wrapper bakes allowlist mode in.
printf 'allowlist\n' > "$BRIDGE_ROOT/policy/read_policy.txt"
chmod 600 "$BRIDGE_ROOT/policy/blocked_scopes.txt" "$BRIDGE_ROOT/policy/read_policy.txt" \
    "$BRIDGE_ROOT/policy/write_policy.txt"

# ---- root-owned code and allowlist ---------------------------------------------
echo "Requesting administrator access for the root-owned code and policy..."
sudo -v
sudo /usr/bin/install -d -o root -g wheel -m 755 \
    "$PRODUCT_ROOT" "$PRODUCT_ROOT/users" "$USER_ROOT" "$CODE_ROOT" \
    "$CODE_ROOT/bin" "$CODE_ROOT/tools" "$CONFIG_ROOT"
if [[ -L "$ALLOWLIST" ]]; then
    echo "Error: hardened allowlist must not be a symlink: $ALLOWLIST" >&2
    exit 1
fi
if [[ ! -e "$ALLOWLIST" ]]; then
    sudo /usr/bin/install -o root -g wheel -m 600 \
        "$SOURCE_ROOT/policy/allowed_scopes.txt.template" "$ALLOWLIST"
fi
if ! "$PYTHON3_PATH" - "$ALLOWLIST" <<'PYCHECK'; then
import os
import stat
import sys

metadata = os.lstat(sys.argv[1])
valid = stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
valid = valid and not metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
raise SystemExit(0 if valid else 1)
PYCHECK
    echo "Error: existing hardened allowlist is not a protected root-owned file." >&2
    exit 1
fi
# Mode 600 root:wheel plus a read-only ACL for this user: the worker can read
# the list, but changing it requires sudo (configure_allowlist.py).
if ! sudo /bin/chmod -N "$ALLOWLIST" 2>/dev/null; then
    echo "  no existing ACL to clear"
fi
sudo /bin/chmod +a "user:$CURRENT_USER allow read" "$ALLOWLIST"

clang -Wall -Wextra -Werror -O2 -fobjc-arc \
    -framework AppKit -framework Foundation \
    -o "$BUILD_DIR/grokbot-things-confirm" "$SOURCE_ROOT/bin/confirm_things_change.m"

clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$CODE_ROOT/bin/helper.py\"" \
    -DWRITE_GATE_SCRIPT="\"$CODE_ROOT/bin/write_gate.py\"" \
    -DCONFIRM_HELPER="\"$CODE_ROOT/bin/grokbot-things-confirm\"" \
    -DBRIDGE_ROOT="\"$BRIDGE_ROOT\"" \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -DEXPECTED_CODE_UID=0 \
    -DREAD_POLICY_MODE='"allowlist"' \
    -DREAD_ALLOWLIST_PATH="\"$ALLOWLIST\"" \
    -DREQUIRE_ROOT_POLICY=1 \
    -o "$BUILD_DIR/grokbot-things-helper" \
    "$SOURCE_ROOT/bin/things_helper.c"

CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
SIGN_ARGS=(--force --sign "$CODESIGN_IDENTITY" --options runtime)
if [[ "$CODESIGN_IDENTITY" != "-" ]]; then
    SIGN_ARGS+=(--timestamp)
fi
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/grokbot-things-helper"
codesign "${SIGN_ARGS[@]}" "$BUILD_DIR/grokbot-things-confirm"

sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/helper.py" "$CODE_ROOT/bin/helper.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/write_gate.py" "$CODE_ROOT/bin/write_gate.py"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/things_helper.c" "$CODE_ROOT/bin/things_helper.c"
sudo /usr/bin/install -o root -g wheel -m 444 \
    "$SOURCE_ROOT/bin/confirm_things_change.m" "$CODE_ROOT/bin/confirm_things_change.m"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/grokbot-things-helper" "$CODE_ROOT/bin/grokbot-things-helper"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$BUILD_DIR/grokbot-things-confirm" "$CODE_ROOT/bin/grokbot-things-confirm"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/doctor.py" "$CODE_ROOT/tools/doctor.py"
sudo /usr/bin/install -o root -g wheel -m 555 \
    "$SOURCE_ROOT/tools/configure_allowlist.py" "$CODE_ROOT/tools/configure_allowlist.py"

# ---- launchd agent --------------------------------------------------------------
mkdir -p "$(dirname "$PLIST_DEST")"
"$PYTHON3_PATH" - "$CODE_ROOT" "$BRIDGE_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
import sys
import xml.etree.ElementTree as ET

code_root, bridge_root, destination, template = sys.argv[1:]
tree = ET.parse(template)
for element in tree.getroot().iter("string"):
    if element.text:
        element.text = element.text.replace("{{CODE_ROOT}}", code_root)
        element.text = element.text.replace("{{BRIDGE_ROOT}}", bridge_root)
tree.write(destination, encoding="UTF-8", xml_declaration=True)
PYGEN
chmod 644 "$PLIST_DEST"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
fi
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/$LABEL"

if [[ "${INSTALL_GROK_SKILL:-1}" == "1" ]]; then
    PATH="$ORIGINAL_PATH" "$SOURCE_ROOT/install-skill.sh"
fi

cat <<EOF

Hardened install complete.

Trusted code (root-owned):   $CODE_ROOT
Runtime bridge (user-owned): $BRIDGE_ROOT
Read policy:                 root-owned allowlist (default-deny)

Nothing is readable until you allow at least one scope, for example:
  "$PYTHON3_PATH" "$CODE_ROOT/tools/configure_allowlist.py" add "area:Work"
  "$PYTHON3_PATH" "$CODE_ROOT/tools/configure_allowlist.py" add unfiled

Grant Full Disk Access (System Settings -> Privacy & Security) to:
  $CODE_ROOT/bin/grokbot-things-helper

Tell Grok this bridge folder:
  $BRIDGE_ROOT

Then verify:
  "$PYTHON3_PATH" "$CODE_ROOT/tools/doctor.py" --bridge "$BRIDGE_ROOT" --code-root "$CODE_ROOT"
EOF
