# Home Assistant control reference

> Mirror of `send_msg.py --help-controls`. The script is canonical — if these
> drift, the script wins.

## Number entities

Tunable, persist in NVS, live in HA's "Configuration" panel.

### Typing & timing

| Slug                   | Unit        | Range       | Default | Notes |
| ---------------------- | ----------- | ----------- | ------- | ----- |
| `typewriter_speed`     | chars/sec   | 1..200      | 20      | Higher = faster. Capped in practice by display refresh. |
| `message_hold_time`    | ms          | 500..60000  | 10000   | How long the fully-typed message stays on screen before clearing. Sticky alerts respect this too — after the hold and clear pause they re-type themselves instead of advancing to the next message. |
| `clear_pause`          | ms          | 0..30000    | 3000    | Blank-screen pause between consecutive messages. |

### Queue / message limits

| Slug                | Unit    | Range      | Default | Notes |
| ------------------- | ------- | ---------- | ------- | ----- |
| `queue_capacity`    | chunks  | 1..50      | 20      | Maximum chunks held simultaneously. Oldest GROUP evicted on overflow. |
| `split_threshold`   | chars   | 50..1000   | 200     | Long messages are split at word boundaries near this length. |
| `max_input_length`  | chars   | 100..8192  | 4096    | Hard cap on a single `add_message` call before any splitting. |
| `max_stored_chunk`  | chars   | 64..2048   | 512     | Ceiling on a single chunk after splitting. |

### Display layout

| Slug              | Unit  | Range    | Default | Notes |
| ----------------- | ----- | -------- | ------- | ----- |
| `visible_columns` | chars | 10..40   | 26      | Where the word-wrap engine breaks; should match the actual font. |
| `visible_rows`    | lines | 3..14    | 9       | Vertical scrollback in the visible region. |
| `line_height`     | px    | 12..40   | 24      | Vertical pixels per row. Should track the font size. |
| `glyph_width`     | px    | 6..24    | 12      | Cursor block width (text rendering itself uses font metrics). |
| `margin`          | px    | 0..20    | 4       | Padding from the screen edges to the text area. |

### CRT effects

| Slug              | Range  | Default | Notes |
| ----------------- | ------ | ------- | ----- |
| `background_glow` | 0..60  | 10      | Faint phosphor wash behind the text. 0 = pure black bg. Value scales the foreground colour by N/255 per channel. |
| `scanline_period` | 0..12 px | 4     | Distance between dithered black scan rows. 0 = scan lines off. |
| `bloom_intensity` | 0..100 % | 0     | Per-glyph halo (4 shifted dim copies). Has the highest per-frame cost — bump cautiously. |
| `vignette`        | 0..100 % | 0     | Bayer-dithered fade-to-black toward corners. 0 = off. |

### Ghost (CRT afterimage on scroll / clear)

| Slug              | Unit / Range | Default | Notes |
| ----------------- | ------------ | ------- | ----- |
| `ghost_duration`  | 0..3000 ms   | 550     | How long the ghost lingers before fully fading. 0 = ghost off. |
| `ghost_intensity` | 0..255       | 60      | Peak brightness of the ghost layer. |

### Colours (RGB, 0..255 each)

| Slug                              | Default      | Notes              |
| --------------------------------- | ------------ | ------------------ |
| `text_r` / `text_g` / `text_b`    | 0 / 242 / 0  | ~95% green         |
| `alert_r` / `alert_g` / `alert_b` | 255 / 176 / 0 | amber             |

### Idle matrix rain

| Slug           | Range    | Default | Notes |
| -------------- | -------- | ------- | ----- |
| `rain_speed`   | 10..300 %| 100     | Scales the per-column falling rate (base 1.5..3.5 rows/s). |
| `rain_density` | 10..100 %| 100     | Column coverage. 100 = every column, 50 = every other, 25 = every 4th. |

### Tap input

| Slug                | Unit | Range      | Default | Notes |
| ------------------- | ---- | ---------- | ------- | ----- |
| `tap_threshold`     | m/s² | 1.0..30.0  | 3.5     | Accel magnitude above 1G rest that registers as a tap. Lower for lighter taps; raise if handling false-triggers. |
| `tap_refractory_ms` | ms   | 30..250    | 80      | Minimum gap between accepted taps so one physical impulse (≈30–80 ms) doesn't register multiple times. Must stay below the 350 ms discriminator window. |

## Other entities

- **Light: "Backlight"** (monochromatic) — LCD backlight on/off +
  brightness. Restore mode: `ALWAYS_ON`.
- **Light: "Status LEDs"** (`esp32_rmt_led_strip`, GPIO16, 3 ×
  WS2812B) — 3 addressable LEDs auto-driven by the alert dispatcher
  (see [help-leds.md](help-leds.md) for the marker → colour/effect
  table). Manual override via HA works until the next alert state
  change.
- **Switch: "Sleep when idle"** (slug `sleep_when_idle`) — when on,
  the screen goes fully black and the LCD backlight is faded off
  whenever the message queue is empty. Send any message and the
  backlight fades back on and typing resumes normally. Pure
  power-saver — settings and state are preserved.
- **Tap input** — tap the device case (or screen) to skip to the next
  message; double-tap to clear the currently-displayed message group.
  The QMI8658 IMU feeds the firmware a magnitude spike whenever the
  case is tapped (threshold + 80 ms refractory). Screen taps via the
  CST816 panel are a secondary path into the same discriminator. The
  single-tap resolves ~350 ms after the last tap so it can be told
  apart from a double-tap. Tunable via the "Tap threshold" (m/s²
  above 1G) and "Tap refractory" (ms) number entities.

## Inspect / set from the CLI

```sh
send_msg.py --list-numbers              # current value + range of every entity
send_msg.py --set <slug> <value>        # set one entity
send_msg.py --help-api                  # ingestion API reference
send_msg.py --help-leds                 # status LED behaviour
```
