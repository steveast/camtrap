#!/bin/sh
# camtrap poller — runs on the Pi at home, from cron, every 2 minutes.
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
TAMPER_LINK_SEC="${TAMPER_LINK_SEC:-600}"
# Unlimited by default: the owner's call — more information beats fewer notifications, and a
# missed event cannot be recovered from a summary. Set MOTION_ALERTS_PER_HOUR to a positive
# number to cap ordinary motion (tamper is never capped) if the flow becomes unusable.
MOTION_ALERTS_PER_HOUR="${MOTION_ALERTS_PER_HOUR:-0}"
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
    if [ -f "$marker" ]; then
        last=$(cut -d' ' -f2 "$marker" 2>/dev/null || echo 0)
        [ $((now - last)) -ge "$REPEAT_SEC" ] || return 0
    fi
    if send_message "$message"; then
        printf '%s %s\n' "$now" "$now" > "$marker"
        log "alert check=$check state=down"
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
    : "${type:=motion}"
    : "${frames:=1}"

    stamp=$(printf '%s\n' "$listing" | awk -v e="$event" '$1 ~ "^"e {print int($3); exit}')
    : "${stamp:=$(date -u +%s)}"
    when=$(local_time "$stamp")

    case "$type" in
        tamper)
            tamper_seen=1
            icon="🚨"
            headline="the laptop is being handled"
            ;;
        light) icon="💡"; headline="light switched on" ;;
        *) icon="📷"; headline="movement in the room" ;;
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
frames: $frames${sound:+
sound: $sound}$cut_short"

    now_epoch=$(date -u +%s)
    if [ "$type" != "tamper" ] && ! motion_budget_left "$now_epoch"; then
        # Over budget: mark it seen so it is not re-examined, and count it for the summary.
        printf '%s\n' "$now_epoch" > "$STATE_DIR/sent-$event"
        motion_note_suppressed
        log "event id=$event type=$type suppressed=1 reason=hourly_cap"
        continue
    fi

    first="${event}_000.jpg"
    if printf '%s\n' "$listing" | grep -q "^$first "; then
        if send_photo "$first" "$caption"; then
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
