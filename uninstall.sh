#!/bin/bash
# uninstall.sh — remove the standard Grok Bot Things launchd agent and skill.
#
# Leaves bin/, control/, and policy/ in place so logs and policy survive.
# Privacy grants must be revoked by hand in System Settings (printed below).

set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
    printf "\033[31mError: do not run this uninstaller as root or with sudo.\033[0m\n" 1>&2
    exit 1
fi

LABEL="com.mcassano.grokbot-things"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
SKILL_DEST="${GROK_HOME:-$HOME/.grok}/skills/things-grok-bot"

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

cat <<'MSG'

Uninstalled the launchd agent and Grok skill.

To remove everything:
  - Delete this folder (it holds the built wrapper, logs, and policy).
  - System Settings -> Privacy & Security -> Full Disk Access:
    remove 'grokbot-things-helper'.
  - System Settings -> Privacy & Security -> Automation:
    turn off 'grokbot-things-helper -> Things3'.
MSG
