# Project overview

> Mirror of `send_msg.py --help-summary`. The script is canonical — if these
> drift, the script wins.

A Waveshare ESP32-S3 2-inch IPS LCD board running an ESPHome firmware
that imitates an old green-phosphor terminal. Anything sent to the
device is typed out one character at a time, line-wraps where it
should, scrolls when the screen fills, lingers for a beat, fades to
black, and then moves on to the next thing in the queue. The
typewriter cursor blinks while it waits, with a fade trail. When the
queue is empty the screen falls back to a Matrix-style rain of hex
glyphs in whatever colour you've set for normal text.

Messages reach the device three ways: a native ESPHome API action
(callable from Home Assistant or from this script over the encrypted
API on port 6053), an MQTT topic for both normal and alert messages,
and indirectly via Home Assistant if you've adopted the device. A
companion Python script (`send_msg.py`) wraps the API for the command
line — sending a message is as simple as `send_msg.py "hello"` or
piping text into stdin. The device-side script clamps, normalises, and
splits the incoming text: anything past `split_threshold` characters
is sliced at word boundaries into chunks, all of which carry the same
group id. Chunks in the same group type continuously without the
between-message pause, so a long paragraph reads as one continuous
thought rather than a series of fragments. Standalone messages get
group id 0. The footer in the bottom-centre always shows
`current_group / total_groups` so you know where you are in the cycle.

Three flavours of message exist: normal (rendered in a configurable
green by default), alert (rendered in a configurable amber), and
sticky alert. A sticky alert types out fully (scrolling if more than
one screen), holds for the configured `message_hold_time`, clears,
and then re-types the same message — repeating indefinitely until
removed via `--clear N` (the group's number from the x/N footer),
`--clear-alerts` (all alerts), or `--clear` (everything). While a
sticky message is on screen the footer is rendered with surrounding
dashes (`- x/N -`) so it's easy to see at a glance that the cycle is
pinned. The alert colour, normal text colour, and a dozen other
display knobs are exposed as Home Assistant `number` entities under
the device's Configuration panel — anything from typing speed to
scanline density to vignette strength can be tweaked live. Settings
persist in NVS across reboots. So does the message queue itself: when
the device boots it reloads whatever was in the queue at the last
save, encoded as a small JSON blob. Inspect it with `--dump-state`.

A handful of CRT-era visual touches layer on top. A faint phosphor
wash tints the entire background in the current foreground colour. A
stippled scanline pattern dims every Nth row. An optional
Bayer-dithered vignette pulls corners toward black. An optional bloom
pass puts a halo around each glyph. The block cursor and any text
wrapped in `*asterisks*` blink at the same rate, each with a short
fade-out trail. Scrolled-off lines and full clears leave a brief
afterimage ("ghost") in the same colour, dimmer, that decays smoothly.
The cumulative effect is intentionally soft — closer to a Wyse or an
early VT100 than a sharp modern OLED.

Glyph coverage is handled by a Mono variant of RobotoMono Nerd Font.
Beyond ASCII and the usual Latin-1, the font carries 10,000+ Nerd
Font icons in the Private Use Area (FontAwesome, Material Design,
Codicons, Devicons, etc.), all constrained to a single cell width.
To keep messages legible to a human typing them, the firmware
expands `:name:` tokens (`:warning:`, `:heart:`, `:smile:`, `:bell:`,
all the box-drawing characters, smart punctuation, etc.) into their
real glyphs before the message is stored. Run `--list-shortcuts` to
see every supported token. Inline `*emphasis*` blinks. Pasting a real
UTF-8 character also works as long as that codepoint is in the font's
glyph list (extend with the `extras:` block in the YAML).

A short string of WS2812B addressable LEDs (3 pixels, wired to
GPIO16) sits alongside the screen and is driven automatically by the
alert dispatcher. Plain alerts pulse amber; alerts containing a
`:warning:`, `:check:`, `:x:`, `:bolt:`, `:heart:`, or `:nf-heart:`
glyph trigger a specific colour + effect combination — see
[help-leds.md](help-leds.md) for the table. Sticky alerts hold the
corresponding effect for the whole time they pin the cycle. The strip
also shows up as a "Status LEDs" light entity in HA for manual
override.

A "Sleep when idle" switch — exposed both in HA and via
`send_msg.py --switch sleep_when_idle on` — turns this into a strict
power-saver: queue empty + switch on → screen blanks fully and the
LCD backlight fades off until something new arrives.

The device has no built-in speaker; adding sound would mean wiring a
piezo buzzer (cheap, basic beeps) or an I²S DAC + speaker (real
audio).

## Deeper references

- [help-api.md](help-api.md) — ingestion API + MQTT topics
- [help-controls.md](help-controls.md) — every Home Assistant tunable
- [help-leds.md](help-leds.md) — status-LED behaviour + effects
- `send_msg.py --list-shortcuts` — every `:name:` glyph token
- `send_msg.py --list-numbers` — live values of every tunable
- `send_msg.py --dump-state` — the JSON currently in NVS
