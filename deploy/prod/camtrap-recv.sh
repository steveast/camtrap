#!/bin/sh
# camtrap receiver — forced command for the LAPTOP's key on the VPS.
#
# The laptop can only write. This script never lists, reads back or deletes anything: the key on
# an unencrypted disk is assumed to be in a thief's hands, so it must not be able to find out what
# was already delivered, let alone remove it.
#
# Install:
#   ~/.ssh/authorized_keys:
#     command="/home/<user>/camtrap-recv.sh",no-pty,no-port-forwarding,no-agent-forwarding,\
#     no-X11-forwarding ssh-ed25519 AAAA... camtrap-laptop
#
# Protocol (the laptop puts the verb in SSH_ORIGINAL_COMMAND, the payload on stdin):
#   put-frame <name>   store an artefact, reply: ok <name> <bytes> <sha256>
#   heartbeat          store one status line, reply: ok heartbeat <bytes>
#   ping               reply: ok ping
#
# One tick line per invocation goes to the journal, in the style of the external prober.
set -eu

CAMTRAP_ROOT="${CAMTRAP_ROOT:-$HOME/camtrap}"
INBOX="$CAMTRAP_ROOT/inbox"
STATE="$CAMTRAP_ROOT/state"
MAX_BYTES="${CAMTRAP_MAX_BYTES:-8388608}"      # 8 MiB per artefact
MAX_HEARTBEAT="${CAMTRAP_MAX_HEARTBEAT:-8192}"
RETENTION_DAYS="${CAMTRAP_RETENTION_DAYS:-14}"

log() {
    # logger keeps this visible as `journalctl -t camtrap-recv`; stderr is the fallback.
    logger -t camtrap-recv "$*" 2>/dev/null || echo "camtrap-recv $*" >&2
}

die() {
    log "reject reason=$1 detail=${2:-}"
    echo "error $1" >&2
    exit 1
}

# The name must be a plain artefact name: no directories, no traversal, no surprises.
valid_name() {
    case "$1" in
        *[!A-Za-z0-9._-]*) return 1 ;;
        .*|*/*|"") return 1 ;;
        evt_*.jpg|evt_*.json) return 0 ;;
        *) return 1 ;;
    esac
}

mkdir -p "$INBOX" "$STATE"
chmod 700 "$CAMTRAP_ROOT" "$INBOX" "$STATE" 2>/dev/null || true

# Word-split only: the value is never evaluated, so `&&`, `$(...)` and `;` cannot run. Extra
# words are refused rather than ignored — silently accepting a malformed request hides a broken
# client, and this is the one interface a stolen laptop can reach.
set -- ${SSH_ORIGINAL_COMMAND:-}
verb="${1:-}"
name="${2:-}"
argc=$#

case "$verb" in
    put-frame) [ "$argc" -eq 2 ] || die bad_arity "$argc" ;;
    ping|heartbeat) [ "$argc" -eq 1 ] || die bad_arity "$argc" ;;
esac

case "$verb" in
    ping)
        log "tick verb=ping"
        echo "ok ping"
        ;;

    heartbeat)
        tmp=$(mktemp "$STATE/.hb.XXXXXX")
        trap 'rm -f "$tmp"' EXIT INT TERM
        head -c "$MAX_HEARTBEAT" > "$tmp"
        bytes=$(wc -c < "$tmp" | tr -d ' ')
        mv "$tmp" "$STATE/heartbeat"
        trap - EXIT INT TERM
        printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STATE/heartbeat.at"
        log "tick verb=heartbeat bytes=$bytes"
        echo "ok heartbeat $bytes"
        ;;

    put-frame)
        [ -n "$name" ] || die missing_name
        valid_name "$name" || die bad_name "$name"

        tmp=$(mktemp "$INBOX/.incoming.XXXXXX")
        trap 'rm -f "$tmp"' EXIT INT TERM
        # head -c caps the payload; a truncated artefact is still better than a full disk, and
        # the size is reported back so the sender can tell.
        head -c "$MAX_BYTES" > "$tmp"
        bytes=$(wc -c < "$tmp" | tr -d ' ')
        [ "$bytes" -gt 0 ] || die empty_payload "$name"

        sum=$(sha256sum "$tmp" | cut -d' ' -f1)
        # Atomic publish: the poller on the Pi never sees a half-written frame.
        mv "$tmp" "$INBOX/$name"
        trap - EXIT INT TERM
        chmod 600 "$INBOX/$name"

        log "tick verb=put-frame name=$name bytes=$bytes sha=$sum"
        echo "ok $name $bytes $sum"

        # Retention runs on receipt: no cron on the VPS, nothing to forget to install.
        find "$INBOX" -type f -mtime "+$RETENTION_DAYS" -delete 2>/dev/null || true
        ;;

    *)
        die unknown_verb "$verb"
        ;;
esac
