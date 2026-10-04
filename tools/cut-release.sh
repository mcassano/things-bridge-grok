#!/bin/bash
# Create a signed, annotated release tag locally. Does not push.
#
# Usage: tools/cut-release.sh vX.Y.Z
#
# The Release workflow publishes only tags whose signature GitHub verifies.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 vX.Y.Z" >&2
    exit 1
fi
TAG="$1"
if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: tag must match vX.Y.Z" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
"$REPO_ROOT/tools/test.sh" "$TAG"

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    echo "Error: working tree is not clean" >&2
    exit 1
fi
if git -C "$REPO_ROOT" rev-parse "$TAG" >/dev/null 2>&1; then
    echo "Error: tag $TAG already exists" >&2
    exit 1
fi

git -C "$REPO_ROOT" tag -s "$TAG" -m "$TAG"
echo "Created signed tag $TAG. Review it, then publish with:"
echo "  git push origin $TAG"
