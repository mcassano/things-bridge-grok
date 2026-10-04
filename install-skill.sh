#!/bin/bash
# Install SKILL.md into Grok's user-level skill discovery directory.

set -euo pipefail

if [[ "$EUID" -eq 0 ]]; then
    echo "Error: run install-skill.sh as your normal user, not with sudo." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILL_NAME="things-grok-bot"
GROK_SKILLS_ROOT="${GROK_HOME:-$HOME/.grok}/skills"
SKILL_DEST="$GROK_SKILLS_ROOT/$SKILL_NAME"

mkdir -p "$SKILL_DEST"
chmod 700 "$GROK_SKILLS_ROOT" "$SKILL_DEST"
cp "$REPO_ROOT/SKILL.md" "$SKILL_DEST/SKILL.md"
chmod 600 "$SKILL_DEST/SKILL.md"
echo "Installed Grok skill: $SKILL_DEST/SKILL.md"

if command -v grok >/dev/null 2>&1; then
    echo "Verifying Grok discovery with: grok inspect"
    inspect_output=""
    inspect_status=0
    inspect_output="$(grok inspect 2>&1)" || inspect_status=$?
    if [[ "$inspect_status" -eq 0 ]] && grep -q "$SKILL_NAME" <<<"$inspect_output"; then
        echo "Grok discovered $SKILL_NAME."
    else
        echo "Warning: grok inspect did not report $SKILL_NAME." >&2
        if [[ "$inspect_status" -ne 0 ]]; then
            echo "grok inspect exited with status $inspect_status." >&2
        fi
        echo "Reload Grok and run /skills, or inspect $SKILL_DEST." >&2
    fi
else
    echo "Grok CLI not found; run 'grok inspect' after installing Grok."
fi
