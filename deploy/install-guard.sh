#!/bin/sh
# Installs the `guard` launcher into the owner's portable app folder (which is on PATH).
# Copies rather than symlinks: that folder syncs to the cloud, and a symlink into ~/Dev would
# break on any other machine.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET_DIR="${GUARD_BIN_DIR:-$HOME/MEGA/os/apps}"

[ -d "$TARGET_DIR" ] || { echo "no such directory: $TARGET_DIR" >&2; exit 1; }

install -m 755 "$REPO/deploy/guard" "$TARGET_DIR/guard"
echo "installed $TARGET_DIR/guard"

# A local copy too, so an unsynced or emptied cloud folder cannot take the trap away.
LOCAL_DIR="$HOME/.local/bin"
mkdir -p "$LOCAL_DIR"
install -m 755 "$REPO/deploy/guard" "$LOCAL_DIR/guard"
echo "installed $LOCAL_DIR/guard (fallback if the synced copy disappears)"

case ":$PATH:" in
    *":$TARGET_DIR:"*) echo "$TARGET_DIR is on PATH" ;;
    *) echo "note: $TARGET_DIR is not on PATH in this shell" ;;
esac

printf '\nsha256: '
sha256sum "$TARGET_DIR/guard" | cut -d' ' -f1
echo "Keep that hash: the folder is cloud-synced, so a changed launcher is worth noticing."
