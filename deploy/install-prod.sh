#!/bin/sh
# Installs the receiver side on the VPS. Run this FIRST — the receiver defines the contract, and
# the reverse order gives a false red on the tick between edits.
#
# It deliberately does NOT touch authorized_keys: adding a forced-command key is the owner's
# decision, and the machine already runs another project's monitoring. It prints the exact line
# instead.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: $0 user@vps" >&2; exit 2; }

echo "== copying scripts to $TARGET"
scp -q "$REPO/deploy/prod/camtrap-recv.sh" "$REPO/deploy/prod/camtrap-tg.sh" "$TARGET:"
ssh "$TARGET" 'chmod 700 camtrap-recv.sh camtrap-tg.sh && mkdir -p camtrap/inbox camtrap/state && chmod 700 camtrap camtrap/inbox camtrap/state'

cat <<'NEXT'

Scripts are in place. Two authorized_keys lines are still needed, and they are yours to add:

  # the laptop: write-only, cannot list or read anything
  command="/home/USER/camtrap-recv.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA...laptop

  # the Pi: reads state and relays to Telegram, token arrives on stdin per call
  command="/home/USER/camtrap-tg.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA...pi

Generate the keys separately (never reuse an existing monitoring key):

  ssh-keygen -t ed25519 -N '' -f ~/.ssh/camtrap-laptop -C camtrap-laptop
  ssh-keygen -t ed25519 -N '' -f ~/.ssh/camtrap-pi     -C camtrap-pi

Then verify from the laptop:  ssh -i ~/.ssh/camtrap-laptop USER@VPS ping   ->  ok ping
NEXT
