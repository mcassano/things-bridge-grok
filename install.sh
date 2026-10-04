#!/bin/bash
# install.sh — standard per-user setup for the Grok Bot Things helper.
#
# What this does, in order:
#   1. Checks for macOS, the Xcode Command Line Tools (clang + codesign), a
#      supported Python, and Things 3.
#   2. Creates the private bridge directories (control/, policy/) and default
#      policy files without overwriting existing ones.
#   3. Locks down the Python worker and write-gate module.
#   4. Compiles the Full Disk Access wrapper and the native confirmation
#      dialog, then ad hoc code-signs both with the hardened runtime.
#   5. Fills in the launchd plist template, installs it under
#      ~/Library/LaunchAgents/, and bootstraps it.
#   6. Optionally installs SKILL.md into Grok's user-level skill directory.
#   7. Prints the one manual step: grant Full Disk Access to the wrapper.
#
# Safe to re-run. It does not overwrite policy files or touch existing grants.

set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
    printf "\033[31mError: do not run this installer as root or with sudo.\033[0m\n" 1>&2
    printf "This is a per-user LaunchAgent. Run as your normal user:\n  ./install.sh\n" 1>&2
    exit 1
fi

INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BIN_DIR="$INSTALL_ROOT/bin"
CONTROL_DIR="$INSTALL_ROOT/control"
POLICY_DIR="$INSTALL_ROOT/policy"
HELPER_PY="$BIN_DIR/helper.py"
WRITE_GATE_PY="$BIN_DIR/write_gate.py"
WRAPPER_SRC="$BIN_DIR/things_helper.c"
WRAPPER_BIN="$BIN_DIR/grokbot-things-helper"
CONFIRM_SRC="$BIN_DIR/confirm_things_change.m"
CONFIRM_BIN="$BIN_DIR/grokbot-things-confirm"
LABEL="com.mcassano.grokbot-things"
PLIST_TEMPLATE="$INSTALL_ROOT/$LABEL.plist.template"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_SELECTOR="$INSTALL_ROOT/tools/select_python.sh"
INSTALL_GROK_SKILL="${INSTALL_GROK_SKILL:-1}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*" 1>&2; }

if [[ ! -f "$PYTHON_SELECTOR" || -L "$PYTHON_SELECTOR" ]]; then
    red "Missing regular Python selector: $PYTHON_SELECTOR"
    exit 1
fi
# shellcheck source=tools/select_python.sh
source "$PYTHON_SELECTOR"

require_safe_runtime_entry() {
    local path="$1"
    local kind="$2"
    if [[ -L "$path" ]]; then
        red "Refusing symlinked runtime path: $path"
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "directory" && ! -d "$path" ]]; then
        red "Expected a runtime directory: $path"
        exit 1
    fi
    if [[ -e "$path" && "$kind" == "file" && ! -f "$path" ]]; then
        red "Expected a regular runtime file: $path"
        exit 1
    fi
}

install_policy_file() {
    local destination="$1"
    local source_template="$2"
    if [[ ! -f "$destination" ]]; then
        cp "$source_template" "$destination"
        chmod 600 "$destination"
        green "  created $destination"
    fi
}

if [[ "$INSTALL_GROK_SKILL" != "0" && "$INSTALL_GROK_SKILL" != "1" ]]; then
    red "INSTALL_GROK_SKILL must be 0 or 1."
    exit 1
fi

# ---- 1. sanity checks -------------------------------------------------------
if [[ "$(uname)" != "Darwin" ]]; then
    red "This installer only runs on macOS."
    exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
    red "Xcode Command Line Tools are required to build the wrapper."
    red "Install them with: xcode-select --install"
    exit 1
fi
for cmd in clang codesign launchctl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        red "Required command not found: $cmd"
        exit 1
    fi
done
if ! PYTHON3_PATH="$(find_supported_python 0)"; then
    red "Python 3.9 or newer with dir_fd support is required."
    red "If THINGS_PYTHON is set, it must be an absolute path to a supported interpreter."
    exit 1
fi
for path in "$HELPER_PY" "$WRITE_GATE_PY" "$WRAPPER_SRC" "$CONFIRM_SRC" "$PLIST_TEMPLATE" \
    "$POLICY_DIR/blocked_scopes.txt.template" "$POLICY_DIR/allowed_scopes.txt.template"; do
    if [[ ! -f "$path" ]]; then
        red "Missing $path"
        exit 1
    fi
done
if [[ "$INSTALL_GROK_SKILL" == "1" && \
      (! -f "$INSTALL_ROOT/SKILL.md" || ! -x "$INSTALL_ROOT/install-skill.sh") ]]; then
    red "Missing SKILL.md or executable install-skill.sh"
    exit 1
fi
if [[ ! -d "/Applications/Things3.app" && ! -d "$HOME/Applications/Things3.app" ]]; then
    yellow "Things 3 was not found in /Applications. The helper will install, but"
    yellow "requests will fail until Things 3 is installed and has been opened once."
fi

bold "Grok Bot Things helper installer"
echo "  install root : $INSTALL_ROOT"
echo "  python       : $PYTHON3_PATH"
echo "  wrapper bin  : $WRAPPER_BIN"
echo "  launchd plist: $PLIST_DEST"
echo

# ---- 2. runtime directories and policy ---------------------------------------
for path in "$CONTROL_DIR" "$CONTROL_DIR/requests" "$CONTROL_DIR/responses" "$POLICY_DIR"; do
    require_safe_runtime_entry "$path" directory
done
for path in "$CONTROL_DIR/log.txt" "$POLICY_DIR/blocked_scopes.txt" \
    "$POLICY_DIR/allowed_scopes.txt" "$POLICY_DIR/read_policy.txt" \
    "$POLICY_DIR/write_policy.txt"; do
    require_safe_runtime_entry "$path" file
done
mkdir -p "$CONTROL_DIR/requests" "$CONTROL_DIR/responses" "$POLICY_DIR"
touch "$CONTROL_DIR/log.txt"
chmod 700 "$INSTALL_ROOT" "$CONTROL_DIR" "$CONTROL_DIR/requests" \
    "$CONTROL_DIR/responses" "$POLICY_DIR"
chmod 600 "$CONTROL_DIR/log.txt"

install_policy_file "$POLICY_DIR/blocked_scopes.txt" "$POLICY_DIR/blocked_scopes.txt.template"
install_policy_file "$POLICY_DIR/allowed_scopes.txt" "$POLICY_DIR/allowed_scopes.txt.template"
if [[ ! -f "$POLICY_DIR/read_policy.txt" ]]; then
    printf 'blocklist\n' > "$POLICY_DIR/read_policy.txt"
    chmod 600 "$POLICY_DIR/read_policy.txt"
    green "  created $POLICY_DIR/read_policy.txt (blocklist)"
fi
if [[ ! -f "$POLICY_DIR/write_policy.txt" ]]; then
    printf 'confirm\n' > "$POLICY_DIR/write_policy.txt"
    chmod 600 "$POLICY_DIR/write_policy.txt"
    green "  created $POLICY_DIR/write_policy.txt (confirm)"
fi

# ---- 3. lock down Python code -------------------------------------------------
chmod 500 "$HELPER_PY"
chmod 400 "$WRITE_GATE_PY"
green "  locked helper.py (500) and write_gate.py (400)"

# ---- 4. build and sign native binaries ---------------------------------------
bold "Building native binaries..."
clang -Wall -Wextra -Werror -O2 \
    -DHELPER_SCRIPT="\"$HELPER_PY\"" \
    -DWRITE_GATE_SCRIPT="\"$WRITE_GATE_PY\"" \
    -DCONFIRM_HELPER="\"$CONFIRM_BIN\"" \
    -DBRIDGE_ROOT="\"$INSTALL_ROOT\"" \
    -DPYTHON_INTERPRETER="\"$PYTHON3_PATH\"" \
    -o "$WRAPPER_BIN" "$WRAPPER_SRC"
chmod 700 "$WRAPPER_BIN"
green "  built $WRAPPER_BIN"

clang -Wall -Wextra -Werror -O2 -fobjc-arc \
    -framework AppKit -framework Foundation \
    -o "$CONFIRM_BIN" "$CONFIRM_SRC"
chmod 700 "$CONFIRM_BIN"
green "  built $CONFIRM_BIN"

# The hardened runtime blocks DYLD_INSERT_LIBRARIES and similar injection, so
# another process cannot borrow the wrapper's Full Disk Access grant.
codesign --force --sign - --options runtime "$WRAPPER_BIN"
codesign --force --sign - --options runtime "$CONFIRM_BIN"
green "  ad hoc signed both binaries (hardened runtime)"
CDHASH="$(codesign -dvvv "$WRAPPER_BIN" 2>&1 | awk -F'=' '/CDHash=/{print $2; exit}')"
echo "  wrapper cdhash: ${CDHASH:-unknown}"

# ---- 5. launchd agent ---------------------------------------------------------
mkdir -p "$(dirname "$PLIST_DEST")"
# Generate the plist with real XML escaping rather than sed substitution.
"$PYTHON3_PATH" - "$INSTALL_ROOT" "$INSTALL_ROOT" "$PLIST_DEST" "$PLIST_TEMPLATE" <<'PYGEN'
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
green "  wrote $PLIST_DEST"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
    echo "  existing agent unloaded"
fi
launchctl bootstrap "gui/$UID" "$PLIST_DEST"
launchctl enable "gui/$UID/$LABEL"
green "  launchd agent bootstrapped ($LABEL)"

# ---- 6. Grok skill ------------------------------------------------------------
if [[ "$INSTALL_GROK_SKILL" == "1" ]]; then
    "$INSTALL_ROOT/install-skill.sh"
else
    yellow "  skipped Grok skill copy (INSTALL_GROK_SKILL=0)"
fi

# ---- 7. next steps ------------------------------------------------------------
echo
bold "Install complete."
echo
yellow "ONE MANUAL STEP REMAINS: grant Full Disk Access to the wrapper."
echo
echo "  1. Open: System Settings -> Privacy & Security -> Full Disk Access"
echo "  2. Click +, press Cmd-Shift-G, and paste:"
echo
echo "       $WRAPPER_BIN"
echo
echo "  3. Make sure 'grokbot-things-helper' is switched ON."
echo
echo "The first time you approve a change, macOS asks whether"
echo "'grokbot-things-helper' may control 'Things3'. Click OK. That grant lives"
echo "under System Settings -> Privacy & Security -> Automation."
echo
echo "Bridge folder (tell Grok this path): $INSTALL_ROOT"
echo "Logs:      $CONTROL_DIR/log.txt"
if [[ "$INSTALL_GROK_SKILL" == "1" ]]; then
    echo "Doctor:    \"$PYTHON3_PATH\" \"$INSTALL_ROOT/tools/doctor.py\" --bridge \"$INSTALL_ROOT\""
else
    echo "Doctor:    \"$PYTHON3_PATH\" \"$INSTALL_ROOT/tools/doctor.py\" --bridge \"$INSTALL_ROOT\" --skip-grok"
fi
echo "Uninstall: ./uninstall.sh"
