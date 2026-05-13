# Status LED reference

> Mirror of `send_msg.py --help-leds`. The script is canonical — if these
> drift, the script wins.

## Hardware

3 × WS2812B addressable LEDs wired to GPIO16 (Din), powered from the
board's 5V rail and sharing ground. Exposed in Home Assistant as the
"Status LEDs" light entity — manual on/off/colour/brightness/effect
control works any time. When an alert begins the dispatcher takes
over, snapshots whatever HA had the strip set to, applies the alert
effect, and restores the snapshot when the alert finishes. The
snapshot survives across multiple alerts in a row; it's only reset
by `--reset-leds` or `--clear` (queue clear), which both put the
strip dark and drop the saved baseline to off.

## Alert-driven behaviour

Whenever an alert (regular or sticky) is the currently-displayed
message on the terminal, the strip auto-selects a colour + effect
based on the FIRST trigger glyph found in the chunk text. If no
trigger is found, the default amber pulse is used. When the cycle
returns to a non-alert message — or the queue empties — the strip
turns off.

| Marker        | Trigger glyph         | Colour            | Effect   |
| ------------- | --------------------- | ----------------- | -------- |
| *(none)*      | —                     | amber             | pulse    |
| `:warning:`   | U+F071 fa-warning     | amber             | scan     |
| `:nf-heart:`  | U+F004 fa-heart       | red               | pulse    |
| `:heart:`     | U+2665 ♥              | red               | pulse    |
| `:x:`         | U+F00D fa-times       | red               | scan     |
| `:check:`     | U+F00C fa-check       | green             | scan     |
| `:bolt:`      | U+26A1 ⚡             | cool blue-white   | flicker  |

First marker in the text wins on conflict — reorder glyphs to choose
which one fires the effect for messages that include several.

## Effect tuning

```
pulse                              addressable_scan (scan)
  transition_length:  1000ms        move_interval:  240ms
  update_interval:    1000ms        scan_width:     1
  min/max brightness: 25% / 100%

addressable_flicker (flicker)
  update_interval:    16ms
  intensity:          80%
  bolt baseline:      25% brightness (overrides default 100%)
```

## Alert examples

```sh
send_msg.py --alert "fire alarm tripped"            # amber pulse
send_msg.py --alert ":bolt: power dip on circuit 3" # blue-white flicker
send_msg.py --alert ":check: backup complete"       # green scan
send_msg.py --alert ":x: build failed"              # red scan
send_msg.py --alert ":warning: low battery"         # amber scan
send_msg.py --sticky ":heart: I love you"           # red pulse, pinned
```

Sticky alerts respect the same trigger table — a sticky `:bolt:` will
flicker blue-white the entire time it pins the cycle.

## Manual control (CLI ⇄ Home Assistant)

The strip is exposed as the "Status LEDs" light entity. Anything HA
sets propagates straight to the device; anything the CLI sets is
broadcast back to HA. They share one source of truth.

```sh
send_msg.py --list-lights                # show every light + its state + effects

send_msg.py --led on                     # turn on (last colour/effect kept)
send_msg.py --led off                    # turn off
send_msg.py --led-color red              # by name
send_msg.py --led-color "255,128,0"      # by RGB triple
send_msg.py --led-color "#00ffaa"        # by hex
send_msg.py --led-brightness 60          # 0..100 percent
send_msg.py --led-effect pulse           # or None / scan / flicker
send_msg.py --led-color cyan --led-brightness 80 --led-effect scan  # combined
```

Color names: `red green blue yellow amber orange purple pink cyan magenta white`

## Snapshot / restore lifecycle

Manual settings (from CLI or HA) persist while no alert is on screen.
When an alert arrives the dispatcher snapshots the current colour /
brightness / effect, applies the alert effect, then restores the
snapshot at alert end. Chain multiple alerts together and the
snapshot survives until the cycle finally returns to non-alert
content.

```sh
send_msg.py --reset-leds                 # off + drop the snapshot (default → off)
send_msg.py --clear                      # also drops the snapshot
```

So a typical "set ambient + let alerts override" flow is:

```sh
send_msg.py --led-color "30,30,80" --led-brightness 30   # dim blue ambient
send_msg.py --alert ":bolt: power blip"                  # flickers white-blue
# ... alert finishes; LEDs return to dim blue ambient
send_msg.py --reset-leds                                  # done for the night
```
