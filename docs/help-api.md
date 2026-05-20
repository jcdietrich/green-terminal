# API reference

> Mirror of `send_msg.py --help-api`. The script is canonical — if these
> drift, the script wins.

## Ingestion — ways to push text onto the device

ESPHome native API actions (called via `send_msg.py` or Home Assistant):

### `add_message(msg_text: string, footer_text: string = "")`
Push a normal (green) message onto the queue. Long messages are split
at word boundaries; chunks of the same source are typed continuously
without inter-chunk pause and count as one group. If `footer_text` is
non-empty it replaces the default x/N counter in the bottom-centre
footer for this message.

```sh
send_msg.py "your text"
send_msg.py "Heads up" --footer "CPU: 90%"
echo "your text" | send_msg.py
```

HA: `service: green_terminal.add_message` · `data: { msg_text: "...", footer_text: "" }`

### `add_alert(msg_text: string, footer_text: string = "")`
Same as `add_message` but flagged as an alert — rendered in the
configured alert colour (default amber 255/176/0).

```sh
send_msg.py --alert "warning text"
send_msg.py --alert "CPU pegged" --footer "node-7"
```

HA: `service: green_terminal.add_alert`

### `add_sticky_alert(msg_text: string, footer_text: string = "")`
Alert that re-loops on itself indefinitely instead of advancing to the
next queued message. After typing out the message, it holds for
`message_hold_time`, clears the screen, holds for `clear_pause`, and
starts re-typing the same message. Works for messages longer than one
screen — they fully type out (scrolling as needed) then loop. While
sticky is on screen the footer is rendered as `- x/N -` with
surrounding dashes. Removed via `--clear`, `--clear N`, or
`clear_alerts`.

```sh
send_msg.py --sticky "OVERHEATING"
```

HA: `service: green_terminal.add_sticky_alert`

### `clear_queue`
Empty the whole queue immediately.

```sh
send_msg.py --clear
```

### `clear_indices(indices: string)`
Remove specific groups by their 1-based position (the `x` of `x/N`).
The indices string is space- or comma-separated; the device sorts
them descending before removal so numbering stays stable.

```sh
send_msg.py --clear 3 5 7
```

HA: `service: green_terminal.clear_indices` · `data: { indices: "3 5 7" }`

### `clear_alerts`
Remove every alert-flagged message; leaves normal messages alone.

```sh
send_msg.py --clear-alerts
```

### `reset_leds`
Turn the status LED strip off right now AND drop the saved HA baseline
back to off. After this call the strip stays dark until either HA
sets it again or an alert fires; future alert exits will go to off
instead of restoring the previous HA state.

```sh
send_msg.py --reset-leds
```

HA: `service: green_terminal.reset_leds`

### `skip`
Advance to the next message immediately (skips the post-message and
clear pauses).

```sh
send_msg.py --skip
```

### `dump_state`
Log the persisted JSON to the device console. The CLI reassembles the
log chunks back into a single JSON payload on stdout.

```sh
send_msg.py --dump-state           # raw JSON
send_msg.py --dump-state | jq      # pretty-printed
```

### MQTT topics

Broker configured in `secrets.yaml`; payload = message text.

| Topic                          | Effect                                  |
| ------------------------------ | --------------------------------------- |
| `green-terminal/enqueue`       | Normal message                          |
| `green-terminal/alert`         | Alert message                           |
| `green-terminal/sticky-alert`  | Sticky alert (pins cycle until cleared) |

```sh
mosquitto_pub -h <broker> -t green-terminal/enqueue -m "hello"
```

## Inline formatting

- `*emphasis*` — text wrapped in asterisks blinks at the cursor-blink
  rate with a fade trail. Asterisks themselves are hidden once the
  closing one arrives.
- `:name:` — glyph shortcut. Expanded server-side before split/store
  so the queue contains the actual glyph. Run
  `send_msg.py --list-shortcuts` to see every shortcut, its codepoint,
  and a preview character.

## Configuration — runtime tunables (HA number entities)

Every visual / timing knob is a `number` entity, persisted in NVS.
List them with current values + ranges:

```sh
send_msg.py --list-numbers
```

Set one (`object_id` from `--list-numbers`):

```sh
send_msg.py --set typewriter_speed 50      # chars/sec
send_msg.py --set background_glow 10       # 0..60
send_msg.py --set vignette 30              # 0..100
send_msg.py --set rain_speed 150           # idle rain
send_msg.py --set alert_r 255              # alert colour RGB
```

See [help-controls.md](help-controls.md) for the full list.

## Idle behaviour

When the queue is empty the display falls back to a Matrix-style rain
of hex glyphs in the configured text colour. Send any message to take
over; clear the queue to return to rain.

## Persistence

Queue contents survive reboot. Saved as JSON in NVS under the `term`
namespace, key `state_json`. Format:

```json
{
  "q": ["chunk1", "chunk2", "..."],
  "g": [0, 1, 1, 0],
  "a": [0, 1, 1, 0],
  "s": [0, 0, 0, 1],
  "f": ["", "", "CPU: 42%", ""],
  "n": 7
}
```

- `q` — source chunks (after `:name:` expansion)
- `g` — group ids (0 = standalone, >0 = split-group)
- `a` — alert flags (parallel to `q`)
- `s` — sticky flags (parallel to `q`)
- `f` — footer override strings (parallel to `q`)
- `n` — next_group_id counter

Inspect any time with `send_msg.py --dump-state`.
