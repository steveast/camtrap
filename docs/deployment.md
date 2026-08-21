# What runs where

Three machines, each with one job. The laptop sees and shouts, the receiver stores, the Pi tells
you. Nothing important depends on two of them at once: the siren needs no network, the frames
survive an unreachable receiver, and the token never leaves the Pi.

```
   LAPTOP (in the room)                RECEIVER (VPS, abroad)          PI (at home)
 ┌──────────────────────────┐        ┌───────────────────────┐      ┌────────────────────────┐
 │ guard            ← typed │        │ camtrap-recv.sh       │      │ camtrap-poll.sh        │
 │  └─ camtrap run          │        │  write-only, forced   │      │  cron: every minute    │
 │      ├─ camera 1080p/q95 │  ssh   │  put-frame/heartbeat  │      │                        │
 │      ├─ detector (MOG2)  │───────▶│                       │      │ reads: list, state,    │
 │      ├─ events + spool   │ 1 conn │ ~/camtrap/inbox/      │◀─────│        manifest        │
 │      ├─ tamper: AC, lid  │ reused │ ~/camtrap/state/      │ ssh  │ sends: send-album,     │
 │      ├─ player: 🔊       │        │                       │      │        send-doc,       │
 │      └─ uploader ──┐     │        │ camtrap-tg.sh         │◀─────│        send-message    │
 └────────────────────┼─────┘        │  read + relay to TG   │      │                        │
                      │ cp,          └───────────┬───────────┘      │ TELEGRAM_BOT_TOKEN     │
                      │ recompressed             │ curl              │  (only copy anywhere)  │
                      ▼ 720p/q75                 ▼                   └────────────────────────┘
              ~/MEGA/camtrap/            api.telegram.org
              (warehouse, no alerts)     → Steve's Servant → your chat
```

## Laptop

| What | Where | Notes |
|---|---|---|
| launcher you type | `~/MEGA/os/apps/guard` (on PATH) and `~/.local/bin/guard` | 7 KB bash; the copy in `.local/bin` survives an emptied cloud folder |
| agent entry point | `~/Dev/camtrap/.venv/bin/camtrap` | console script from `pip install -e .` |
| code | `~/Dev/camtrap/src/camtrap/` | ~490 KB, the repository is the source of truth |
| venv | `~/Dev/camtrap/.venv/` | opencv-python-headless, numpy |
| config | `~/.config/camtrap/config.toml` | overrides only; defaults live in `config.py` |
| sounds | `~/.local/share/camtrap/sounds/` | `siren.ogg`, `warn-vi.ogg`, `warn-en.ogg` |
| voice model | `~/.local/share/camtrap/voices/vi.onnx` | 61 MB, only needed to regenerate the warning |
| spool | `~/.local/share/camtrap/spool/` | frames waiting for acknowledgement; 1 GB cap |
| journal | `~/.local/share/camtrap/logs/camtrap.log` | written by the agent itself, not by the terminal |
| mode | `~/.local/share/camtrap/mode` | `armed` / `paused` |
| key to receiver | `~/.ssh/camtrap-laptop` | write-only: `put-frame`, `heartbeat`, `ping` and nothing else |
| cloud warehouse | `~/MEGA/camtrap/` | recompressed 720p/q75 copies, ~4 MB an event |

No systemd unit is enabled: you start it by hand. The unit exists in `deploy/systemd/` if that
ever changes.

## Receiver (VPS)

| What | Where |
|---|---|
| receiver for the laptop | `~/camtrap-recv.sh` (700) — forced command on the laptop's key |
| relay for the Pi | `~/camtrap-tg.sh` (700) — forced command on the Pi's key |
| frames and manifests | `~/camtrap/inbox/` — retention 14 days, ceiling 512 MB |
| heartbeat | `~/camtrap/state/heartbeat` |
| journal | `journalctl -t camtrap-recv` |

Two lines in `~/.ssh/authorized_keys`, both `restrict`. The laptop's key cannot list or read; the
Pi's key cannot write. Verified in both directions.

## Pi (at home)

| What | Where |
|---|---|
| poller | `/usr/local/bin/camtrap-poll.sh` (755 root) |
| schedule | `/etc/cron.d/camtrap-poll` — `* * * * * <poller user>`, its own file |
| token and settings | `/etc/camtrap-poll.env` (600, poller user) — the only copy of the bot token |
| state | `/var/lib/camtrap-poll/` — `sent-*`, `fail-*`, `last-armed`, `last-tamper`, `motion-alerts` |
| key to receiver | `~<poller user>/.ssh/camtrap-pi` — generated on the Pi, never travelled |
| journal | `journalctl -t camtrap-poll` |

The Pi runs other unrelated jobs; camtrap keeps its own cron file, state directory and journal tag.

## The daily cycle

1. **Leaving:** `guard`. Preflight refuses to arm if the camera, the sounds or the speakers fail.
   Then it waits for the room to be still for 10 s and arms behind you. **On arming it locks the
   screen and takes exclusive control of the power buttons**, so a single press can no longer
   suspend the machine — the desktop never sees the event.
2. **While away:** motion → spoken warning + frames + Telegram; power pulled, lid closed or case
   lifted → siren, session locked, 🚨 ahead of the queue. Heartbeat every 60 s.
3. **Coming back:** unlock the screen. That disarms for 5 minutes — picking the laptop up is
   silent — and hands the power buttons back, so you can switch the machine off as usual.
4. **Powering off:** just switch the machine off. The agent notices systemd is stopping, marks the
   offline as expected and publishes a last heartbeat, so no "agent went silent" arrives.

What still alerts, by design: the machine dying without a shutdown — battery pulled, power held
down, or the laptop carried off. That is the case worth waking up for.

## Reading the results

```sh
guard status      # mode, arming, spool, sound readiness
guard report      # events, sirens, warnings, refusals, drops
guard logs 200    # raw journal lines
journalctl -t camtrap-poll -n 50            # on the Pi: what was sent and when
journalctl -t camtrap-recv -n 50            # on the receiver: what arrived
```
