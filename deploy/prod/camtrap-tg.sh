#!/bin/sh
# camtrap Telegram relay + read side — forced command for the PI's key on the VPS.
#
# The Pi holds the token and never lets it touch disk here: it arrives on stdin per call and is
# used once. This box relays because api.telegram.org is blocked on the home network, and because
# the frame is already here — the picture crosses the network once instead of three times.
#
# Install:
#   ~/.ssh/authorized_keys:
#     command="/home/<user>/camtrap-tg.sh",no-pty,no-port-forwarding,no-agent-forwarding,\
#     no-X11-forwarding ssh-ed25519 AAAA... camtrap-pi
#
# Protocol (verb in SSH_ORIGINAL_COMMAND):
#   list                       one line per inbox artefact: <name> <bytes> <mtime>
#   state                      the stored heartbeat line, plus hb_age=<seconds>
#   manifest <name>            print one manifest
#   send-photo <name>          stdin: <token>\n<chat_id>\n<caption...>
#   send-album <n1,n2,...>     stdin: <token>\n<chat_id>\n<caption...>  (up to 10, one group)
#   send-doc <name>            stdin: <token>\n<chat_id>\n<caption...>  (uncompressed original)
#   send-message               stdin: <token>\n<chat_id>\n<text...>
#   delete <name>              remove one delivered artefact
set -eu

CAMTRAP_ROOT="${CAMTRAP_ROOT:-$HOME/camtrap}"
INBOX="$CAMTRAP_ROOT/inbox"
STATE="$CAMTRAP_ROOT/state"
API="${CAMTRAP_TG_API:-https://api.telegram.org}"
CURL_MAX_TIME="${CAMTRAP_CURL_MAX_TIME:-30}"

log() { logger -t camtrap-tg "$*" 2>/dev/null || echo "camtrap-tg $*" >&2; }
die() { log "reject reason=$1 detail=${2:-}"; echo "error $1" >&2; exit 1; }

valid_name() {
    case "$1" in
        *[!A-Za-z0-9._-]*) return 1 ;;
        .*|*/*|"") return 1 ;;
        evt_*.jpg|evt_*.json) return 0 ;;
        *) return 1 ;;
    esac
}

set -- ${SSH_ORIGINAL_COMMAND:-}
verb="${1:-}"
name="${2:-}"
argc=$#

mkdir -p "$INBOX" "$STATE"

case "$verb" in
    list)
        [ "$argc" -eq 1 ] || die bad_arity "$argc"
        # name, size, mtime — enough for the Pi to decide what is new without reading anything
        find "$INBOX" -maxdepth 1 -type f -name 'evt_*' -printf '%f %s %T@\n' 2>/dev/null |
            sort || true
        ;;

    state)
        [ "$argc" -eq 1 ] || die bad_arity "$argc"
        if [ -f "$STATE/heartbeat" ]; then
            cat "$STATE/heartbeat"
            now=$(date -u +%s)
            hb=$(stat -c %Y "$STATE/heartbeat" 2>/dev/null || echo "$now")
            echo "hb_age=$((now - hb))"
        else
            echo "hb_age=-1"
        fi
        ;;

    manifest)
        [ "$argc" -eq 2 ] || die bad_arity "$argc"
        valid_name "$name" || die bad_name "$name"
        case "$name" in *.json) ;; *) die not_a_manifest "$name" ;; esac
        [ -f "$INBOX/$name" ] || die missing "$name"
        cat "$INBOX/$name"
        ;;

    send-photo)
        [ "$argc" -eq 2 ] || die bad_arity "$argc"
        valid_name "$name" || die bad_name "$name"
        [ -f "$INBOX/$name" ] || die missing "$name"
        IFS= read -r token || die no_token
        IFS= read -r chat || die no_chat
        caption=$(cat)
        # --form reads the file from disk here; the token is only ever an argument to this one call
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
            -F "chat_id=$chat" \
            -F "caption=$caption" \
            -F "photo=@$INBOX/$name" \
            "$API/bot$token/sendPhoto" || echo 000)
        log "tick verb=send-photo name=$name code=$code"
        [ "$code" = "200" ] || die tg_http "$code"
        echo "ok send-photo $name"
        ;;

    send-doc)
        # sendDocument, not sendPhoto: Telegram re-encodes anything sent as a photo, and a face
        # that survived JPEG 95 at 1080p should not then be resized by a chat client. This is the
        # copy an investigator would be shown.
        [ "$argc" -eq 2 ] || die bad_arity "$argc"
        valid_name "$name" || die bad_name "$name"
        [ -f "$INBOX/$name" ] || die missing "$name"
        IFS= read -r token || die no_token
        IFS= read -r chat || die no_chat
        caption=$(cat)
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
            -F "chat_id=$chat" \
            -F "caption=$caption" \
            -F "document=@$INBOX/$name" \
            "$API/bot$token/sendDocument" || echo 000)
        log "tick verb=send-doc name=$name code=$code"
        [ "$code" = "200" ] || die tg_http "$code"
        echo "ok send-doc $name"
        ;;

    send-album)
        [ "$argc" -eq 2 ] || die bad_arity "$argc"
        IFS= read -r token || die no_token
        IFS= read -r chat || die no_chat
        caption=$(cat)

        # Build a sendMediaGroup call: JSON describing the items, plus one attachment per file.
        media=""; forms=""; count=0
        old_ifs=$IFS; IFS=','
        for one in $name; do
            IFS=$old_ifs
            valid_name "$one" || die bad_name "$one"
            [ -f "$INBOX/$one" ] || die missing "$one"
            [ "$count" -lt 10 ] || break
            # Caption rides on the first item only; Telegram shows it under the group.
            if [ "$count" -eq 0 ]; then
                item="{\"type\":\"photo\",\"media\":\"attach://p$count\",\"caption\":$(
                    printf '%s' "$caption" | sed 's/\\/\\\\/g; s/"/\\"/g; s/$/\\n/' | tr -d '\n' |
                    sed 's/^/"/; s/\\n$/"/'
                )}"
            else
                item="{\"type\":\"photo\",\"media\":\"attach://p$count\"}"
            fi
            [ -z "$media" ] && media="$item" || media="$media,$item"
            forms="$forms -F p$count=@$INBOX/$one"
            count=$((count + 1))
            IFS=','
        done
        IFS=$old_ifs
        [ "$count" -gt 0 ] || die no_files

        # shellcheck disable=SC2086
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
            -F "chat_id=$chat" -F "media=[$media]" $forms \
            "$API/bot$token/sendMediaGroup" || echo 000)
        log "tick verb=send-album count=$count code=$code"
        [ "$code" = "200" ] || die tg_http "$code"
        echo "ok send-album $count"
        ;;

    send-message)
        [ "$argc" -eq 1 ] || die bad_arity "$argc"
        IFS= read -r token || die no_token
        IFS= read -r chat || die no_chat
        text=$(cat)
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
            --data-urlencode "chat_id=$chat" \
            --data-urlencode "text=$text" \
            -G "$API/bot$token/sendMessage" || echo 000)
        log "tick verb=send-message code=$code"
        [ "$code" = "200" ] || die tg_http "$code"
        echo "ok send-message"
        ;;

    delete)
        [ "$argc" -eq 2 ] || die bad_arity "$argc"
        valid_name "$name" || die bad_name "$name"
        rm -f "$INBOX/$name"
        log "tick verb=delete name=$name"
        echo "ok delete $name"
        ;;

    *)
        die unknown_verb "$verb"
        ;;
esac
