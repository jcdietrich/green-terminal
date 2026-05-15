#!/usr/bin/env python3
"""Send a message to the green-terminal ESP32-S3 over the ESPHome native API.

Usage:
    python send_msg.py "Hello, World"            # arg form
    echo "Hello, World" | python send_msg.py     # stdin form
    cat poem.txt | python send_msg.py            # piped file
    python send_msg.py --alert "Boom"            # alert (amber by default)
    echo "Boom" | python send_msg.py --alert     # alert via stdin
    python send_msg.py --sticky "OVERHEATING"    # sticky alert (pins cycle)
    python send_msg.py --skip                    # skip to next item
    python send_msg.py --clear                   # empty the whole queue
    python send_msg.py --clear-alerts            # remove only alert messages
    python send_msg.py --reset-leds              # turn off status LEDs + drop saved baseline
    python send_msg.py --set background_glow 10  # set a number entity
    python send_msg.py --list-numbers            # list tunable entities
    python send_msg.py --list-shortcuts          # list :name: glyph shortcuts
    python send_msg.py --dump-state              # print the persisted queue JSON
    python send_msg.py --help-api                # full API reference
    python send_msg.py --help-controls           # full HA controls reference
    python send_msg.py --help-summary            # prose project overview
    python send_msg.py --help-leds               # status LED behaviour

Reads from stdin when not a TTY (so cat / heredocs / shell pipes all work)
and falls back to argv[1] otherwise. Concatenates multiple argv positional
arguments with single spaces.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path
from aioesphomeapi import APIClient, LogLevel

ADDR = "192.168.86.25"
PORT = 6053


def _load_noise_psk() -> str:
    secrets_path = Path(__file__).resolve().parent / "secrets.yaml"
    for line in secrets_path.read_text().splitlines():
        m = re.match(r'^\s*api_key\s*:\s*"([^"]+)"\s*$', line)
        if m:
            return m.group(1)
    sys.exit(f"ERROR: api_key not found in {secrets_path}")


NOISE_PSK = _load_noise_psk()


def read_message_from_args(args: argparse.Namespace) -> str:
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped:
            return piped.rstrip("\n")
    if args.text:
        return " ".join(args.text)
    return ""


async def call(action_name: str, payload: dict) -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    try:
        _, services = await cli.list_entities_services()
        svc = next((s for s in services if s.name == action_name), None)
        if svc is None:
            print(f"ERROR: action '{action_name}' not found on device "
                  f"(available: {[s.name for s in services]})",
                  file=sys.stderr)
            return 1
        await cli.execute_service(svc, payload)
        await asyncio.sleep(0.3)
    finally:
        await cli.disconnect()
    return 0


async def dump_state() -> int:
    """Call the dump_state action, collect log chunks, print the JSON."""
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    chunks: list[str] = []
    done = asyncio.Event()
    collecting = False

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")

    def on_log(msg):
        nonlocal collecting
        try:
            text = msg.message.decode("utf-8", "replace")
        except AttributeError:
            text = str(msg.message)
        if "term:state" not in text:
            return
        # Strip the ESPHome log prefix (`[I][term:state:NNN]: `) and any
        # ANSI colour escapes the logger wraps the line in.
        idx = text.find("]: ")
        payload = text[idx + 3:] if idx >= 0 else text
        payload = ansi_re.sub("", payload).rstrip("\r\n")
        if payload.startswith("<<<DUMP"):
            chunks.clear()
            collecting = True
            return
        if payload.startswith("<<<END>>>"):
            collecting = False
            done.set()
            return
        if collecting:
            chunks.append(payload)

    cli.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_INFO)
    try:
        _, services = await cli.list_entities_services()
        svc = next((s for s in services if s.name == "dump_state"), None)
        if svc is None:
            print("ERROR: dump_state action not found", file=sys.stderr)
            return 1
        # Let the log subscription land before firing the action.
        await asyncio.sleep(0.3)
        await cli.execute_service(svc, {})
        try:
            await asyncio.wait_for(done.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            print("ERROR: dump_state log not received within 3s",
                  file=sys.stderr)
            return 1
    finally:
        await cli.disconnect()
    print("".join(chunks))
    return 0


COLOR_NAMES = {
    "red":     (1.00, 0.00, 0.00),
    "green":   (0.00, 1.00, 0.00),
    "blue":    (0.00, 0.00, 1.00),
    "yellow":  (1.00, 1.00, 0.00),
    "amber":   (1.00, 0.69, 0.00),
    "orange":  (1.00, 0.50, 0.00),
    "purple":  (0.50, 0.00, 0.50),
    "pink":    (1.00, 0.40, 0.70),
    "cyan":    (0.00, 1.00, 1.00),
    "magenta": (1.00, 0.00, 1.00),
    "white":   (1.00, 1.00, 1.00),
}


def parse_color(s: str):
    """red | 255,128,0 | #ff8000 → (r, g, b) 0..1 floats."""
    s = s.strip().lower()
    if s in COLOR_NAMES:
        return COLOR_NAMES[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 6:
            return (int(h[0:2], 16) / 255,
                    int(h[2:4], 16) / 255,
                    int(h[4:6], 16) / 255)
    if "," in s:
        try:
            r, g, b = [int(p.strip()) for p in s.split(",")]
            return (r / 255, g / 255, b / 255)
        except ValueError:
            pass
    raise ValueError(f"can't parse color {s!r} — "
                     f"use a name ({', '.join(COLOR_NAMES)}), "
                     "R,G,B integers, or #rrggbb hex")


async def set_switch(object_id: str, state: bool) -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    try:
        entities, _ = await cli.list_entities_services()
        switches = {e.object_id: e for e in entities
                    if type(e).__name__ == "SwitchInfo"}
        target = switches.get(object_id)
        if target is None:
            print(f"ERROR: switch '{object_id}' not found", file=sys.stderr)
            print(f"  available: {sorted(switches.keys())}", file=sys.stderr)
            return 1
        cli.switch_command(target.key, state)
        await asyncio.sleep(0.3)
        print(f"{object_id}: {'on' if state else 'off'}", file=sys.stderr)
    finally:
        await cli.disconnect()
    return 0


async def list_switches() -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    states = {}
    cli.subscribe_states(lambda s: states.update({s.key: s.state}))
    try:
        entities, _ = await cli.list_entities_services()
        await asyncio.sleep(0.6)
        switches = [e for e in entities if type(e).__name__ == "SwitchInfo"]
        if not switches:
            return 0
        widest = max(len(s.object_id) for s in switches)
        for s in sorted(switches, key=lambda x: x.object_id):
            v = states.get(s.key)
            vstr = "on" if v else "off" if v is False else "?"
            print(f"  {s.object_id:<{widest}}  {vstr:<4}  ({s.name})")
    finally:
        await cli.disconnect()
    return 0


async def set_light(object_id: str, *, state=None, brightness=None,
                    rgb=None, effect=None) -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    try:
        entities, _ = await cli.list_entities_services()
        lights = {e.object_id: e for e in entities
                  if type(e).__name__ == "LightInfo"}
        target = lights.get(object_id)
        if target is None:
            print(f"ERROR: light '{object_id}' not found", file=sys.stderr)
            print(f"  available: {sorted(lights.keys())}", file=sys.stderr)
            return 1
        kwargs = {"key": target.key}
        if state is not None:        kwargs["state"] = state
        if brightness is not None:   kwargs["brightness"] = brightness
        if rgb is not None:          kwargs["rgb"] = rgb
        if effect is not None:       kwargs["effect"] = effect
        cli.light_command(**kwargs)
        await asyncio.sleep(0.3)
        changed = ", ".join(f"{k}={v}" for k, v in kwargs.items() if k != "key")
        print(f"{object_id}: {changed}", file=sys.stderr)
    finally:
        await cli.disconnect()
    return 0


async def list_lights() -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    states = {}
    cli.subscribe_states(lambda s: states.update({s.key: s}))
    try:
        entities, _ = await cli.list_entities_services()
        await asyncio.sleep(0.6)
        lights = [e for e in entities if type(e).__name__ == "LightInfo"]
        if not lights:
            print("(no lights exposed)", file=sys.stderr)
            return 0
        for l in lights:
            st = states.get(l.key)
            on = bool(st.state) if st and hasattr(st, "state") else "?"
            br = f"{st.brightness:.2f}" if st and getattr(st, "brightness", None) is not None else "?"
            r  = getattr(st, "red", None) if st else None
            g  = getattr(st, "green", None) if st else None
            b  = getattr(st, "blue", None) if st else None
            eff = getattr(st, "effect", None) if st else None
            rgb = (f"({r:.2f},{g:.2f},{b:.2f})"
                   if r is not None else "(?,?,?)")
            effs = ", ".join(l.effects) if l.effects else "(none)"
            print(f"{l.object_id}")
            print(f"  name:        {l.name}")
            print(f"  state:       on={on}  brightness={br}  rgb={rgb}  effect={eff!r}")
            print(f"  effects:     {effs}")
    finally:
        await cli.disconnect()
    return 0


async def set_number(object_id: str, value: float) -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    try:
        entities, _ = await cli.list_entities_services()
        nums = {
            e.object_id: e for e in entities
            if type(e).__name__ == "NumberInfo"
        }
        target = nums.get(object_id)
        if target is None:
            print(f"ERROR: number entity '{object_id}' not found",
                  file=sys.stderr)
            print(f"  available: {sorted(nums.keys())}", file=sys.stderr)
            return 1
        if not (target.min_value <= value <= target.max_value):
            print(f"ERROR: {value} out of range "
                  f"[{target.min_value}, {target.max_value}]",
                  file=sys.stderr)
            return 1
        cli.number_command(target.key, value)
        await asyncio.sleep(0.3)
        print(f"set {object_id} → {value}", file=sys.stderr)
    finally:
        await cli.disconnect()
    return 0


def help_leds() -> int:
    """Print a reference for the WS2812B status-LED strip behaviour."""
    text = """\
green-terminal — status LED reference

═══════════════════════════════════════════════════════════════════════
HARDWARE
═══════════════════════════════════════════════════════════════════════

  3 × WS2812B addressable LEDs wired to GPIO16 (Din), powered from the
  board's 5V rail and sharing ground. Exposed in Home Assistant as the
  "Status LEDs" light entity — manual on/off/colour/brightness/effect
  control works any time. When an alert begins the dispatcher takes
  over, snapshots whatever HA had the strip set to, applies the alert
  effect, and restores the snapshot when the alert finishes. The
  snapshot survives across multiple alerts in a row; it's only reset
  by `--reset-leds` or `--clear` (queue clear), which both put the
  strip dark and drop the saved baseline to off.

═══════════════════════════════════════════════════════════════════════
ALERT-DRIVEN BEHAVIOUR
═══════════════════════════════════════════════════════════════════════

Whenever an alert (regular or sticky) is the currently-displayed
message on the terminal, the strip auto-selects a colour + effect
based on the FIRST trigger glyph found in the chunk text. If no
trigger is found, the default amber pulse is used. When the cycle
returns to a non-alert message — or the queue empties — the strip
turns off.

  Marker       Trigger glyph          Colour           Effect
  ──────────── ────────────────────── ──────────────── ───────────────
  (none)       —                      amber            pulse
  :warning:    U+F071 fa-warning      amber            scan
  :nf-heart:   U+F004 fa-heart        red              pulse
  :heart:      U+2665 ♥               red              pulse
  :x:          U+F00D fa-times        red              scan
  :check:      U+F00C fa-check        green            scan
  :bolt:       U+26A1 ⚡              cool blue-white  flicker

  First marker in the text wins on conflict — reorder glyphs to choose
  which one fires the effect for messages that include several.

═══════════════════════════════════════════════════════════════════════
EFFECT TUNING
═══════════════════════════════════════════════════════════════════════

  pulse                              addressable_scan (scan)
    transition_length:  1000ms        move_interval:  240ms
    update_interval:    1000ms        scan_width:     1
    min/max brightness: 25% / 100%

  addressable_flicker (flicker)
    update_interval:    16ms
    intensity:          80%
    bolt baseline:      25% brightness (overrides default 100%)

═══════════════════════════════════════════════════════════════════════
ALERT EXAMPLES
═══════════════════════════════════════════════════════════════════════

  send_msg.py --alert "fire alarm tripped"            # amber pulse
  send_msg.py --alert ":bolt: power dip on circuit 3" # blue-white flicker
  send_msg.py --alert ":check: backup complete"       # green scan
  send_msg.py --alert ":x: build failed"              # red scan
  send_msg.py --alert ":warning: low battery"         # amber scan
  send_msg.py --sticky ":heart: I love you"           # red pulse, pinned

Sticky alerts respect the same trigger table — a sticky :bolt: will
flicker blue-white the entire time it pins the cycle.

═══════════════════════════════════════════════════════════════════════
MANUAL CONTROL (CLI ⇄ Home Assistant)
═══════════════════════════════════════════════════════════════════════

The strip is exposed as the "Status LEDs" light entity. Anything HA
sets propagates straight to the device; anything the CLI sets is
broadcast back to HA. They share one source of truth.

  send_msg.py --list-lights                # show every light + its state + effects

  send_msg.py --led on                     # turn on (last colour/effect kept)
  send_msg.py --led off                    # turn off
  send_msg.py --led-color red              # by name
  send_msg.py --led-color "255,128,0"      # by RGB triple
  send_msg.py --led-color "#00ffaa"        # by hex
  send_msg.py --led-brightness 60          # 0..100 percent
  send_msg.py --led-effect pulse           # or None / scan / flicker
  send_msg.py --led-color cyan --led-brightness 80 --led-effect scan  # combined

  Color names: red green blue yellow amber orange purple pink cyan magenta white

═══════════════════════════════════════════════════════════════════════
SNAPSHOT / RESTORE LIFECYCLE
═══════════════════════════════════════════════════════════════════════

Manual settings (from CLI or HA) persist while no alert is on screen.
When an alert arrives the dispatcher snapshots the current colour /
brightness / effect, applies the alert effect, then restores the
snapshot at alert end. Chain multiple alerts together and the
snapshot survives until the cycle finally returns to non-alert
content.

  send_msg.py --reset-leds                 # off + drop the snapshot (default → off)
  send_msg.py --clear                      # also drops the snapshot

So a typical "set ambient + let alerts override" flow is:

  send_msg.py --led-color "30,30,80" --led-brightness 30   # dim blue ambient
  send_msg.py --alert ":bolt: power blip"                  # flickers white-blue
  # ... alert finishes; LEDs return to dim blue ambient
  send_msg.py --reset-leds                                  # done for the night
"""
    print(text)
    return 0


def help_summary() -> int:
    """Print a prose overview of the project."""
    text = """\
green-terminal — overview

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
dashes ("- x/N -") so it's easy to see at a glance that the cycle is
pinned. The alert colour, normal text
colour, and a dozen other display knobs are exposed as Home Assistant
`number` entities under the device's Configuration panel — anything
from typing speed to scanline density to vignette strength can be
tweaked live. Settings persist in NVS across reboots. So does the
message queue itself: when the device boots it reloads whatever was
in the queue at the last save, encoded as a small JSON blob. Inspect
it with `--dump-state`.

A handful of CRT-era visual touches layer on top. A faint phosphor
wash tints the entire background in the current foreground colour. A
stippled scanline pattern dims every Nth row. An optional Bayer-
dithered vignette pulls corners toward black. An optional bloom pass
puts a halo around each glyph. The block cursor and any text wrapped
in `*asterisks*` blink at the same rate, each with a short fade-out
trail. Scrolled-off lines and full clears leave a brief afterimage
("ghost") in the same colour, dimmer, that decays smoothly. The cumulative
effect is intentionally soft — closer to a Wyse or an early VT100
than a sharp modern OLED.

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
`--help-leds` for the table. Sticky alerts hold the corresponding
effect for the whole time they pin the cycle. The strip also shows up
as a "Status LEDs" light entity in HA for manual override.

A "Sleep when idle" switch — exposed both in HA and via
`send_msg.py --switch sleep_when_idle on` — turns this into a strict
power-saver: queue empty + switch on → screen blanks fully and the
LCD backlight fades off until something new arrives.

The device has no built-in speaker; adding sound would mean wiring a
piezo buzzer (cheap, basic beeps) or an I²S DAC + speaker (real
audio).

For deeper references:
  send_msg.py --help-api            ingestion API + MQTT topics
  send_msg.py --help-controls       every Home Assistant tunable
  send_msg.py --help-leds           status-LED behaviour + effects
  send_msg.py --list-shortcuts      every `:name:` glyph token
  send_msg.py --list-numbers        live values of every tunable
  send_msg.py --dump-state          the JSON currently in NVS
"""
    print(text)
    return 0


def help_controls() -> int:
    """Print a reference for every HA-exposed control."""
    text = """\
green-terminal — Home Assistant control reference

═══════════════════════════════════════════════════════════════════════
NUMBER ENTITIES — tunable, persist in NVS, live in HA's "Configuration"
═══════════════════════════════════════════════════════════════════════

TYPING & TIMING
  typewriter_speed       chars/sec       1..200   default 20
      Higher = faster. Capped in practice by display refresh.

  message_hold_time      ms              500..60000   default 10000
      How long the fully-typed message stays on screen before clearing.
      Sticky alerts respect this too — after the hold and clear pause
      they re-type themselves instead of advancing to the next message.

  clear_pause            ms              0..30000   default 3000
      Blank-screen pause between consecutive messages.

QUEUE / MESSAGE LIMITS
  queue_capacity         chunks          1..50   default 20
      Maximum chunks held simultaneously. Oldest GROUP evicted on overflow.

  split_threshold        chars           50..1000   default 200
      Long messages are split at word boundaries near this length.

  max_input_length       chars           100..8192   default 4096
      Hard cap on a single `add_message` call before any splitting.

  max_stored_chunk       chars           64..2048   default 512
      Ceiling on a single chunk after splitting.

DISPLAY LAYOUT
  visible_columns        chars           10..40   default 26
      Where the word-wrap engine breaks; should match the actual font.

  visible_rows           lines           3..14   default 9
      Vertical scrollback in the visible region.

  line_height            px              12..40   default 24
      Vertical pixels per row. Should track the font size.

  glyph_width            px              6..24   default 12
      Cursor block width (text rendering itself uses font metrics).

  margin                 px              0..20   default 4
      Padding from the screen edges to the text area.

CRT EFFECTS
  background_glow        0..60           default 10
      Faint phosphor wash behind the text. 0 = pure black bg.
      Value scales the foreground colour by N/255 per channel.

  scanline_period        px              0..12   default 4
      Distance between dithered black scan rows. 0 = scan lines off.

  bloom_intensity        %               0..100   default 0
      Per-glyph halo (4 shifted dim copies). Has the highest per-frame
      cost — bump cautiously.

  vignette               %               0..100   default 0
      Bayer-dithered fade-to-black toward corners. 0 = off.

GHOST (CRT afterimage on scroll / clear)
  ghost_duration         ms              0..3000   default 550
      How long the ghost lingers before fully fading. 0 = ghost off.

  ghost_intensity        0..255          default 60
      Peak brightness of the ghost layer.

COLOURS (RGB, 0..255 each)
  text_r / text_g / text_b      default 0 / 242 / 0     (~95% green)
  alert_r / alert_g / alert_b   default 255 / 176 / 0   (amber)

IDLE MATRIX RAIN
  rain_speed             %               10..300   default 100
      Scales the per-column falling rate (base 1.5..3.5 rows/s).

  rain_density           %               10..100   default 100
      Column coverage. 100 = every column, 50 = every other, 25 = every 4th.

TAP INPUT
  tap_threshold          m/s²            1.0..30.0 default 3.5
      Accel magnitude (above 1G rest) that registers as a tap. Lower to
      catch lighter taps; raise if handling the device false-triggers.

  tap_refractory_ms      ms              30..250   default 80
      Minimum gap between accepted taps so a single physical impulse
      (≈30–80 ms long) doesn't register multiple times. Must stay
      below the discriminator window (350 ms) so real double-taps
      register as two events.

═══════════════════════════════════════════════════════════════════════
OTHER ENTITIES
═══════════════════════════════════════════════════════════════════════

  Light:        "Backlight" (monochromatic)
      LCD backlight on/off + brightness. Restore mode: ALWAYS_ON.

  Light:        "Status LEDs" (esp32_rmt_led_strip, GPIO16, 3 × WS2812B)
      3 addressable LEDs auto-driven by the alert dispatcher (see
      `--help-leds` for the marker → colour/effect table). Manual
      override via HA works until the next alert state change.

  Switch:       "Sleep when idle"  (slug `sleep_when_idle`)
      When on, the screen goes fully black and the LCD backlight is
      faded off whenever the message queue is empty. Send any message
      and the backlight fades back on and typing resumes normally.
      Pure power-saver — settings and state are preserved.

  Tap input:    Tap the device case (or screen) to skip to the next
      message; double-tap to clear the currently-displayed message
      group. The QMI8658 IMU on the Waveshare board feeds the firmware
      a magnitude spike whenever the case is tapped (threshold + 80 ms
      refractory). Screen taps via the CST816 panel are a secondary
      path into the same discriminator. The single-tap resolves ~350 ms
      after the last tap so it can be told apart from a double-tap.
      Tunable via the "Tap threshold" (m/s² above 1G) and "Tap
      refractory" (ms) number entities.

═══════════════════════════════════════════════════════════════════════
INSPECT / SET FROM THE CLI
═══════════════════════════════════════════════════════════════════════

  send_msg.py --list-numbers              # current value + range of every entity
  send_msg.py --set <slug> <value>        # set one entity
  send_msg.py --help-api                  # ingestion API reference
  send_msg.py --help-leds                 # status LED behaviour
"""
    print(text)
    return 0


def help_api() -> int:
    """Print a reference for every device-side API surface."""
    text = """\
green-terminal API reference

═══════════════════════════════════════════════════════════════════════
INGESTION — ways to push text onto the device
═══════════════════════════════════════════════════════════════════════

ESPHome native API actions (called via this script or Home Assistant):

  add_message(message: string)
      Push a normal (green) message onto the queue. Long messages are
      split at word boundaries; chunks of the same source are typed
      continuously without inter-chunk pause and count as one group.

      CLI:    send_msg.py "your text"
      CLI:    echo "your text" | send_msg.py
      HA:     service green_terminal.add_message  data:  message: "..."

  add_alert(message: string)
      Same as add_message but flagged as an alert — rendered in the
      configured alert colour (default amber 255/176/0).

      CLI:    send_msg.py --alert "warning text"
      HA:     service green_terminal.add_alert

  add_sticky_alert(message: string)
      Alert that re-loops on itself indefinitely instead of advancing to
      the next queued message. After typing out the message, it holds
      for `message_hold_time`, clears the screen, holds for
      `clear_pause`, and starts re-typing the same message. Works for
      messages longer than one screen — they fully type out (scrolling
      as needed) then loop. While sticky is on screen the footer is
      rendered as "- x/N -" with surrounding dashes. Removed via
      `--clear`, `--clear N`, or `clear_alerts`.

      CLI:    send_msg.py --sticky "OVERHEATING"
      HA:     service green_terminal.add_sticky_alert

  clear_queue
      Empty the whole queue immediately.

      CLI:    send_msg.py --clear

  clear_indices(indices: string)
      Remove specific groups by their 1-based position (the x of x/N).
      The indices string is space- or comma-separated; the device sorts
      them descending before removal so numbering stays stable.

      CLI:    send_msg.py --clear 3 5 7
      HA:     service green_terminal.clear_indices  data:  indices: "3 5 7"

  clear_alerts
      Remove every alert-flagged message; leaves normal messages alone.

      CLI:    send_msg.py --clear-alerts

  reset_leds
      Turn the status LED strip off right now AND drop the saved HA
      baseline back to off. After this call the strip stays dark until
      either HA sets it again or an alert fires; future alert exits
      will go to off instead of restoring the previous HA state.

      CLI:    send_msg.py --reset-leds
      HA:     service green_terminal.reset_leds

  skip
      Advance to the next message immediately (skips the post-message
      and clear pauses).

      CLI:    send_msg.py --skip

  dump_state
      Log the persisted JSON to the device console. The CLI reassembles
      the log chunks back into a single JSON payload on stdout.

      CLI:    send_msg.py --dump-state           # raw JSON
      CLI:    send_msg.py --dump-state | jq      # pretty-printed

MQTT topics (broker configured in secrets.yaml; payload = message text):

  green-terminal/enqueue       → normal message
  green-terminal/alert         → alert message
  green-terminal/sticky-alert  → sticky alert (pins cycle until cleared)

      mosquitto_pub -h <broker> -t green-terminal/enqueue -m "hello"

═══════════════════════════════════════════════════════════════════════
INLINE FORMATTING
═══════════════════════════════════════════════════════════════════════

  *emphasis*    Text wrapped in asterisks blinks at the cursor-blink
                rate with a fade trail. Asterisks themselves are hidden
                once the closing one arrives.

  :name:        Glyph shortcut. Expanded server-side before split/store
                so the queue contains the actual glyph. Run:

                    send_msg.py --list-shortcuts

                to see every available shortcut, its codepoint, and a
                preview character.

═══════════════════════════════════════════════════════════════════════
CONFIGURATION — runtime tunables (HA number entities)
═══════════════════════════════════════════════════════════════════════

Every visual / timing knob is a `number` entity, persisted in NVS.
List them with current values + ranges:

  send_msg.py --list-numbers

Set one (object_id from --list-numbers):

  send_msg.py --set typewriter_speed 50      # chars/sec
  send_msg.py --set background_glow 10       # 0..60
  send_msg.py --set vignette 30              # 0..100
  send_msg.py --set rain_speed 150           # idle rain
  send_msg.py --set alert_r 255              # alert colour RGB

═══════════════════════════════════════════════════════════════════════
IDLE BEHAVIOUR
═══════════════════════════════════════════════════════════════════════

When the queue is empty the display falls back to a Matrix-style rain
of hex glyphs in the configured text colour. Send any message to take
over; clear the queue to return to rain.

═══════════════════════════════════════════════════════════════════════
PERSISTENCE
═══════════════════════════════════════════════════════════════════════

Queue contents survive reboot. Saved as JSON in NVS under the "term"
namespace, key "state_json". Format:

  {
    "q": ["chunk1", "chunk2", ...],   # source chunks (after :name: expansion)
    "g": [0, 1, 1, 0, ...],           # group ids (0=standalone, >0=split-group)
    "a": [0, 1, 1, 0, ...],           # alert flags (parallel to q)
    "n": 7                            # next_group_id counter
  }

Inspect any time with:

  send_msg.py --dump-state
"""
    print(text)
    return 0


def list_shortcuts() -> int:
    """Print every `:name:` shortcut declared in includes/queue_storage.h."""
    header = Path(__file__).resolve().parent / "includes" / "queue_storage.h"
    text = header.read_text(encoding="utf-8")
    entry = re.compile(
        r'\{"(:[a-zA-Z0-9_\-]+:)",\s*"((?:\\x[0-9A-Fa-f]{2})+)"\s*\}'
        r'\s*,\s*//\s*U\+([0-9A-Fa-f]+)(?:\s*(.*))?',
    )
    rows = []
    for m in entry.finditer(text):
        name = m.group(1)
        cp = int(m.group(3), 16)
        desc = (m.group(4) or "").strip()
        rows.append((name, cp, desc))
    if not rows:
        print("ERROR: no shortcuts found in queue_storage.h", file=sys.stderr)
        return 1
    widest = max(len(r[0]) for r in rows)
    print(f"{len(rows)} shortcuts (use :name: in messages):")
    for name, cp, desc in rows:
        # Renderable if not in PUA — but the terminal might not have the glyph
        # anyway, so always print both the literal and the codepoint.
        glyph = chr(cp)
        in_pua = 0xE000 <= cp <= 0xF8FF or cp >= 0xF0000
        display = "·" if in_pua else glyph
        suffix = f" — {desc}" if desc else ""
        print(f"  {name:<{widest}}  {display}  U+{cp:04X}{suffix}")
    return 0


async def list_numbers() -> int:
    cli = APIClient(ADDR, PORT, password=None, noise_psk=NOISE_PSK)
    await cli.connect(login=True)
    states = {}
    cli.subscribe_states(lambda s: states.update({s.key: s.state}))
    try:
        entities, _ = await cli.list_entities_services()
        await asyncio.sleep(0.7)            # let states arrive
        nums = [e for e in entities if type(e).__name__ == "NumberInfo"]
        if not nums:
            return 0
        widest = max(len(n.object_id) for n in nums)
        for n in sorted(nums, key=lambda x: x.object_id):
            v = states.get(n.key)
            vstr = f"{v:g}" if v is not None else "?"
            print(f"  {n.object_id:<{widest}} = {vstr:>7}  "
                  f"[{n.min_value:g} .. {n.max_value:g}] "
                  f"step={n.step:g}  ({n.name})")
    finally:
        await cli.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("text", nargs="*",
                   help="Message text (omit if piping via stdin)")
    p.add_argument("--alert", action="store_true",
                   help="Send as an alert (rendered in the alert colour)")
    p.add_argument("--sticky", action="store_true",
                   help="Sticky alert: pins the cycle on this message until "
                        "it's cleared via --clear or --clear <N>. Implies --alert.")
    p.add_argument("--skip", action="store_true",
                   help="Call the 'skip' action to advance to next message")
    p.add_argument("--clear", nargs="*", metavar="N", default=None,
                   help="Empty the queue. With no args clears everything; "
                        "with 1-based group numbers (e.g. --clear 3 5 7) "
                        "removes only those messages.")
    p.add_argument("--clear-alerts", action="store_true",
                   help="Call 'clear_alerts' to remove only alert messages")
    p.add_argument("--reset-leds", action="store_true",
                   help="Turn off the status LED strip and drop the saved "
                        "HA-set baseline so future alert exits go dark.")
    p.add_argument("--set", nargs=2, metavar=("NAME", "VALUE"),
                   help="Set a tunable number entity by object_id, e.g. "
                        "--set background_glow 10")
    p.add_argument("--list-numbers", action="store_true",
                   help="List all tunable number entities and their ranges")
    p.add_argument("--list-lights", action="store_true",
                   help="List all light entities with current state + effects")
    p.add_argument("--list-switches", action="store_true",
                   help="List all switch entities with current state")
    p.add_argument("--switch", nargs=2, metavar=("NAME", "STATE"),
                   help="Toggle a switch: --switch sleep_when_idle on")
    p.add_argument("--led",
                   choices=["on", "off"],
                   help="Turn the status LED strip on or off")
    p.add_argument("--led-color", metavar="COLOR",
                   help="Set LED colour. Accepts name (red/blue/amber/...), "
                        "R,G,B integers (0..255), or #rrggbb hex.")
    p.add_argument("--led-brightness", metavar="PCT", type=int,
                   help="Set LED brightness 0..100")
    p.add_argument("--led-effect", metavar="NAME",
                   help="Set LED effect by name (use --list-lights to see "
                        "available; 'None' disables effects)")
    p.add_argument("--list-shortcuts", action="store_true",
                   help="List `:name:` glyph shortcuts (from queue_storage.h)")
    p.add_argument("--dump-state", action="store_true",
                   help="Print the queue state JSON currently stored in NVS")
    p.add_argument("--help-api", action="store_true",
                   help="Print a full reference for every device-side API")
    p.add_argument("--help-controls", action="store_true",
                   help="Print a full reference for every HA-exposed control")
    p.add_argument("--help-summary", action="store_true",
                   help="Print a prose overview of the project")
    p.add_argument("--help-leds", action="store_true",
                   help="Print the status-LED behaviour reference")
    args = p.parse_args()

    if args.help_summary:
        return help_summary()
    if args.help_api:
        return help_api()
    if args.help_controls:
        return help_controls()
    if args.help_leds:
        return help_leds()
    if args.list_shortcuts:
        return list_shortcuts()
    if args.list_lights:
        return asyncio.run(list_lights())
    if args.list_switches:
        return asyncio.run(list_switches())
    if args.switch:
        name, st = args.switch
        st = st.lower()
        if st not in ("on", "off", "true", "false", "1", "0"):
            print(f"ERROR: --switch STATE must be on/off, got {st!r}",
                  file=sys.stderr)
            return 2
        return asyncio.run(set_switch(name,
                                      st in ("on", "true", "1")))
    led_kwargs = {}
    if args.led is not None:
        led_kwargs["state"] = (args.led == "on")
    if args.led_color is not None:
        try:
            led_kwargs["rgb"] = parse_color(args.led_color)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        led_kwargs.setdefault("state", True)
    if args.led_brightness is not None:
        if not 0 <= args.led_brightness <= 100:
            print("ERROR: --led-brightness must be 0..100", file=sys.stderr)
            return 2
        led_kwargs["brightness"] = args.led_brightness / 100
        led_kwargs.setdefault("state", True)
    if args.led_effect is not None:
        led_kwargs["effect"] = args.led_effect
        led_kwargs.setdefault("state", True)
    if led_kwargs:
        return asyncio.run(set_light("status_leds", **led_kwargs))
    if args.dump_state:
        return asyncio.run(dump_state())
    if args.list_numbers:
        return asyncio.run(list_numbers())
    if args.set:
        name, raw_value = args.set
        try:
            value = float(raw_value)
        except ValueError:
            print(f"ERROR: {raw_value!r} is not a number", file=sys.stderr)
            return 2
        return asyncio.run(set_number(name, value))
    if args.clear is not None:
        if args.clear:                       # specific indices given
            try:
                nums = sorted({int(x) for x in args.clear}, reverse=True)
            except ValueError:
                print(f"ERROR: --clear takes integers, got {args.clear}",
                      file=sys.stderr)
                return 2
            return asyncio.run(call("clear_indices",
                                    {"indices": " ".join(str(n) for n in nums)}))
        return asyncio.run(call("clear_queue", {}))
    if args.clear_alerts:
        return asyncio.run(call("clear_alerts", {}))
    if args.reset_leds:
        return asyncio.run(call("reset_leds", {}))
    if args.skip:
        return asyncio.run(call("skip", {}))

    msg = read_message_from_args(args)
    if not msg:
        print("ERROR: no message given (pipe via stdin or pass as argv)",
              file=sys.stderr)
        return 2

    if args.sticky:
        action = "add_sticky_alert"
    elif args.alert:
        action = "add_alert"
    else:
        action = "add_message"
    print(f"{action} ({len(msg)} chars): {msg[:60]}"
          f"{'...' if len(msg) > 60 else ''}", file=sys.stderr)
    return asyncio.run(call(action, {"message": msg}))


if __name__ == "__main__":
    raise SystemExit(main())
