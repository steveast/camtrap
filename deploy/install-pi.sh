#!/bin/sh
# Installs the poller on the Pi. Run LAST: receiver, then sender, then poller.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo "run as root (it writes /usr/local/bin and /etc/cron.d)" >&2; exit 1; }

install -m 755 -o root -g root "$REPO/deploy/pi/camtrap-poll.sh" /usr/local/bin/camtrap-poll.sh
install -m 644 -o root -g root "$REPO/deploy/pi/camtrap-poll.cron" /etc/cron.d/camtrap-poll
install -d -m 755 /var/lib/camtrap-poll

if [ ! -f /etc/camtrap-poll.env ]; then
    install -m 600 "$REPO/deploy/pi/camtrap-poll.env.example" /etc/camtrap-poll.env
    echo "created /etc/camtrap-poll.env — fill in the token, chat id and CAMTRAP_SSH"
fi

echo "installed. its own cron file, not mixed into anyone else's:"
cat /etc/cron.d/camtrap-poll
