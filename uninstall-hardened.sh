#!/bin/bash
# Remove the hardened helper while preserving the user-owned runtime bridge.

set -euo pipefail
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

if [[ "$EUID" -eq 0 ]]; then
    echo "Error: run as your normal user; this script invokes sudo narrowly." >&2
    exit 1
fi

LABEL="com.mcassano.grokbot-things"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
SKILL_DEST="${GROK_HOME:-$HOME/.grok}/skills/things-grok-bot"
PRODUCT_ROOT="/Library/Application Support/GrokBotThings"
USER_ROOT="$PRODUCT_ROOT/users/$UID"
BRIDGE_ROOT="${GROKBOT_THINGS_BRIDGE:-$HOME/Library/Application Support/GrokBotThings}"

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL"
    echo "  launchd agent unloaded"
fi
if [[ -f "$PLIST_DEST" ]]; then
    rm -f "$PLIST_DEST"
    echo "  removed $PLIST_DEST"
fi
if [[ -d "$SKILL_DEST" && ! -L "$SKILL_DEST" ]]; then
    rm -rf "$SKILL_DEST"
    echo "  removed Grok skill $SKILL_DEST"
fi
if [[ -d "$USER_ROOT" && ! -L "$USER_ROOT" ]]; then
    sudo /bin/rm -rf "$USER_ROOT"
    echo "  removed root-owned helper $USER_ROOT"
    if sudo /bin/rmdir "$PRODUCT_ROOT/users" 2>/dev/null; then
        if ! sudo /bin/rmdir "$PRODUCT_ROOT" 2>/dev/null; then
            echo "  retained non-empty $PRODUCT_ROOT"
        fi
    fi
fi

cat <<MSG

Hardened helper uninstalled. Runtime data remains at:
  $BRIDGE_ROOT

Delete that directory after reviewing anything you want to keep. Revoke
'grokbot-things-helper' under Full Disk Access and Automation in
System Settings -> Privacy & Security.
MSG
