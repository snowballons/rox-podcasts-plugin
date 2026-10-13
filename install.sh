#!/bin/bash
#
# Install the Rox podcast plugin.
#
# Downloads the 10 plugin files from the public repo and copies them into
# Rox's plugins folder for this platform:
#   Linux   $XDG_DATA_HOME/rox/plugins/podcast  (~/.local/share/...)
#   macOS   ~/Library/Application Support/rox/plugins/podcast
#   Windows (Git Bash)  $APPDATA/rox/plugins/podcast
#
# Native Windows PowerShell users: use install.ps1 instead.
#
# Usage:
#   bash install.sh [--target DIR] [--branch NAME]
#
# Afterwards: Rox > Settings > Plugins > switch Podcasts on and approve
# the trust card. (Any new or changed files start switched off.)

set -eu -o pipefail

REPO="snowballons/rox-podcasts-plugin"
BRANCH="main"
TARGET_OVERRIDE=""

# The 10 plugin files. Everything else in the repo (installer scripts,
# LICENSE, git metadata) is deliberately left out of Rox's file hash.
PLUGIN_FILES="podcast.py protocol.py store.py directory.py feed.py stream.py handlers.py plugin.json icon.svg README.md"

while [ $# -gt 0 ]; do
    case "$1" in
        --target)
            TARGET_OVERRIDE="${2:?--target needs a directory}"; shift 2 ;;
        --branch)
            BRANCH="${2:?--branch needs a name}"; shift 2 ;;
        -h|--help)
            echo "Usage: bash install.sh [--target DIR] [--branch NAME]"; exit 0 ;;
        *)
            echo "error: unknown argument: $1" >&2; exit 1 ;;
    esac
done

resolve_target() {
    if [ -n "$TARGET_OVERRIDE" ]; then
        printf '%s' "$TARGET_OVERRIDE"
        return
    fi
    case "$(uname -s)" in
        Darwin)
            printf '%s' "$HOME/Library/Application Support/rox/plugins/podcast" ;;
        MINGW*|MSYS*|CYGWIN*)
            if [ -z "${APPDATA:-}" ]; then
                echo "error: APPDATA is not set; use --target DIR" >&2; exit 1
            fi
            printf '%s' "$APPDATA/rox/plugins/podcast" ;;
        *)
            printf '%s' "${XDG_DATA_HOME:-$HOME/.local/share}/rox/plugins/podcast" ;;
    esac
}

TARGET="$(resolve_target)"

if [ -e "$TARGET" ]; then
    echo "exists: $TARGET" >&2
    echo "Remove it first to reinstall, then re-approve the plugin in Rox." >&2
    exit 1
fi

# Pick a Python 3 interpreter for the pre-flight check. (Rox does its own
# lookup when the plugin is switched on: python3/python elsewhere,
# py -3/python3/python on Windows.)
PY=""
for candidate in python3 py python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    case "$(uname -s)" in
        Darwin)
            echo "error: no Python 3 found. Install it first, e.g. 'brew install python3'." >&2 ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "error: no Python 3 found (tried python3, py, python)." >&2
            echo "Install it from https://www.python.org/downloads/ and re-run." >&2 ;;
        *)
            echo "error: no Python 3 found. Install it with your distro package manager." >&2 ;;
    esac
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "error: curl not found; install curl and re-run." >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Downloading $REPO ($BRANCH)..."
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" -o "$WORK/plugin.tar.gz"
tar -xzf "$WORK/plugin.tar.gz" -C "$WORK"

# GitHub tarballs extract to a single top-level dir (<repo>-<branch>).
SRC="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$SRC" ]; then
    echo "error: could not unpack the download." >&2
    exit 1
fi

mkdir -p "$TARGET"
for f in $PLUGIN_FILES; do
    if [ ! -f "$SRC/$f" ]; then
        echo "error: expected file missing from download: $f" >&2
        rm -rf "$TARGET"
        exit 1
    fi
    cp "$SRC/$f" "$TARGET/"
done

if [ "$(ls -1 "$TARGET" | wc -l | tr -d ' ')" != "10" ]; then
    echo "error: expected exactly 10 files in $TARGET; aborting." >&2
    rm -rf "$TARGET"
    exit 1
fi

# Syntax check without writing bytecode (ast.parse creates no __pycache__,
# which must stay out of the plugin folder so it never enters Rox's hash).
"$PY" -c "
import ast, sys
for path in sys.argv[1:]:
    with open(path, encoding='utf-8') as fh:
        ast.parse(fh.read(), filename=path)
" "$TARGET/podcast.py" "$TARGET/protocol.py" \
    "$TARGET/store.py" "$TARGET/directory.py" "$TARGET/feed.py" \
    "$TARGET/stream.py" "$TARGET/handlers.py" || {
    echo "error: downloaded files failed a syntax check; aborting." >&2
    rm -rf "$TARGET"
    exit 1
}

echo "Installed 10 files to: $TARGET"
echo "Next steps:"
echo "  1. Rox > Settings > Plugins > turn on Enable Plugins"
echo "  2. Switch Podcasts on and approve the trust card"
