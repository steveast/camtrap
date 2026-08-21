#!/bin/sh
# camtrap poller — runs on the Pi at home, from cron, every minute.
#
# The token lives here and nowhere else. Frames stay on the VPS: this script asks the VPS to send
# them, so the picture crosses the network once. Discipline copied from the external prober:
#   - state mutates only AFTER a successful send, so a failed send is a retry, not a lost event
#   - a new failure alerts once, repeats every REPEAT_SEC while it lasts, then recovers with a green
#   - one story per situation: "handled then went silent" is one message, not two alarms
set -eu

CONF="${CAMTRAP_POLL_ENV:-/etc/camtrap-poll.env}"
[ -f "$CONF" ] && . "$CONF"

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN missing}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID missing}"
: "${CAMTRAP_SSH:?CAMTRAP_SSH missing (e.g. \"ssh -i /home/piuser/.ssh/camtrap-pi user@vps\")}"

STATE_DIR="${CAMTRAP_STATE_DIR:-/var/lib/camtrap-poll}"
HB_STALE_SEC="${HB_STALE_SEC:-300}"
REPEAT_SEC="${REPEAT_SEC:-1800}"
# Reminders back off: 30 min, 1 h, 2 h, 4 h, then every REPEAT_MAX_SEC. Eighteen identical alerts
# through one night taught nobody anything the second one had not already said, and an alert that
# repeats past usefulness is an alert that gets muted — the exact failure this project cannot
# afford. Recovery still reports immediately.
REPEAT_MAX_SEC="${REPEAT_MAX_SEC:-21600}"
TAMPER_LINK_SEC="${TAMPER_LINK_SEC:-600}"
# Unlimited by default: the owner's call — more information beats fewer notifications, and a
# missed event cannot be recovered from a summary. Set MOTION_ALERTS_PER_HOUR to a positive
# number to cap ordinary motion (tamper is never capped) if the flow becomes unusable.
MOTION_ALERTS_PER_HOUR="${MOTION_ALERTS_PER_HOUR:-0}"
# How many frames of one event to send as a group. The first frame BY NUMBER is the oldest
# pre-buffer frame — an empty room seconds before anything happened — so the manifest's key_frame
# leads, and the rest follow as an album. 1 disables the album.
ALBUM_MAX="${ALBUM_MAX:-6}"
# Telegram re-encodes photos. The key frame is therefore ALSO sent as a document, which keeps the
# original 1080p/q95 pixels — the copy that would be shown to police. 0 disables it.
SEND_ORIGINAL="${SEND_ORIGINAL:-1}"
SUMMARY_MIN_SEC="${SUMMARY_MIN_SEC:-3600}"
TZ_LOCAL="${CAMTRAP_TZ:-Asia/Ho_Chi_Minh}"

mkdir -p "$STATE_DIR"

log() { logger -t camtrap-poll "$*" 2>/dev/null || echo "camtrap-poll $*" >&2; }

remote() { # shellcheck disable=SC2086
    ${CAMTRAP_SSH} "$@"
}

send_message() {
    printf '%s\n%s\n%s' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" "$1" | remote send-message
}

send_photo() {
    printf '%s\n%s\n%s' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" "$2" | remote "send-photo $1"
}

send_album() {
    printf '%s\n%s\n%s' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" "$2" | remote "send-album $1"
}

send_doc() {
    printf '%s\n%s\n%s' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" "$2" | remote "send-doc $1"
}

local_time() {
    TZ="$TZ_LOCAL" date -d "@$1" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null ||
        TZ="$TZ_LOCAL" date '+%Y-%m-%d %H:%M:%S %Z'
}

# --- failure bookkeeping: alert once, repeat while broken, recover green ------------------------

note_failure() { # check, message
    check=$1
    message=$2
    marker="$STATE_DIR/fail-$check"
    now=$(date -u +%s)
    repeats=0
    wait_for="$REPEAT_SEC"
    if [ -f "$marker" ]; then
        # marker: <first_seen> <last_sent> <repeats>
        last=$(cut -d' ' -f2 "$marker" 2>/dev/null || echo 0)
        repeats=$(cut -d' ' -f3 "$marker" 2>/dev/null || echo 0)
        [ -n "$repeats" ] || repeats=0
        # Double the interval per reminder, capped.
        wait_for=$REPEAT_SEC
        i=0
        while [ "$i" -lt "$repeats" ]; do
            wait_for=$((wait_for * 2))
            [ "$wait_for" -ge "$REPEAT_MAX_SEC" ] && wait_for=$REPEAT_MAX_SEC && break
            i=$((i + 1))
        done
        [ $((now - last)) -ge "$wait_for" ] || return 0
        first=$(cut -d' ' -f1 "$marker" 2>/dev/null || echo "$now")
        hours=$(( (now - first) / 3600 ))
        [ "$hours" -gt 0 ] && message="$message
(unresolved for ${hours}h; next reminder in $((wait_for * 2 / 3600))h or on recovery)"
    fi
    if send_message "$message"; then
        first=${first:-$now}
        printf '%s %s %s\n' "$first" "$now" "$((repeats + 1))" > "$marker"
        log "alert check=$check state=down repeats=$((repeats + 1)) next_in=$((wait_for * 2))"
    else
        log "alert_failed check=$check"
    fi
}

note_recovery() { # check, message
    check=$1
    message=$2
    marker="$STATE_DIR/fail-$check"
    [ -f "$marker" ] || return 0
    if send_message "$message"; then
        rm -f "$marker"
        log "alert check=$check state=up"
    else
        log "recovery_failed check=$check"
    fi
}

# --- motion alert budget -----------------------------------------------------------------------

# One epoch per line, pruned to the last hour.
motion_recent() {
    now=$1
    [ -f "$STATE_DIR/motion-alerts" ] || return 0
    awk -v now="$now" '$1 > now - 3600 { print $1 }' "$STATE_DIR/motion-alerts"
}

motion_budget_left() {
    now=$1
    # 0 (the default) means no cap at all.
    [ "$MOTION_ALERTS_PER_HOUR" -le 0 ] && return 0
    used=$(motion_recent "$now" | grep -c . || true)
    [ "$used" -lt "$MOTION_ALERTS_PER_HOUR" ]
}

motion_note_sent() {
    now=$1
    { motion_recent "$now"; echo "$now"; } > "$STATE_DIR/motion-alerts.new"
    mv "$STATE_DIR/motion-alerts.new" "$STATE_DIR/motion-alerts"
}

motion_note_suppressed() {
    count=0
    [ -f "$STATE_DIR/motion-suppressed" ] && count=$(cat "$STATE_DIR/motion-suppressed")
    echo $((count + 1)) > "$STATE_DIR/motion-suppressed"
}

# --- events ------------------------------------------------------------------------------------

listing=$(remote list || true)
state=$(remote state || true)

hb_age=$(printf '%s\n' "$state" | tr ' ' '\n' | awk -F= '$1 == "hb_age" {print $2}' | tail -1)
hb_mode=$(printf '%s\n' "$state" | tr ' ' '\n' | awk -F= '$1 == "mode" {print $2}' | tail -1)
hb_sound=$(printf '%s\n' "$state" | tr ' ' '\n' | awk -F= '$1 == "sound_ok" {print $2}' | tail -1)
hb_armed=$(printf '%s\n' "$state" | tr ' ' '\n' | awk -F= '$1 == "armed" {print $2}' | tail -1)
hb_reason=$(printf '%s\n' "$state" | tr ' ' '\n' | awk -F= '$1 == "arm_reason" {print $2}' | tail -1)
: "${hb_age:=-1}"
: "${hb_mode:=unknown}"
: "${hb_sound:=1}"
: "${hb_armed:=-}"
: "${hb_reason:=-}"

# Event ids present remotely, tamper first: the owner reacts differently to "someone came in"
# and to "the laptop is in someone's hands".
events=$(printf '%s\n' "$listing" | awk '{print $1}' | sed -n 's/^\(evt_[0-9TZ]*\).*/\1/p' | sort -u)

tamper_seen=0
for event in $events; do
    [ -n "$event" ] || continue
    [ -f "$STATE_DIR/sent-$event" ] && continue

    manifest=""
    if printf '%s\n' "$listing" | grep -q "^$event.json "; then
        manifest=$(remote "manifest $event.json" 2>/dev/null || true)
    fi

    type=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"type":"\([a-z]*\)".*/\1/p')
    frames=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"frames":\([0-9]*\).*/\1/p')
    signals=$(printf '%s' "$manifest" | tr -d ' \n' |
        sed -n 's/.*"signals":\[\([^]]*\)\].*/\1/p' | tr -d '"')
    sound=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"sound_stage":"\([a-z]*\)".*/\1/p')
    closed=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"closed":\([a-z]*\).*/\1/p')
    key=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"key_frame":"\([^"]*\)".*/\1/p')
    key_pct=$(printf '%s' "$manifest" | tr -d ' \n' |
        sed -n 's/.*"key_changed_pct":\([0-9.]*\).*/\1/p')
    : "${type:=motion}"
    : "${frames:=1}"

    stamp=$(printf '%s\n' "$listing" | awk -v e="$event" '$1 ~ "^"e {print int($3); exit}')
    : "${stamp:=$(date -u +%s)}"
    when=$(local_time "$stamp")

    # A power-button press outranks everything else in this list. It is not "handling": the
    # machine was armed and screaming, and someone reached for the one control that can end
    # that. Holding it for several seconds cuts power in hardware, so these frames may be the
    # last ones this laptop ever sends — that has to be legible at a glance, half asleep.
    case "$type,$signals" in
        tamper,*power_button_pressed*)
            tamper_seen=1
            icon="🆘"
            headline="POWER BUTTON PRESSED — someone is trying to switch the laptop off"
            urgent="
⚠️ The press was blocked and the siren is sounding, but holding the button for
   several seconds cuts power in hardware. These frames may be the last ones."
            ;;
        tamper,*)
            tamper_seen=1
            icon="🚨"
            headline="the laptop is being handled"
            urgent=""
            ;;
        light,*) icon="💡"; headline="light switched on"; urgent="" ;;
        *) icon="📷"; headline="movement in the room"; urgent="" ;;
    esac

    # An unclosed manifest means the agent stopped while the event was still running — the
    # laptop was carried off, shut down, or ran out of battery. That is worth saying out loud.
    cut_short=""
    [ "$closed" = "false" ] && cut_short="
note: event was still running when the agent stopped"

    caption="$icon $headline
time: $when
type: $type${signals:+
signals: $signals}
frames: $frames${key_pct:+
motion: $key_pct% of frame}${sound:+
sound: $sound}$cut_short$urgent"

    now_epoch=$(date -u +%s)
    if [ "$type" != "tamper" ] && ! motion_budget_left "$now_epoch"; then
        # Over budget: mark it seen so it is not re-examined, and count it for the summary.
        printf '%s\n' "$now_epoch" > "$STATE_DIR/sent-$event"
        motion_note_suppressed
        log "event id=$event type=$type suppressed=1 reason=hourly_cap"
        continue
    fi

    # The frame worth looking at, per the manifest; fall back to the first by number.
    first="${key:-${event}_000.jpg}"
    printf '%s\n' "$listing" | grep -q "^$first " || first="${event}_000.jpg"

    # Album: the key frame leads, the rest of the event follows in order.
    album="$first"
    if [ "$ALBUM_MAX" -gt 1 ]; then
        count=1
        for candidate in $(printf '%s\n' "$listing" | awk '{print $1}' |
                grep "^${event}_.*\.jpg$" | sort); do
            [ "$candidate" = "$first" ] && continue
            [ "$count" -lt "$ALBUM_MAX" ] || break
            album="$album,$candidate"
            count=$((count + 1))
        done
    fi

    # The uncompressed original of the key frame, for the copy that matters.
    send_original() {
        [ "$SEND_ORIGINAL" = "1" ] || return 0
        send_doc "$first" "🔍 original, uncompressed — $first" || log "original_failed name=$first"
    }

    if [ "$ALBUM_MAX" -gt 1 ] && [ "$album" != "$first" ]; then
        if send_album "$album" "$caption"; then
            send_original
            printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/sent-$event"
            [ "$type" = "tamper" ] && printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/last-tamper"
            [ "$type" != "tamper" ] && motion_note_sent "$now_epoch"
            log "event id=$event type=$type frames=$frames key=$first album=1 sent=1"
            continue
        fi
        log "event id=$event album_failed=1 falling back to single photo"
    fi

    if printf '%s\n' "$listing" | grep -q "^$first "; then
        if send_photo "$first" "$caption"; then
            send_original
            printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/sent-$event"
            [ "$type" = "tamper" ] && printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/last-tamper"
            [ "$type" != "tamper" ] && motion_note_sent "$now_epoch"
            log "event id=$event type=$type frames=$frames sent=1"
        else
            log "event id=$event type=$type sent=0"
            continue
        fi
    else
        if send_message "$caption"; then
            printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/sent-$event"
            log "event id=$event type=$type sent=1 photo=0"
        fi
    fi
done

# --- suppressed-motion summary -----------------------------------------------------------------

suppressed=0
[ -f "$STATE_DIR/motion-suppressed" ] && suppressed=$(cat "$STATE_DIR/motion-suppressed")
if [ "$suppressed" -gt 0 ]; then
    last_summary=0
    [ -f "$STATE_DIR/last-summary" ] && last_summary=$(cat "$STATE_DIR/last-summary")
    now_epoch=$(date -u +%s)
    if [ $((now_epoch - last_summary)) -ge "$SUMMARY_MIN_SEC" ]; then
        if send_message "📊 camtrap: $suppressed further motion event(s) not sent individually — the hourly cap is $MOTION_ALERTS_PER_HOUR. Frames are on the receiver."; then
            printf '%s\n' "$now_epoch" > "$STATE_DIR/last-summary"
            rm -f "$STATE_DIR/motion-suppressed"
            log "summary suppressed=$suppressed"
        fi
    fi
fi

# --- heartbeat ---------------------------------------------------------------------------------

now=$(date -u +%s)
last_tamper=0
[ -f "$STATE_DIR/last-tamper" ] && last_tamper=$(cat "$STATE_DIR/last-tamper")

if [ "$hb_age" -lt 0 ]; then
    note_failure hb "🔴 camtrap: no heartbeat on the receiver at all"
elif [ "$hb_age" -gt "$HB_STALE_SEC" ] && [ "$hb_mode" != "paused" ]; then
    silence_since=$((now - hb_age))
    if [ "$last_tamper" -gt 0 ] && [ $((now - last_tamper)) -le "$TAMPER_LINK_SEC" ]; then
        # One story with two cut-offs, not two separate alarms about the same minute.
        note_failure hb "🚨 camtrap: handled at $(local_time "$last_tamper"), then went silent at $(local_time "$silence_since")"
    else
        note_failure hb "🔴 camtrap: agent went silent at $(local_time "$silence_since") (mode=$hb_mode)"
    fi
else
    note_recovery hb "🟢 camtrap: heartbeat is back (age ${hb_age}s)"
fi

# Arming transitions: the owner walks out and wants to know the trap actually took hold, and
# wants to know just as much when it stops being armed.
if [ "$hb_armed" != "-" ]; then
    prev="-"
    [ -f "$STATE_DIR/last-armed" ] && prev=$(cat "$STATE_DIR/last-armed")
    if [ "$prev" = "-" ]; then
        # First observation: record it, say nothing. Announcing "not armed" the first time the
        # poller ever runs is noise, and noise is what makes alarms ignorable.
        printf '%s\n' "$hb_armed" > "$STATE_DIR/last-armed"
        log "armed_init value=$hb_armed reason=$hb_reason"
    elif [ "$hb_armed" != "$prev" ]; then
        if [ "$hb_armed" = "1" ]; then
            msg="🛡 camtrap: armed at $(local_time "$now") — the room is being watched"
        else
            msg="🔓 camtrap: no longer armed ($hb_reason)"
        fi
        if send_message "$msg"; then
            printf '%s\n' "$hb_armed" > "$STATE_DIR/last-armed"
            log "armed_change value=$hb_armed reason=$hb_reason"
        fi
    fi
fi

if [ "$hb_mode" = "armed" ] && [ "$hb_sound" = "0" ]; then
    note_failure sound "🔴 camtrap: the siren will not fire — sound_ok=0 on the agent"
else
    note_recovery sound "🟢 camtrap: sound is ready again"
fi

log "tick hb_age=$hb_age mode=$hb_mode sound_ok=$hb_sound events=$(printf '%s\n' "$events" | grep -c . || true) tamper=$tamper_seen"
