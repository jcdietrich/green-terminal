#!/usr/bin/env python3
"""green-terminal device simulator.

Renders MP4 demos of the firmware behaviour: typewriter typing, scrolling,
alerts, sticky-alert loops, and the idle Matrix-rain screen. Reproduces the
ST7789 display pipeline from `green-terminal.yaml` (background phosphor wash,
4-direction bloom, scanline stipple, Bayer-dithered vignette, ghost
afterimage on clear, blinking cursor with fade trail).

The script is canonical for the actual device; this sim mirrors the
rendering logic but lives entirely off-board for screenshot/recording use.

Usage:
    python tools/sim.py --all
    python tools/sim.py --scenario basic
    python tools/sim.py --list
    echo "custom text" | python tools/sim.py                       # piped one-off
    echo "custom text" | python tools/sim.py --name demo.mp4       # name the output
    echo "custom text" | python tools/sim.py --footer "CPU: 42%"   # override x/N footer
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ───────────────────────── Device geometry (post-rotation) ─────────────────
W, H = 320, 240            # canvas after rotation:90
MG = 4                     # margin
GLYPH_W = 12               # cursor block width
LINE_H = 24                # vertical pixels per row
VIS_COLS = 26
VIS_ROWS = 9
FONT_BODY_SIZE = 18
FONT_FOOTER_SIZE = 12
FPS = 60
SCALE = 2                  # nearest-upscale before encoding

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = REPO_ROOT / "fonts" / "RobotoMonoNerdFontMono-Bold.ttf"
OUT_DIR = REPO_ROOT / "docs" / "img"


# ───────────────────────── Tunables (yaml initial_values) ──────────────────
@dataclass
class Tunables:
    typewriter_cps: float = 20.0
    message_hold_ms: int = 10000
    clear_pause_ms: int = 3000
    background_glow: int = 10
    scanline_period: int = 4
    bloom_intensity: int = 0       # %  — off on device by default
    vignette: int = 0              # %  — off on device by default
    ghost_duration_ms: int = 550
    ghost_intensity: int = 60      # peak ghost brightness scale (0..255)
    text_rgb: Tuple[int, int, int] = (0, 242, 0)
    alert_rgb: Tuple[int, int, int] = (255, 176, 0)
    rain_speed: int = 100          # %
    rain_density: int = 100        # %


def demo_tunables() -> Tunables:
    """Real device defaults pull bloom/vignette/ghost-intensity down for a
    subtle CRT feel. Bump them for demos so the showcase clips actually
    exhibit the effects after scanline + vignette + GIF quantization.

    Hold/clear timings are also shortened so scenes don't drag."""
    return Tunables(
        bloom_intensity=20,
        vignette=30,
        ghost_intensity=160,        # default 60 is intentionally subtle
        ghost_duration_ms=900,      # default 550 = ~8 frames at 15fps
        message_hold_ms=2000,
        clear_pause_ms=1000,
    )


# ───────────────────────── Font cache ──────────────────────────────────────
_body_font: Optional[ImageFont.FreeTypeFont] = None
_footer_font: Optional[ImageFont.FreeTypeFont] = None


def body_font() -> ImageFont.FreeTypeFont:
    global _body_font
    if _body_font is None:
        _body_font = ImageFont.truetype(str(FONT_PATH), FONT_BODY_SIZE)
    return _body_font


def footer_font() -> ImageFont.FreeTypeFont:
    global _footer_font
    if _footer_font is None:
        _footer_font = ImageFont.truetype(str(FONT_PATH), FONT_FOOTER_SIZE)
    return _footer_font


# ───────────────────────── `:name:` shortcut expansion ────────────────────
# Subset of the device's mapping (includes/queue_storage.h). Only the glyphs
# used by the demo scenarios are listed; the device has the full table.
_SHORTCUTS = {
    ":heart:":    "♥",
    ":bolt:":     "⚡",
    ":warning:":  "",
    ":check:":    "",
    ":x:":        "",
    ":nf-heart:": "",
}


def expand_shortcuts(text: str) -> str:
    for name, glyph in _SHORTCUTS.items():
        text = text.replace(name, glyph)
    return text


# ───────────────────────── Word wrap / scroll ──────────────────────────────
def wrap_text(text: str, cols: int = VIS_COLS) -> List[str]:
    """Word-wrap that preserves trailing partial words (needed for type-out).

    Newlines force a break. Words longer than `cols` are hard-broken.
    """
    if not text:
        return [""]
    out: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = ""
        # tokenize keeping whitespace
        tokens: List[str] = []
        i = 0
        while i < len(paragraph):
            if paragraph[i].isspace():
                j = i
                while j < len(paragraph) and paragraph[j].isspace():
                    j += 1
                tokens.append(paragraph[i:j])
                i = j
            else:
                j = i
                while j < len(paragraph) and not paragraph[j].isspace():
                    j += 1
                tokens.append(paragraph[i:j])
                i = j
        for tok in tokens:
            while tok:
                room = cols - len(line)
                if len(tok) <= room:
                    line += tok
                    tok = ""
                else:
                    # If the token is whitespace and we have content, break here.
                    if tok.isspace():
                        out.append(line)
                        line = ""
                        # drop the whitespace at start of next line
                        tok = ""
                    elif len(tok) > cols:
                        # hard break the long word
                        if line:
                            out.append(line)
                            line = ""
                        out.append(tok[:cols])
                        tok = tok[cols:]
                    else:
                        # word doesn't fit; push to next line
                        out.append(line)
                        line = ""
        out.append(line)
    return out


def visible_window(lines: List[str], rows: int = VIS_ROWS) -> List[str]:
    """Return the last `rows` lines (the visible scrollback)."""
    if len(lines) <= rows:
        return lines
    return lines[-rows:]


# ───────────────────────── Scene model ─────────────────────────────────────
@dataclass
class Cursor:
    visible: bool = False
    x: int = MG
    y: int = MG
    blinking: bool = False     # if False, fully solid; if True, use blink_alpha


@dataclass
class Ghost:
    lines: List[str] = field(default_factory=list)
    age_ms: int = 0                # 0 = just-started ghost, fades over ghost_duration_ms
    is_alert: bool = False


@dataclass
class Frame:
    """Per-frame state handed to the renderer."""
    visible_lines: List[str] = field(default_factory=list)
    fg_rgb: Tuple[int, int, int] = (0, 242, 0)
    cursor: Cursor = field(default_factory=Cursor)
    ghost: Optional[Ghost] = None
    footer: Optional[str] = None
    rain: bool = False        # if True, render idle matrix rain instead


@dataclass
class SubtitleEntry:
    """A single timed subtitle cue."""
    start_ms: int
    end_ms: int
    text: str


# ───────────────────────── Blink-alpha curve ───────────────────────────────
def blink_alpha(t_ms: int) -> float:
    """Reproduce the device's 1000 ms cursor-blink cycle: 0..500 ON,
    500..800 fade, 800..1000 OFF."""
    cycle = t_ms % 1000
    if cycle < 500:
        return 1.0
    if cycle < 800:
        return 1.0 - (cycle - 500) / 300.0
    return 0.0


# ───────────────────────── Renderer ────────────────────────────────────────
# Pre-computed Bayer 4×4 matrix (matches the lambda).
_BAYER4 = np.array([
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
], dtype=np.uint8)
_BAYER_TILE = np.tile(_BAYER4, (H // 4 + 1, W // 4 + 1))[:H, :W]


def _scaled_color(rgb: Tuple[int, int, int], scale: float) -> Tuple[int, int, int]:
    return (
        max(0, min(255, int(rgb[0] * scale))),
        max(0, min(255, int(rgb[1] * scale))),
        max(0, min(255, int(rgb[2] * scale))),
    )


def _draw_text_line(
    canvas: Image.Image,
    x: int,
    y: int,
    text: str,
    color: Tuple[int, int, int],
    blink_a: float,
) -> int:
    """Draw a line, parsing `*emphasis*` as blink segments. Returns end x."""
    draw = ImageDraw.Draw(canvas)
    f = body_font()
    cx = x
    in_blink = False
    seg = ""

    def flush_seg(nonlocal_in_blink: bool) -> None:
        nonlocal cx, seg
        if not seg:
            return
        if nonlocal_in_blink and blink_a <= 0.01:
            # skip drawing while faded out
            w = int(draw.textlength(seg, font=f))
            cx += w
            seg = ""
            return
        col = color
        if nonlocal_in_blink:
            col = _scaled_color(color, blink_a)
        draw.text((cx, y), seg, font=f, fill=col, anchor="lt")
        w = int(draw.textlength(seg, font=f))
        cx += w
        seg = ""

    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "*":
            flush_seg(in_blink)
            in_blink = not in_blink
            i += 1
            continue
        seg += ch
        i += 1
    flush_seg(in_blink)
    return cx


def _render_text_lines(
    canvas: Image.Image,
    lines: List[str],
    color: Tuple[int, int, int],
    blink_a: float,
) -> int:
    """Render all visible lines from y=MG downward. Returns end-x of last line."""
    last_x = MG
    for i, line in enumerate(lines):
        y = MG + i * LINE_H
        last_x = _draw_text_line(canvas, MG, y, line, color, blink_a)
    return last_x


def _apply_bloom(
    canvas_np: np.ndarray,
    text_layer_np: np.ndarray,
    fg_rgb: Tuple[int, int, int],
    intensity_pct: int,
) -> np.ndarray:
    """Replicate the lambda's bloom: render 4 cardinal-shifted dim copies of
    the text layer in fg * intensity/100 and ADD them under the bright text.

    `text_layer_np` is an HxWx4 array where alpha=255 marks rendered pixels.
    """
    if intensity_pct <= 0:
        return canvas_np
    bloom_col = np.array(_scaled_color(fg_rgb, intensity_pct / 100.0), dtype=np.uint16)
    mask = text_layer_np[..., 3] > 0  # boolean HxW
    out = canvas_np.astype(np.uint16)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        shifted = np.zeros_like(mask)
        y0a = max(0, dy)
        y0b = max(0, -dy)
        x0a = max(0, dx)
        x0b = max(0, -dx)
        h = mask.shape[0] - abs(dy)
        w = mask.shape[1] - abs(dx)
        shifted[y0a:y0a + h, x0a:x0a + w] = mask[y0b:y0b + h, x0b:x0b + w]
        # Add bloom_col where shifted mask is set
        idx = shifted
        out[idx, 0] = np.minimum(255, out[idx, 0] + bloom_col[0])
        out[idx, 1] = np.minimum(255, out[idx, 1] + bloom_col[1])
        out[idx, 2] = np.minimum(255, out[idx, 2] + bloom_col[2])
    return out.astype(np.uint8)


def _apply_scanlines(canvas_np: np.ndarray, period: int) -> np.ndarray:
    """Stippled scanline pattern: every Nth row, alternating x parity → black."""
    if period <= 0:
        return canvas_np
    out = canvas_np
    H_, W_, _ = out.shape
    for y in range(1, H_, period):
        x_start = y & 1
        out[y, x_start::2, :] = 0
    return out


def _apply_vignette(canvas_np: np.ndarray, strength: int) -> np.ndarray:
    """Bayer-dithered fade-to-black toward corners (matches the lambda)."""
    if strength <= 0:
        return canvas_np
    H_, W_, _ = canvas_np.shape
    vcx, vcy = W_ // 2, H_ // 2
    max_r2 = vcx * vcx + vcy * vcy
    inner_sq = (max_r2 * (100 - strength)) // 100
    ys, xs = np.indices((H_, W_))
    dy = ys - vcy
    dx = xs - vcx
    r2 = dx * dx + dy * dy
    # fade_factor 0..16 across the ring; clamp to 16 outside maxR2
    denom = max(1, max_r2 - inner_sq)
    fade = np.where(
        r2 >= max_r2,
        16,
        np.where(r2 <= inner_sq, 0, ((r2 - inner_sq) * 16) // denom),
    ).astype(np.int32)
    threshold = _BAYER_TILE
    black = fade > threshold
    out = canvas_np.copy()
    out[black] = 0
    return out


def _render_matrix_rain(canvas: Image.Image, now_ms: int, t: Tunables) -> None:
    """Per-column stable hex-glyph rain, matching the lambda's seed formula."""
    draw = ImageDraw.Draw(canvas)
    f = body_font()
    fg = t.text_rgb
    n_cols = (W - 2 * MG) // GLYPH_W
    n_rows = (H - MG) // LINE_H
    rain_speed_factor = t.rain_speed / 100.0
    col_step = max(1, 100 // max(1, t.rain_density))
    pool = "0123456789ABCDEF"
    pool_size = len(pool)

    for col in range(0, n_cols, col_step):
        seed = col * 73 + 11
        speed_rps = (1.5 + (seed % 5) * 0.5) * rain_speed_factor
        phase_ms = (seed * 257) % 6000
        trail_len = 4 + (seed % 4)
        head_row_f = ((now_ms + phase_ms) / 1000.0) * speed_rps
        cycle_len = float(n_rows + trail_len + 4)
        head_row_f = math.fmod(head_row_f, cycle_len)
        head_row = int(head_row_f)
        for ti in range(trail_len):
            row = head_row - ti
            if row < 0 or row >= n_rows:
                continue
            shift = (now_ms // 250) + col * 37 + row * 71
            ch = pool[shift % pool_size]
            if ti == 0:
                col_rgb = (
                    min(255, fg[0] + 120),
                    min(255, fg[1] + 60),
                    min(255, fg[2] + 120),
                )
            else:
                fade = 1.0 - ti / trail_len
                col_rgb = _scaled_color(fg, fade)
            x = MG + col * GLYPH_W
            y = MG + row * LINE_H
            draw.text((x, y), ch, font=f, fill=col_rgb, anchor="lt")


def render_frame(
    frame: Frame,
    now_ms: int,
    t: Tunables,
    blink_a: Optional[float] = None,
) -> np.ndarray:
    """Build one HxWx3 uint8 array for the current frame state."""
    if blink_a is None:
        blink_a = blink_alpha(now_ms)
    fg = frame.fg_rgb
    bg = _scaled_color(fg, t.background_glow / 255.0)
    canvas = Image.new("RGB", (W, H), bg)

    if frame.rain:
        _render_matrix_rain(canvas, now_ms, t)
    else:
        # Ghost layer (drawn first; bright text overdraws where it can).
        ghost_active = False
        if frame.ghost and frame.ghost.lines and t.ghost_duration_ms > 0:
            ghost_age = frame.ghost.age_ms
            if 0 <= ghost_age < t.ghost_duration_ms:
                ghost_active = True
                tg = 1.0 - ghost_age / t.ghost_duration_ms
                scale = tg * t.ghost_intensity / 255.0
                src = t.alert_rgb if frame.ghost.is_alert else t.text_rgb
                gcol = _scaled_color(src, scale)
                # Ghost shows blink segments as solid (snapshot frozen).
                for i, line in enumerate(frame.ghost.lines):
                    _draw_text_line(canvas, MG, MG + i * LINE_H, line, gcol, 1.0)

        # Bloom (suppressed while ghost is active to avoid flicker).
        if t.bloom_intensity > 0 and frame.visible_lines and not ghost_active:
            text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            _render_text_lines(text_layer, frame.visible_lines, (255, 255, 255), blink_a)
            canvas_np = np.array(canvas, dtype=np.uint8)
            text_np = np.array(text_layer, dtype=np.uint8)
            canvas_np = _apply_bloom(canvas_np, text_np, fg, t.bloom_intensity)
            canvas = Image.fromarray(canvas_np)

        # Bright text.
        last_x = _render_text_lines(canvas, frame.visible_lines, fg, blink_a)

        # Cursor (filled rectangle).
        if frame.cursor.visible:
            alpha = blink_a if frame.cursor.blinking else 1.0
            if alpha > 0.01:
                col = _scaled_color(fg, alpha)
                # Cursor position: defaults to end of last line unless explicit.
                cx = frame.cursor.x if frame.cursor.x else last_x
                cy = frame.cursor.y
                draw = ImageDraw.Draw(canvas)
                draw.rectangle(
                    [cx, cy, cx + GLYPH_W - 1, cy + LINE_H - 1],
                    fill=col,
                )

    # Footer (rendered above scanlines so it stays crisp).
    if frame.footer:
        draw = ImageDraw.Draw(canvas)
        ff = footer_font()
        text_w = int(draw.textlength(frame.footer, font=ff))
        x = (W - text_w) // 2
        y = H - MG - FONT_FOOTER_SIZE  # approximate baseline
        draw.text((x, y), frame.footer, font=ff, fill=fg, anchor="lt")

    # Pixel-level effects.
    canvas_np = np.array(canvas, dtype=np.uint8)
    canvas_np = _apply_scanlines(canvas_np, t.scanline_period)
    canvas_np = _apply_vignette(canvas_np, t.vignette)
    return canvas_np


# ───────────────────────── Cursor placement ────────────────────────────────
def cursor_end_x(lines: List[str]) -> int:
    """Compute the pixel x where the cursor should sit after the last line.

    Walks `*emphasis*` segments the same way the renderer does so asterisks
    don't shift the cursor."""
    if not lines:
        return MG
    text = lines[-1]
    # strip asterisks (they're inline-blink markers, not drawn)
    drawn = text.replace("*", "")
    f = body_font()
    # Use a temp image for textlength (PIL needs a Draw context)
    tmp = Image.new("L", (1, 1))
    d = ImageDraw.Draw(tmp)
    w = int(d.textlength(drawn, font=f))
    return MG + w


def cursor_row_y(lines: List[str]) -> int:
    row = max(0, len(lines) - 1)
    return MG + row * LINE_H


# ───────────────────────── Scenario timeline helpers ───────────────────────
@dataclass
class Segment:
    """One typed message inside a scenario."""
    text: str
    is_alert: bool = False
    is_sticky: bool = False
    footer_override: str = ""

    def __post_init__(self) -> None:
        # Mirror the device's shortcut expansion that happens before storage.
        self.text = expand_shortcuts(self.text)


@dataclass
class Scenario:
    name: str
    description: str
    segments: List[Segment]
    tunables: Tunables = field(default_factory=demo_tunables)
    show_footer: bool = True
    idle_intro_ms: int = 0           # matrix rain before the first message
    idle_outro_ms: int = 0           # matrix rain after final clear
    sticky_loops: int = 2            # how many times sticky re-types
    # If true, end the last segment with a clear_queue snap (type → hold
    # → idle) instead of the usual clear→home→loop. Matches what the
    # device does when something calls clear_queue / send_msg.py --clear
    # while a single-message queue is being displayed. With a single
    # non-sticky message the device otherwise re-types forever (`cur_idx
    # = (cur_idx+1) % msg_queue.size()`), so idle rain only resumes when
    # the queue is actually emptied.
    clear_queue_at_end: bool = False
    fps: int = FPS
    duration_ms: int = 0             # auto-computed from segments if 0

    def __post_init__(self) -> None:
        if self.duration_ms <= 0:
            self.duration_ms = self._compute_duration_ms()

    def _compute_duration_ms(self) -> int:
        total = self.idle_intro_ms + self.idle_outro_ms
        for i, seg in enumerate(self.segments):
            loops = self.sticky_loops if seg.is_sticky else 1
            phases = _segment_phases(seg, self.tunables, loops)
            is_last = i == len(self.segments) - 1
            if self.clear_queue_at_end and is_last:
                phases = [(p, d) for p, d in phases if p in ("type", "hold")]
            total += sum(d for _, d in phases)
        # Drop the final "home" pause for non-rain-outro scenarios so we
        # don't trail with a black cursor screen.
        if (self.idle_outro_ms == 0
                and self.segments
                and not self.clear_queue_at_end):
            total -= self.tunables.clear_pause_ms
        return total


def _segment_phases(seg: Segment, t: Tunables, loops: int = 1) -> List[Tuple[str, int]]:
    """Per-segment timeline: (phase_name, duration_ms).

    Phases: 'type', 'hold', 'clear', 'home'. Sticky loops 'type→hold→clear→home'
    `loops` times; non-sticky goes once."""
    type_ms = int(1000.0 * len(seg.text) / t.typewriter_cps)
    out: List[Tuple[str, int]] = []
    n = max(1, loops if seg.is_sticky else 1)
    for _ in range(n):
        out.append(("type", type_ms))
        out.append(("hold", t.message_hold_ms))
        out.append(("clear", t.ghost_duration_ms))
        out.append(("home", t.clear_pause_ms))
    return out


def build_subtitles(scenario: Scenario) -> List[SubtitleEntry]:
    """Generate subtitle entries synchronized with the scenario timeline.

    One entry per segment, timed from the start of the 'type' phase through
    the end of the 'hold' phase. Sticky segments get one entry per loop.
    Idle rain intro/outro produce no subtitles.
    """
    entries: List[SubtitleEntry] = []
    now_ms = scenario.idle_intro_ms

    for seg in scenario.segments:
        loops = scenario.sticky_loops if seg.is_sticky else 1
        phases = _segment_phases(seg, scenario.tunables, loops)
        text = seg.text  # already shortcut-expanded

        type_start: Optional[int] = None
        for phase_name, dur in phases:
            if phase_name == "type":
                type_start = now_ms
            elif phase_name == "hold" and type_start is not None:
                entries.append(SubtitleEntry(
                    start_ms=type_start,
                    end_ms=now_ms + dur,
                    text=text,
                ))
                type_start = None
            elif phase_name in ("clear", "home"):
                type_start = None
            now_ms += dur

    return entries


def build_frame(scenario: Scenario, ms: int) -> Frame:
    """Resolve the scenario's state at absolute time `ms` into a Frame."""
    t = scenario.tunables
    # Idle intro
    if ms < scenario.idle_intro_ms:
        return Frame(rain=True, fg_rgb=t.text_rgb)
    rel = ms - scenario.idle_intro_ms

    # Walk segments
    total_groups = len(scenario.segments)
    for seg_idx, seg in enumerate(scenario.segments):
        loops = scenario.sticky_loops if seg.is_sticky else 1
        phases = _segment_phases(seg, t, loops)
        is_last = seg_idx == len(scenario.segments) - 1
        if scenario.clear_queue_at_end and is_last:
            phases = [(p, d) for p, d in phases if p in ("type", "hold")]
        seg_dur = sum(d for _, d in phases)
        if rel < seg_dur:
            return _frame_within_segment(
                scenario, seg, seg_idx, total_groups, rel, phases,
            )
        rel -= seg_dur

    # All segments done — idle outro?
    if scenario.idle_outro_ms > 0 and rel < scenario.idle_outro_ms:
        # Use absolute ms so rain animation continues smoothly through transition
        return Frame(rain=True, fg_rgb=t.text_rgb)

    # Otherwise hold on the last cleared state.
    return Frame(fg_rgb=t.text_rgb)


def _frame_within_segment(
    scenario: Scenario,
    seg: Segment,
    seg_idx: int,
    total_groups: int,
    rel_ms: int,
    phases: List[Tuple[str, int]],
) -> Frame:
    t = scenario.tunables
    fg = t.alert_rgb if seg.is_alert else t.text_rgb
    footer_num = seg_idx + 1
    if scenario.show_footer:
        if seg.footer_override:
            footer = seg.footer_override
        else:
            footer = (f"- {footer_num}/{total_groups} -"
                      if seg.is_sticky
                      else f"{footer_num}/{total_groups}")
    else:
        footer = None

    # Find which phase we're in within this segment
    cum = 0
    for phase_name, dur in phases:
        if rel_ms < cum + dur:
            phase_rel = rel_ms - cum
            return _frame_within_phase(
                scenario, seg, seg_idx, footer, fg, phase_name, phase_rel, dur, cum, phases,
            )
        cum += dur
    return Frame(fg_rgb=fg, footer=footer)


def _frame_within_phase(
    scenario: Scenario,
    seg: Segment,
    seg_idx: int,
    footer: Optional[str],
    fg: Tuple[int, int, int],
    phase: str,
    phase_rel: int,
    phase_dur: int,
    phase_start_within_seg: int,
    phases: List[Tuple[str, int]],
) -> Frame:
    t = scenario.tunables
    if phase == "type":
        # chars typed so far
        full_chars = len(seg.text)
        typed = min(full_chars, int(phase_rel / 1000.0 * t.typewriter_cps))
        partial = seg.text[:typed]
        lines = visible_window(wrap_text(partial))
        cur = Cursor(
            visible=True,
            blinking=False,
            x=cursor_end_x(lines),
            y=cursor_row_y(lines),
        )
        return Frame(
            visible_lines=lines,
            fg_rgb=fg,
            cursor=cur,
            footer=footer,
        )
    elif phase == "hold":
        lines = visible_window(wrap_text(seg.text))
        cur = Cursor(
            visible=True,
            blinking=True,
            x=cursor_end_x(lines),
            y=cursor_row_y(lines),
        )
        return Frame(
            visible_lines=lines,
            fg_rgb=fg,
            cursor=cur,
            footer=footer,
        )
    elif phase == "clear":
        # Ghost decays over phase_dur (which equals ghost_duration_ms)
        ghost_lines = visible_window(wrap_text(seg.text))
        return Frame(
            visible_lines=[],
            fg_rgb=fg,
            ghost=Ghost(
                lines=ghost_lines,
                age_ms=phase_rel,
                is_alert=seg.is_alert,
            ),
            footer=footer,
        )
    elif phase == "home":
        cur = Cursor(visible=True, blinking=True, x=MG, y=MG)
        return Frame(
            visible_lines=[],
            fg_rgb=fg,
            cursor=cur,
            footer=footer,
        )
    return Frame(fg_rgb=fg, footer=footer)


def get_unique_path(path: Path) -> Path:
    """If path exists, add _01, _02 etc. before extension until free."""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix

    # Check if stem already ends in _NN
    match = re.search(r'_(\d+)$', stem)
    if match:
        base = stem[:match.start()]
        num = int(match.group(1)) + 1
    else:
        base = stem
        num = 1

    while True:
        candidate = parent / f"{base}_{num:02d}{suffix}"
        if not candidate.exists():
            return candidate
        num += 1


# ───────────────────────── Encoder ─────────────────────────────────────────
def encode_mp4(scenario: Scenario, out_path: Path,
               title: Optional[str] = None,
               description: Optional[str] = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(scenario.duration_ms / 1000.0 * scenario.fps)
    print(f"  rendering {n_frames} frames @ {scenario.fps} fps → {out_path.name}")

    # ffmpeg command: read rgb24 frames from stdin, encode H.264 with yuv420p
    target_w, target_h = W * SCALE, H * SCALE
    cmd: List[str] = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{target_w}x{target_h}",
        "-r", str(scenario.fps),
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "20",
        "-movflags", "+faststart",
    ]
    if title:
        cmd.extend(["-metadata", f"title={title}"])
    if description:
        cmd.extend(["-metadata", f"description={description}"])
    cmd.extend([
        "-f", "mp4",  # Force mp4 container even if extension is .mp3
        str(out_path),
    ])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None

    for i in range(n_frames):
        ms = int(i / scenario.fps * 1000)
        frame = build_frame(scenario, ms)
        arr = render_frame(frame, ms, scenario.tunables)
        # Nearest-upscale
        if SCALE != 1:
            arr = np.repeat(np.repeat(arr, SCALE, axis=0), SCALE, axis=1)
        proc.stdin.write(arr.tobytes())

    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited {rc}")
    print(f"  ✓ {out_path.relative_to(REPO_ROOT)} ({out_path.stat().st_size // 1024} KiB)")


def write_vtt(entries: List[SubtitleEntry], path: Path) -> None:
    """Write subtitle entries as a WebVTT file.

    YouTube accepts .vtt uploads directly through YouTube Studio.
    """
    def _fmt(ms: int) -> str:
        h, r = divmod(ms, 3600000)
        m, r = divmod(r, 60000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n{_fmt(e.start_ms)} --> {_fmt(e.end_ms)}\n{e.text}\n\n")
    print(f"  ✓ subtitles: {path.relative_to(REPO_ROOT)}")


# ───────────────────────── Scenarios ───────────────────────────────────────
def scenario_basic() -> Scenario:
    return Scenario(
        name="basic",
        description="Short message types out, holds, clears with ghost, returns to home cursor.",
        segments=[Segment("Hello, world.")],
    )


def scenario_long() -> Scenario:
    msg = (
        "The quick brown fox jumps over the lazy dog. "
        "Pack my box with five dozen liquor jugs. "
        "How vexingly quick daft zebras jump. "
        "Sphinx of black quartz, judge my vow. "
        "Bright vixens jump; dozy fowl quack. "
        "Waltz, bad nymph, for quick jigs vex. "
        "Glib jocks quiz nymph to vex dwarf."
    )
    return Scenario(
        name="long",
        description="Long message wraps + scrolls within the 9-row visible area.",
        segments=[Segment(msg)],
    )


def scenario_alert() -> Scenario:
    return Scenario(
        name="alert",
        description="Amber alert message — same flow as normal, different colour.",
        segments=[Segment(":x: build failed", is_alert=True)],
    )


def scenario_sticky() -> Scenario:
    return Scenario(
        name="sticky",
        description="Sticky alert pins the cycle — footer shows '- 1/1 -' and the message re-types.",
        segments=[Segment(":heart: i love you", is_alert=True, is_sticky=True)],
        sticky_loops=2,
    )
    
    
def scenario_footer() -> Scenario:
    return Scenario(
        name="footer",
        description="Message with a custom footer override.",
        segments=[Segment("System Status: OK", footer_override="CPU: 42%")],
    )


def scenario_rain_message_rain() -> Scenario:
    return Scenario(
        name="rain",
        description=(
            "Idle matrix rain → a message arrives and types out → "
            "`clear_queue` is called and the queue empties → rain resumes. "
            "(Without the explicit clear, a single non-sticky message would "
            "re-loop instead — rain only returns when the queue is empty.)"
        ),
        segments=[Segment("INCOMING")],
        idle_intro_ms=2500,
        idle_outro_ms=3500,
        clear_queue_at_end=True,
    )


SCENARIOS = {
    s.name: s for s in [
        scenario_basic(),
        scenario_long(),
        scenario_alert(),
        scenario_sticky(),
        scenario_rain_message_rain(),
        scenario_footer(),
    ]
}


# ───────────────────────── CLI ─────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--all", action="store_true", help="Render every scenario")
    p.add_argument("--scenario", metavar="NAME", help="Render one scenario by name")
    p.add_argument("--list", action="store_true", help="List available scenarios")
    p.add_argument("--name", help="Output filename (for piped input or overriding scenario name)")
    p.add_argument("--footer", metavar="TEXT", default="",
                   help="Footer override for the piped/custom scenario "
                        "(replaces the x/N counter, same as send_msg.py --footer)")
    p.add_argument("--glow", type=int, help="Background phosphor glow intensity (0-255)")
    p.add_argument("--title", help="Title to embed in MP4 metadata")
    p.add_argument("--description", help="Description to embed in MP4 metadata")
    p.add_argument("--no-subtitles", action="store_true",
                   help="Skip generating a .vtt subtitle file alongside the MP4")
    args = p.parse_args()

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH", file=sys.stderr)
        return 1
    if not FONT_PATH.exists():
        print(f"ERROR: font missing: {FONT_PATH}", file=sys.stderr)
        return 1

    # Check for piped input
    piped_text = None
    if not sys.stdin.isatty():
        piped_text = sys.stdin.read().strip()

    if args.list:
        for s in SCENARIOS.values():
            print(f"  {s.name:<8} {s.description}")
        return 0

    targets: List[Scenario]
    if piped_text:
        targets = [Scenario(
            name="piped",
            description="Piped input from stdin",
            segments=[Segment(piped_text, footer_override=args.footer)],
        )]
    elif args.all:
        targets = list(SCENARIOS.values())
    elif args.scenario:
        if args.scenario not in SCENARIOS:
            print(f"ERROR: unknown scenario '{args.scenario}'", file=sys.stderr)
            return 1
        targets = [SCENARIOS[args.scenario]]
    else:
        p.print_help()
        return 1

    if args.glow is not None:
        for s in targets:
            s.tunables.background_glow = args.glow

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s in targets:
        print(f"[{s.name}] {s.description}")

        # Determine output filename
        if piped_text:
            filename = args.name or "piped.mp4"
        elif args.name and len(targets) == 1:
            filename = args.name
        else:
            filename = f"{s.name}.mp4"

        if "." not in filename:
            filename += ".mp4"

        out_path = get_unique_path(OUT_DIR / filename)
        encode_mp4(s, out_path, args.title, args.description)

        if not args.no_subtitles:
            sub_entries = build_subtitles(s)
            if sub_entries:
                vtt_path = out_path.with_suffix(".vtt")
                write_vtt(sub_entries, vtt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
