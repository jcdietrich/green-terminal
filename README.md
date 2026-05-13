# green-terminal

ESPHome firmware for a Waveshare ESP32-S3 2-inch IPS LCD that turns the
board into an imitation green-phosphor terminal. Anything sent to the
device is typed out one character at a time, line-wraps where it
should, scrolls when the screen fills, lingers for a beat, fades to
black, and moves on to the next thing in the queue. When the queue is
empty the screen falls back to a Matrix-style rain of hex glyphs.

## Showcase

All clips are rendered by [`tools/sim.py`](tools/sim.py), an off-device
simulator that mirrors the firmware's display pipeline exactly
(background phosphor wash, 4-direction bloom, scanline stipple,
Bayer-dithered vignette, ghost afterimage, blinking cursor with fade
trail). Hold and clear-pause durations are shortened so the loops
stay punchy.

| Scenario | Demo |
| -------- | ---- |
| Short message: types out, holds, clears with a ghost afterimage, returns to a blinking home cursor. | <video src="docs/img/basic.mp4" controls muted loop playsinline></video> |
| Long message: wraps at 26 columns and scrolls inside the 9-row visible window. | <video src="docs/img/long.mp4" controls muted loop playsinline></video> |
| Amber alert: same flow, alert colour, `:x:` glyph expanded server-side. | <video src="docs/img/alert.mp4" controls muted loop playsinline></video> |
| Sticky alert: footer shows `- 1/1 -` and the message re-types itself indefinitely until cleared. | <video src="docs/img/sticky.mp4" controls muted loop playsinline></video> |
| Idle matrix rain: hex glyphs fall in per-column streams with bright heads and fading tails; a message arrives, plays out, then rain resumes. | <video src="docs/img/rain.mp4" controls muted loop playsinline></video> |

> Render or re-render locally with `python tools/sim.py --all`. Requires
> `Pillow`, `numpy`, and `ffmpeg` on PATH.

## Hardware

- **Board:** Waveshare ESP32-S3-Touch-LCD-2 (2.0" IPS 240×320, ST7789T3
  + CST816D touch — touch is wired but currently unused)
- **LEDs:** 3 × WS2812B addressable, on GPIO16, used as a status strip
  by the alert dispatcher
- **No speaker** — beeps would need a piezo on a spare GPIO; real audio
  would need an I²S DAC

## Setup

1. Clone:
   ```sh
   git clone https://github.com/jcdietrich/green-terminal.git
   cd green-terminal
   ```

2. Create a `secrets.yaml` alongside `green-terminal.yaml` (a symlink to
   your usual ESPHome secrets file is fine). At minimum it needs:
   ```yaml
   wifi_ssid: "..."
   wifi_password: "..."
   api_key: "..."          # base64 NOISE PSK — used by HA and send_msg.py
   ota_password: "..."
   mqtt_broker: "..."
   mqtt_username: ""
   mqtt_password: ""
   ```
   `secrets.yaml` is gitignored.

3. Compile & flash with ESPHome (USB the first time, OTA after that):
   ```sh
   esphome run green-terminal.yaml
   ```

4. (Optional) Install the Python sender's one dependency:
   ```sh
   pip install aioesphomeapi
   ```

## Sending messages

Three paths in: the native ESPHome API (HA service or `send_msg.py`),
MQTT, or Home Assistant once the device is adopted.

```sh
# Normal messages
send_msg.py "hello"
echo "hello from stdin" | send_msg.py
cat poem.txt | send_msg.py

# Alerts
send_msg.py --alert "Boom"                 # amber
send_msg.py --sticky "OVERHEATING"         # pins the cycle until cleared

# Queue control
send_msg.py --skip                         # advance to next item
send_msg.py --clear                        # empty the whole queue
send_msg.py --clear-alerts                 # remove just alerts
send_msg.py --reset-leds                   # status LEDs off + drop baseline

# Live tuning
send_msg.py --list-numbers                 # every HA-tunable knob
send_msg.py --set background_glow 10       # set one
send_msg.py --list-shortcuts               # every `:name:` glyph token
send_msg.py --dump-state                   # the JSON currently in NVS
```

`send_msg.py` reads `ADDR`/`PORT` from the top of the script and pulls
the NOISE PSK from `secrets.yaml` (`api_key`). Override `ADDR` if your
device's IP differs.

### MQTT

```
topic: green-terminal/enqueue        # payload = message text
topic: green-terminal/alert          # payload = alert text (amber)
```

Full topic list: `send_msg.py --help-api`.

### Home Assistant

Once adopted, the device exposes:

- A `green_terminal_add_message` action (also `add_alert`, `add_sticky`,
  `skip`, `clear`, etc.)
- A `number.*` entity per tunable (typing speed, scanline density,
  vignette strength, alert colour, …) — all live, all persisted to NVS
- A `light.status_leds` entity for manual LED override
- A `switch.sleep_when_idle` — when on, an empty queue blanks the
  screen and fades the backlight off

## Further reading

The same content is available both inside the script (`send_msg.py
--help-*`) and as standalone docs:

| Doc                                            | Script flag        | Covers                                  |
| ---------------------------------------------- | ------------------ | --------------------------------------- |
| [docs/help-summary.md](docs/help-summary.md)   | `--help-summary`   | Prose project overview                  |
| [docs/help-api.md](docs/help-api.md)           | `--help-api`       | Ingestion API + MQTT topics             |
| [docs/help-controls.md](docs/help-controls.md) | `--help-controls`  | Every HA-exposed tunable                |
| [docs/help-leds.md](docs/help-leds.md)         | `--help-leds`      | Status-LED behaviour + effects          |

The script also has two runtime-computed reference commands:

```sh
send_msg.py --list-shortcuts     # every `:name:` glyph token
send_msg.py --list-numbers       # live values of every tunable
```

## Repo layout

```
green-terminal.yaml         ESPHome config (main entry point)
includes/queue_storage.h    NVS-backed cyclic queue
fonts/                      RobotoMono Nerd Font (Mono variant, Bold)
send_msg.py                 CLI sender / queue controller
secrets.yaml                gitignored — see Setup
```
