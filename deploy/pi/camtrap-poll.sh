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
# `if`, not `[ -f x ] && . x`: this script runs under `set -eu`, where that one-liner *exits*
# whenever the file is absent — status 1, no output, before the `:?` checks below ever get to say
# which variable is missing. A renamed env file would look like a poller that simply stopped.
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
fi

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
# leads, and the rest follow as an album. 1 disables the album, and **1 is the default since
# 2026-08-31, on the owner's instruction**: six photographs of one visit is five more than the
# alert needs, and the ones that were not the key frame were never the reason anyone opened it.
# The whole event is still on the receiver and in the warehouse, which is where a second view
# would be fetched from. Raise it to get the album back.
ALBUM_MAX="${ALBUM_MAX:-1}"
# Deliver EVERY frame of an event, not one photograph per event. The agent takes one frame every
# `snapshot_interval_sec` (5 s) for as long as motion lasts; with a single alert per event the rest
# of them only ever reached the receiver, so the chat showed one photograph of a three-minute
# visit. On the owner's instruction since 2026-09-03: the frames arrive as they are taken.
#
# The two complaints this has to satisfy at once — "show me the whole visit" and "do not put
# twenty-nine messages in my chat" — are answered by grouping rather than by dropping: frames
# travel as media groups of up to STREAM_BATCH_MAX (Telegram's own ceiling is 10), so a minute of
# motion is one or two messages carrying twelve photographs. STREAM=0 restores one alert per event.
STREAM="${STREAM:-1}"
STREAM_BATCH_MAX="${STREAM_BATCH_MAX:-10}"
# Groups per event per pass. cron fires every other minute and the cadence writes ~12 frames a
# minute, so one group per pass would fall permanently behind a visit that lasts. Three keeps up
# with 2.5 minutes of motion per pass and still leaves the tick well inside POLL_BUDGET_SEC.
STREAM_BATCHES_MAX="${STREAM_BATCHES_MAX:-3}"
# How full a FOLLOW-UP group has to be before it is worth a message of its own. The alert itself
# never waits — the first group of an event goes with whatever has arrived — but a pass runs every
# 15 s and the cadence writes a frame every 5 s, so without this a visit sends a group of three
# every quarter minute: four messages a minute, which is the complaint that shrank the cadence
# three times over. Waiting until a group can be filled makes a lasting visit ~one message a
# minute carrying ten photographs instead. Set to 1 to send each group as soon as it exists.
STREAM_MIN_BATCH="${STREAM_MIN_BATCH:-$STREAM_BATCH_MAX}"
# The tail of a visit is short by definition, and a closed event flushes it at once. This is the
# backstop for a visit that never closes — the agent was carried off, shut down or ran out of
# battery mid-event — so the last few frames cannot sit on the receiver waiting for a tenth that
# will never be taken.
STREAM_TAIL_SEC="${STREAM_TAIL_SEC:-120}"
# Telegram re-encodes photos, so the key frame used to ALSO go as a document keeping the original
# 1080p/q95 pixels. **Off by default since 2026-08-28, on the owner's instruction**: it doubled the
# messages per event for a copy nobody opened on a phone, and the original was never at risk — the
# untouched frame sits on the receiver and in the MEGA warehouse, which is where it would be
# fetched from if it were ever needed as evidence. Telegram was only ever the notification. Set to
# 1 to bring it back.
SEND_ORIGINAL="${SEND_ORIGINAL:-0}"
SUMMARY_MIN_SEC="${SUMMARY_MIN_SEC:-3600}"
TZ_LOCAL="${CAMTRAP_TZ:-Asia/Ho_Chi_Minh}"

# One connection, reused. Every `remote` call used to pay a full TCP and ssh handshake to a VPS
# abroad: measured from the same home network, 2707 ms cold against 674 ms over an established
# connection, and a tick makes several calls — which is why a tick took 8-10 s of the minute it
# had. ControlPersist also keeps the master alive between cron ticks, so most ticks pay nothing.
# Options given here come after $CAMTRAP_SSH, so anything already set there still wins.
CTL_DIR="${XDG_RUNTIME_DIR:-/tmp}"
SSH_MUX="-o ControlMaster=auto -o ControlPath=$CTL_DIR/camtrap-poll-%C -o ControlPersist=90"
[ "${SSH_MULTIPLEX:-1}" = "1" ] || SSH_MUX=""

mkdir -p "$STATE_DIR"

# Markers outlive the events they name: the receiver's retention drops an event and the poller
# then simply stops seeing it, but `sent-evt_*` stays behind for good. One tiny file per event is
# invisible, which is exactly why it would never be noticed accumulating.
find "$STATE_DIR" -maxdepth 1 -name 'sent-evt_*' -mtime +30 -delete 2>/dev/null || true

log() { logger -t camtrap-poll "$*" 2>/dev/null || echo "camtrap-poll $*" >&2; }

remote() { # shellcheck disable=SC2086
    ${CAMTRAP_SSH} ${SSH_MUX} "$@"
}

# Sub-minute cadence without a second scheduler. cron is the coarsest part of the whole chain: it
# fires once a minute, so an event waits 0-60 s (mean 30) before anyone even looks. With
# POLL_PASSES>1 the cron tick makes several passes inside its minute, and a crash still self-heals
# on the next tick. Each pass is a child, so a failure in one does not skip the rest.
#
# Two guards, because a run that spans most of a minute can meet the next cron tick:
#
#   - one run at a time. Two overlapping runs would both see the same unsent event and both send
#     it, and a duplicate alert is exactly what teaches you to stop reading them. The lock is taken
#     non-blocking, so the late run gives up: the winner is already doing the work.
#   - a budget. Passes stop once the run has spent POLL_BUDGET_SEC, handing the minute back
#     instead of colliding with its successor.
if [ "${POLL_PASSES:-1}" -gt 1 ] && [ -z "${CAMTRAP_POLL_CHILD:-}" ]; then
    LOCK="$STATE_DIR/poll.lock"
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK"
        flock -n 9 || { log "skip reason=already_running"; exit 0; }
    else
        mkdir "$LOCK.d" 2>/dev/null || { log "skip reason=already_running"; exit 0; }
        trap 'rmdir "$LOCK.d" 2>/dev/null || true' EXIT INT TERM
    fi
    started=$(date +%s)
    budget="${POLL_BUDGET_SEC:-50}"
    interval="${POLL_INTERVAL_SEC:-15}"
    pass=0
    while [ "$pass" -lt "${POLL_PASSES}" ]; do
        CAMTRAP_POLL_CHILD=1 "$0" || true
        pass=$((pass + 1))
        [ "$pass" -lt "${POLL_PASSES}" ] || break
        elapsed=$(( $(date +%s) - started ))
        if [ $((elapsed + interval)) -ge "$budget" ]; then
            log "passes_cut pass=$pass of ${POLL_PASSES} elapsed=${elapsed}s budget=${budget}s"
            break
        fi
        sleep "$interval"
    done
    exit 0
fi


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

# --- one group of frames, one message ----------------------------------------------------------

# Telegram's sendMediaGroup wants 2-10 items, so a batch of one goes as a plain photo. A group the
# relay refuses degrades to its newest frame rather than jamming: a batch that can never be sent
# would hold back every later frame of the same event, and the newest frame is the one that still
# shows what is happening. What was skipped is logged, never silently dropped.
send_batch() { # names (comma-separated, oldest first), caption
    names=$1
    text=$2
    count=$(printf '%s' "$names" | tr ',' '\n' | grep -c . || true)
    if [ "$count" -le 1 ]; then
        send_photo "$names" "$text"
        return $?
    fi
    if send_album "$names" "$text"; then
        return 0
    fi
    newest=$(printf '%s' "$names" | tr ',' '\n' | tail -1)
    log "batch album_failed=1 degraded_to=$newest skipped=$((count - 1))"
    send_photo "$newest" "$text"
}

# The numeric index of a frame: evt_20260903T101010Z_007.jpg -> 7.
frame_index() {
    printf '%s\n' "$1" | awk -F_ 'NF > 1 { n = $NF; sub(/\.jpg$/, "", n); print n + 0 }'
}

tamper_seen=0
for event in $events; do
    [ -n "$event" ] || continue

    marker="$STATE_DIR/sent-$event"

    # The highest frame index that has arrived, and the mtime of the newest of them — one awk for
    # both. This loop visits every event on the receiver on every pass, which was 99 of them on
    # 2026-09-03, so what it spends per event is the pass time: measured on the Pi, 1.45 s before
    # the stream existed and 2.3 s with this probe, against a POLL_INTERVAL_SEC of 15.
    #
    # A table built once for the whole listing and looked up here with parameter expansion was the
    # obvious way to have no probe at all, and it was measured at 13 s a pass — four times WORSE
    # than forking. `${table##*"<key>"}` rescans a 600-line string from the end for every event,
    # and shell string handling loses to a fork by two orders of magnitude on this box. Anything
    # cheaper than this has to iterate the listing once and drive the loop from it, which means
    # a subshell and losing what the loop accumulates.
    probe=$(printf '%s\n' "$listing" | awk -v e="$event" '
        BEGIN { newest = -1; mtime = 0 }
        $1 ~ "^"e"_[0-9]+\\.jpg$" {
            n = $1; sub(/^.*_/, "", n); sub(/\.jpg$/, "", n); n += 0
            if (n > newest) newest = n
            if ($3 + 0 > mtime) mtime = $3 + 0
        }
        END { printf "%d %d\n", newest, mtime }')
    newest_index=${probe% *}
    newest_mtime=${probe#* }

    # marker: <epoch of the last send> <highest frame index delivered>
    first_delivery=1
    sent_upto=-1
    if [ -f "$marker" ]; then
        first_delivery=0
        # `read` rather than `cut`: a builtin forks nothing, and it leaves the second field EMPTY
        # for a one-field marker, which is exactly the case below. `cut` had to be talked into
        # that with `-s` — without it, it prints the whole line when the delimiter is absent, so
        # a marker from before the stream read back as index 1787000000 and the event went silent
        # for good, because no frame could ever be "newer" than that.
        sent_upto=""
        read -r _marker_stamp sent_upto < "$marker" 2>/dev/null || true
        if [ -z "$sent_upto" ]; then
            # A marker written before the stream existed says the event was alerted but not which
            # frames went. Adopt what is on the receiver now and send nothing: deploying this must
            # not replay a whole afternoon into the chat.
            printf '%s %s\n' "$(date -u +%s)" "$newest_index" > "$marker"
            log "event id=$event adopted=1 upto=$newest_index"
            continue
        fi
        [ "$STREAM" = "1" ] || continue
        [ "$newest_index" -gt "$sent_upto" ] || continue
    fi

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
    # Where the event proper starts: the frames before it are the pre-buffer, the room BEFORE
    # anything happened. They stay on the receiver as proof it was empty; sending five
    # near-identical photographs of an empty room is not what "show me the whole visit" meant.
    prebuffer=$(printf '%s' "$manifest" | tr -d ' \n' | sed -n 's/.*"prebuffer":\([0-9]*\).*/\1/p')
    # The clip of this visit never reaches the receiver — it is a warehouse artefact by
    # configuration — so the manifest is the only way the chat can learn that it exists. Say so:
    # a photograph that arrives without mentioning the minute of video beside it is a photograph
    # nobody thinks to go looking behind. The numbers appear once the event closes and the
    # encoder is shut, so an alert sent mid-visit says nothing and its follow-ups do.
    clip_segments=$(printf '%s' "$manifest" | tr -d ' \n' |
        sed -n 's/.*"clip_segments":\([0-9]*\).*/\1/p')
    clip_bytes=$(printf '%s' "$manifest" | tr -d ' \n' |
        sed -n 's/.*"clip_bytes":\([0-9]*\).*/\1/p')
    clip_line=""
    if [ "${clip_segments:-0}" -gt 0 ] 2>/dev/null; then
        clip_line="
clip: ${clip_segments} segment(s), $(( ${clip_bytes:-0} / 1048576 )) MB — in the warehouse"
    fi
    : "${type:=motion}"
    : "${frames:=1}"

    now_epoch=$(date -u +%s)
    # How long since the newest frame of this event arrived. A visit that has stopped producing
    # frames is a visit whose tail must not sit waiting for a group it can never fill.
    newest_age=$((now_epoch - newest_mtime))
    [ "$newest_mtime" -gt 0 ] || newest_age=0

    # Frames of this event that have actually arrived, oldest first. `_000` is the OLDEST
    # pre-buffer frame — by construction the room a few seconds before anything happened.
    present=$(printf '%s\n' "$listing" | awk '{print $1}' | grep "^${event}_.*\.jpg$" | sort)

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
sound: $sound}$clip_line$cut_short$urgent"

    # Only a first delivery is a new alert. Later groups of the same visit are the alert
    # continuing, and counting them against the hourly cap would make the cap mean "photographs
    # per hour" instead of "events per hour" — one long visit would exhaust it on its own.
    if [ "$first_delivery" = "1" ] && [ "$type" != "tamper" ] &&
        ! motion_budget_left "$now_epoch"; then
        # Over budget: mark it seen so it is not re-examined, and count it for the summary.
        printf '%s %s\n' "$now_epoch" "$newest_index" > "$marker"
        motion_note_suppressed
        log "event id=$event type=$type suppressed=1 reason=hourly_cap"
        continue
    fi

    # The frame worth looking at, per the manifest. The manifest sorts ahead of the frames it
    # names, so it can arrive naming a frame that is still uploading; the old fallback then picked
    # `_000` and the alert showed an empty room. Measured in production: two of four events sent
    # that afternoon led with `_000`. Fall back to the NEWEST frame present instead — whatever is
    # happening, it is happening in the latest frame, not in the pre-buffer.
    lead="${key:-}"
    if [ -z "$lead" ] || ! printf '%s\n' "$present" | grep -q "^$lead$"; then
        lead=$(printf '%s\n' "$present" | tail -1)
        [ -n "$lead" ] || lead="${event}_000.jpg"
    fi

    # The uncompressed original of the key frame, for the copy that matters.
    send_original() {
        [ "$SEND_ORIGINAL" = "1" ] || return 0
        send_doc "$lead" "🔍 original, uncompressed — $lead" || log "original_failed name=$lead"
    }

    note_first_delivery() {
        [ "$type" = "tamper" ] && printf '%s\n' "$(date -u +%s)" > "$STATE_DIR/last-tamper"
        [ "$type" != "tamper" ] && motion_note_sent "$now_epoch"
        return 0
    }

    # --- the stream: every frame of the visit, in groups ---------------------------------------

    if [ "$STREAM" = "1" ]; then
        # Missing `prebuffer` means a manifest written by an agent older than this field. The key
        # frame is then the best available answer to "where does the run-up end", and it is the
        # one the old single-photo alert used for exactly that reason.
        stream_from="${prebuffer:-}"
        [ -n "$stream_from" ] || stream_from=$(frame_index "$lead")
        : "${stream_from:=0}"
        [ "$sent_upto" -ge "$((stream_from - 1))" ] || sent_upto=$((stream_from - 1))

        batches=0
        while [ "$batches" -lt "$STREAM_BATCHES_MAX" ]; do
            batch=""
            batch_last=$sent_upto
            batch_count=0
            for name in $present; do
                index=$(frame_index "$name")
                [ -n "$index" ] || continue
                [ "$index" -gt "$sent_upto" ] || continue
                [ "$batch_count" -lt "$STREAM_BATCH_MAX" ] || break
                [ -z "$batch" ] && batch="$name" || batch="$batch,$name"
                batch_last=$index
                batch_count=$((batch_count + 1))
            done

            if [ -z "$batch" ]; then
                # Nothing to send yet. On a first delivery that means the manifest arrived ahead
                # of its frames — worth saying at once, because the alert is the point and the
                # frames follow on their own.
                if [ "$first_delivery" = "1" ] && send_message "$caption"; then
                    printf '%s %s\n' "$(date -u +%s)" "$sent_upto" > "$marker"
                    note_first_delivery
                    log "event id=$event type=$type sent=1 photo=0"
                fi
                break
            fi

            # A follow-up waits until it can fill a group. The alert never waits, a closed event
            # flushes whatever is left, and a visit that stopped producing frames flushes on the
            # tail timer — the point is to group what is still coming, not to strand a tail.
            if [ "$first_delivery" = "0" ] && [ "$batch_count" -lt "$STREAM_MIN_BATCH" ] &&
                [ "$closed" != "true" ] && [ "$newest_age" -lt "$STREAM_TAIL_SEC" ]; then
                log "event id=$event held=$batch_count of $STREAM_MIN_BATCH age=${newest_age}s"
                break
            fi

            if [ "$first_delivery" = "1" ]; then
                text="$caption"
            else
                text="$icon $headline — continues
time: $(local_time "$(date -u +%s)")
frames: $((sent_upto + 1))-$batch_last of $frames$clip_line"
            fi

            if ! send_batch "$batch" "$text"; then
                log "event id=$event type=$type sent=0 from=$((sent_upto + 1))"
                break
            fi
            printf '%s %s\n' "$(date -u +%s)" "$batch_last" > "$marker"
            log "event id=$event type=$type frames=$batch_count upto=$batch_last stream=1 sent=1"
            if [ "$first_delivery" = "1" ]; then
                send_original
                note_first_delivery
                first_delivery=0
            fi
            sent_upto=$batch_last
            batches=$((batches + 1))
        done
        continue
    fi

    # --- STREAM=0: one photograph per event, the behaviour up to 2026-09-03 --------------------

    # Album: the key frame leads, then the newest frames. The pre-buffer proves the room was
    # empty before, which is worth keeping on the receiver but is not what the alert is about.
    album="$lead"
    if [ "$ALBUM_MAX" -gt 1 ]; then
        count=1
        for candidate in $(printf '%s\n' "$present" | sort -r); do
            [ "$candidate" = "$lead" ] && continue
            [ "$count" -lt "$ALBUM_MAX" ] || break
            album="$album,$candidate"
            count=$((count + 1))
        done
    fi

    lead_index=$(frame_index "$lead")
    : "${lead_index:=0}"

    if [ "$ALBUM_MAX" -gt 1 ] && [ "$album" != "$lead" ]; then
        if send_album "$album" "$caption"; then
            send_original
            printf '%s %s\n' "$(date -u +%s)" "$newest_index" > "$marker"
            note_first_delivery
            log "event id=$event type=$type frames=$frames key=$lead album=1 sent=1"
            continue
        fi
        log "event id=$event album_failed=1 falling back to single photo"
    fi

    if printf '%s\n' "$present" | grep -q "^$lead$"; then
        if send_photo "$lead" "$caption"; then
            send_original
            printf '%s %s\n' "$(date -u +%s)" "$lead_index" > "$marker"
            note_first_delivery
            log "event id=$event type=$type frames=$frames sent=1"
        else
            log "event id=$event type=$type sent=0"
            continue
        fi
    else
        if send_message "$caption"; then
            printf '%s %s\n' "$(date -u +%s)" "$sent_upto" > "$marker"
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
