#!/bin/sh
# Installs camtrap on the laptop: venv, sounds, systemd --user unit.
# Source of truth is the repository; nothing here is edited by hand on the machine.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/camtrap"
VENV="$DATA/venv"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/camtrap"

echo "== venv $VENV"
mkdir -p "$DATA" "$UNIT_DIR" "$CONFIG_DIR" "$DATA/sounds" "$DATA/spool"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$REPO/requirements.txt"
"$VENV/bin/pip" install -q -e "$REPO"

echo "== sounds"
"$REPO/tools/make-siren.sh" --mode "${SIREN_MODE:-yelp}" >/dev/null
"$REPO/tools/make-warning.sh" --all >/dev/null || echo "   (some warning languages failed)"
ls -1 "$DATA/sounds"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    echo "== config $CONFIG_DIR/config.toml"
    cat > "$CONFIG_DIR/config.toml" <<'TOML'
# camtrap — only overrides; defaults live in src/camtrap/config.py
[sound]
warn_langs = ["vi", "en"]

[arming]
mode = "on_lock"
TOML
fi

# The token file for the clip sink. Created empty and 600 rather than filled in: the installer
# has no business knowing a token, and the agent refuses to read this file if anyone else can.
TG_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/camtrap/telegram.env"
if [ ! -f "$TG_ENV" ]; then
    mkdir -p "$(dirname "$TG_ENV")"
    cp "$REPO/deploy/telegram.env.example" "$TG_ENV"
    chmod 600 "$TG_ENV"
    echo "== created $TG_ENV (600) — put a bot token in it, ideally a bot of its own"
fi

echo "== unit $UNIT_DIR/camtrap.service"
cp "$REPO/deploy/systemd/camtrap.service" "$UNIT_DIR/camtrap.service"
systemctl --user daemon-reload

cat <<'NEXT'

Installed, not started. Before arming, in this order:

  0. ~/.config/camtrap/telegram.env — a bot token for the clips, in a bot of its own
  1. camtrap selftest          — camera, audio path, inhibitors, receiver, encoder, chat
  2. camtrap siren-test        — is the siren audible from the built-in speakers?
  3. camtrap warn-test         — is the spoken warning intelligible in its language?
  4. camtrap calibrate --sec 60 — in the room, at the light level it will have
  5. systemctl --user enable --now camtrap

Power settings are yours to change, not the installer's: set the lid action to "do nothing"
in the desktop's power manager. The agent holds its own inhibitor, but a desktop that insists
on suspending will still win.
NEXT
