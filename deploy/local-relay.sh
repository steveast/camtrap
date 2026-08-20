#!/bin/sh
# Local stand-in for `ssh <target> <verb>`: runs the real camtrap-tg.sh with the verb where it
# expects it. Used to exercise the receiver and poller scripts end to end on one machine, before
# anything is installed on the boxes — same code, local transport.
set -eu

SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${CAMTRAP_ROOT:?CAMTRAP_ROOT must point at the local inbox root}"

SSH_ORIGINAL_COMMAND="$*" exec sh "$SELF_DIR/prod/camtrap-tg.sh"
