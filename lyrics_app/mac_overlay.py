"""Native macOS floating-lyrics overlay (AppKit via PyObjC).

Every song (lane) gets its own borderless, always-on-top window styled as a
frosted-glass card with a song-themed gradient. With a two-song mashup:

    * "Colors" — midnight-blue card, top-left, lyrics FALL with snow crystals
    * "Bags"   — sunset-amber card, bottom-right, lyrics RISE with glowing embers

Design details: Apple-Music-style header (artwork tile, title/artist, live
equalizer), rounded system font, karaoke sweep that fills the word being sung
letter by letter, glowing sung words, beat-reactive pulse, gradient progress
bar with time labels. Audio reactivity comes from `spectrum.Spectrum`.

Why not Tk on macOS: Apple's bundled Tk 8.5 is deprecated and renders
transparent / borderless windows as nothing at all on modern macOS.
"""
from __future__ import annotations

import json
import math
import random
import signal
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import objc
from AppKit import (
    NSAffineTransform, NSApp, NSApplication, NSApplicationActivationPolicyRegular,
    NSAttributedString, NSBackingStoreBuffered, NSBezierPath, NSColor,
    NSEventModifierFlagCommand, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
    NSGradient, NSGraphicsContext, NSKernAttributeName, NSMakePoint, NSMakeRect, NSMenu,
    NSMenuItem, NSScreen, NSShadow, NSShadowAttributeName, NSStatusWindowLevel, NSView,
    NSVisualEffectView, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

from .audio import AudioPlayer
from .lrc import Line, Lyrics

try:
    from .spectrum import Spectrum
except ImportError:  # numpy missing → visuals just aren't audio-reactive
    Spectrum = None  # type: ignore

try:
    from AppKit import NSFontDescriptorSystemDesignRounded as _ROUNDED
except ImportError:
    _ROUNDED = None

# ------------------------------------------------------------------ themes
THEMES: Dict[str, dict] = {
    "colors": dict(  # Halsey — Colors: midnight → electric blue, falling snow
        accent="#7CC8FF", glow="#3BA3FF",
        bg=["#07122B", "#0F2A63", "#1D4ED8"], art=["#93C5FD", "#1E40AF"],
        bar=["#38BDF8", "#818CF8"], particle="snow", particle_color="#E3F0FF", icon="❄"),
    "bags": dict(    # Clairo — Bags: warm sunset amber → rose, rising embers
        accent="#FFD166", glow="#FFB020",
        bg=["#1F0E06", "#5B2110", "#B4410F"], art=["#FDE68A", "#C2410C"],
        bar=["#FBBF24", "#FB7185"], particle="ember", particle_color="#FFC56B", icon="✦"),
}
FALLBACK_THEMES = [
    dict(accent="#B8F2A0", glow="#4ADE80", bg=["#06170D", "#0F3D24", "#15803D"],
         art=["#BBF7D0", "#166534"], bar=["#4ADE80", "#22D3EE"], particle="snow",
         particle_color="#E8FFF0", icon="♪"),
    dict(accent="#FF9BD2", glow="#F472B6", bg=["#1E0816", "#5B1640", "#BE185D"],
         art=["#FBCFE8", "#9D174D"], bar=["#F472B6", "#A78BFA"], particle="ember",
         particle_color="#FFC1E3", icon="♪"),
]

UNSUNG = "#FFFFFF"
SPACING = 2.3        # distance between consecutive lines (× font size)
MIN_SCALE = 0.48     # size of lines far from the middle (× current-line size)
MARGIN = 36          # distance from the screen edges
PARTICLES = 18       # snow crystals / embers per card
SWAY_SPEED = 0.45    # rad/s — slow, lazy side-to-side drift
RADIUS = 28.0        # card corner radius
_MATERIAL_HUD, _BLEND_BEHIND, _STATE_ACTIVE = 13, 0, 1
POS_FILE = Path(__file__).resolve().parent.parent / ".overlay_position.json"

W_REG, W_MED, W_BOLD, W_HEAVY = 0.0, 0.23, 0.4, 0.56  # NSFontWeight values


# ------------------------------------------------------------------ helpers
@lru_cache(maxsize=1024)
def _color(hexstr: str, alpha: float = 1.0):
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


@lru_cache(maxsize=512)
def _font(size: float, weight: float = W_BOLD, mono_digits: bool = False):
    if mono_digits:
        return NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    f = NSFont.systemFontOfSize_weight_(size, weight)
    if _ROUNDED is not None:
        desc = f.fontDescriptor().fontDescriptorWithDesign_(_ROUNDED)
        if desc is not None:
            f = NSFont.fontWithDescriptor_size_(desc, size) or f
    return f


@lru_cache(maxsize=128)
def _shadow(color: str, alpha: float, blur: float, dy: float = -1.0):
    s = NSShadow.alloc().init()
    s.setShadowColor_(_color(color, alpha))
    s.setShadowBlurRadius_(blur)
    s.setShadowOffset_((0, dy))
    return s


def _q(x: float, step: float) -> float:
    return round(round(x / step) * step, 3)


def _attr(text: str, size: float, color: str, alpha: float = 1.0, weight: float = W_BOLD,
          glow: Optional[str] = None, kern: float = 0.0, mono: bool = False):
    size, alpha = _q(size, 0.5), _q(alpha, 0.05)
    shadow = (_shadow(glow, _q(0.9 * alpha, 0.05), 14.0, 0.0) if glow
              else _shadow("#000000", _q(0.75 * alpha, 0.05), 5.0))
    attrs = {
        NSFontAttributeName: _font(size, weight, mono),
        NSForegroundColorAttributeName: _color(color, alpha),
        NSShadowAttributeName: shadow,
    }
    if kern:
        attrs[NSKernAttributeName] = kern
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


@lru_cache(maxsize=8192)
def _width(text: str, size: float, weight: float = W_BOLD) -> float:
    return _attr(text, size, "#FFFFFF", weight=weight).size().width


@lru_cache(maxsize=64)
def _gradient(colors: Tuple[str, ...], alpha: float):
    return NSGradient.alloc().initWithColors_([_color(c, alpha) for c in colors])


@lru_cache(maxsize=256)
def _radial(color: str, alpha: float):
    return NSGradient.alloc().initWithStartingColor_endingColor_(
        _color(color, alpha), _color(color, 0.0))


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _smooth(x: float) -> float:
    x = _clamp(x)
    return x * x * (3 - 2 * x)


def _fmt(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


def lane_theme(lyr: Lyrics, k: int) -> dict:
    title = (lyr.meta.get("ti") or "").strip().lower()
    return THEMES.get(title) or FALLBACK_THEMES[k % len(FALLBACK_THEMES)]


# --------------------------------------------------------------- Cocoa classes
class _OverlayWindow(NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class _LyricsView(NSView):
    def isFlipped(self):
        return True  # top-left origin

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        self.ctl.draw(self.lane_index, self.bounds().size)

    def keyDown_(self, event):
        if not self.ctl.key(event):
            objc.super(_LyricsView, self).keyDown_(event)

    def mouseDown_(self, event):
        self.window().performWindowDragWithEvent_(event)

    def rightMouseDown_(self, event):
        NSMenu.popUpContextMenu_withEvent_forView_(self.ctl.menu, event, self)

    def tick_(self, timer):
        self.ctl.tick()

    def menuAction_(self, sender):
        self.ctl.command(sender.representedObject())


# --------------------------------------------------------------- particles
class _Particle:
    __slots__ = ("x", "y", "r", "v", "phase", "amp", "alpha", "spin", "rot", "crystal")

    def __init__(self, w: float, h: float, anywhere: bool, direction: int, kind: str):
        self.r = random.uniform(1.2, 3.0) if kind == "ember" else random.uniform(1.0, 2.6)
        self.x = random.uniform(0, w)
        self.y = random.uniform(0, h) if anywhere else (-8.0 if direction > 0 else h + 8.0)
        self.v = (4 + self.r * 4) * direction           # slow, like real snow
        self.phase = random.uniform(0, math.tau)
        self.amp = random.uniform(4, 12)
        self.alpha = random.uniform(0.35, 0.8)
        self.spin = random.uniform(-25, 25)              # deg/s for crystals
        self.rot = random.uniform(0, 360)
        self.crystal = kind == "snow" and random.random() < 0.35


class _Lane:
    """One song: lyrics, theme, motion direction, alignment and window."""

    def __init__(self, lyr: Lyrics, k: int, n: int):
        self.lyr, self.k = lyr, k
        self.theme = lane_theme(lyr, k)
        self.color = self.theme["accent"]
        if n == 1:
            self.direction, self.align, self.corner = +1, "center", "bottom-center"
        elif k % 2 == 0:
            self.direction, self.align, self.corner = +1, "left", "top-left"       # falls
        else:
            self.direction, self.align, self.corner = -1, "right", "bottom-right"  # rises
        self.particles: List[_Particle] = []
        self.layout_cache: dict = {}
        self.eq: List[float] = []
        self.win = self.view = self.fx = None


# --------------------------------------------------------------- controller
class MacFloatingLyrics:
    def __init__(self, lanes: Sequence[Lyrics], player: AudioPlayer, title: str = "",
                 width: Optional[int] = None, font_size: int = 46, loop: bool = True,
                 offset: float = 0.0, backdrop: bool = True, snow: bool = True):
        n = max(1, len(lanes))
        self.lanes = [_Lane(l, k, n) for k, l in enumerate(lanes)]
        self.player, self.title = player, title
        self.loop, self.offset, self.fs, self.snow = loop, offset, font_size, snow
        self.backdrop = backdrop
        self.lane_w = width or (int(font_size * 17) if n > 1 else int(font_size * 26))
        self.toast, self.toast_until = "", 0.0
        self._last_tick = time.monotonic()
        self._dt = 1 / 60
        self.level = 0.0
        self.spectrum = None
        if Spectrum is not None:
            try:
                self.spectrum = Spectrum.from_file(Path(player.path))
            except Exception:
                self.spectrum = None

        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self._build_main_menu()
        saved = self._load_positions()
        for lane in self.lanes:
            self._make_window(lane, saved.get(str(lane.k)))
        self.menu = self._build_context_menu()

    # ------------------------------------------------------------ windows
    def _height(self) -> int:
        return int(self.fs * 9.0)

    def _default_origin(self, lane: _Lane, w: float, h: float) -> Tuple[float, float]:
        f = NSScreen.mainScreen().visibleFrame()  # Cocoa: origin bottom-left
        left, right = f.origin.x + MARGIN, f.origin.x + f.size.width - w - MARGIN
        bottom, top = f.origin.y + MARGIN, f.origin.y + f.size.height - h - MARGIN
        return {
            "top-left": (left, top),
            "bottom-right": (right, bottom),
            "bottom-center": (f.origin.x + (f.size.width - w) / 2, bottom),
        }[lane.corner]

    def _make_window(self, lane: _Lane, saved: Optional[dict]) -> None:
        f = NSScreen.mainScreen().visibleFrame()
        w = min(self.lane_w, f.size.width / 2 - MARGIN) if len(self.lanes) > 1 else self.lane_w
        h = min(self._height(), (f.size.height - 3 * MARGIN) / max(1, len(self.lanes)))
        x, y = self._default_origin(lane, w, h)
        if saved and self._on_screen(saved["x"], saved["y"], w, h):
            x, y = saved["x"], saved["y"]

        win = _OverlayWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(True)
        win.setLevel_(NSStatusWindowLevel)
        win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                   | NSWindowCollectionBehaviorFullScreenAuxiliary
                                   | NSWindowCollectionBehaviorStationary)
        win.setHidesOnDeactivate_(False)
        win.setReleasedWhenClosed_(False)
        win.setTitle_(f"Floating Lyrics — {lane.lyr.caption or lane.k + 1}")

        fx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        fx.setMaterial_(_MATERIAL_HUD)
        fx.setBlendingMode_(_BLEND_BEHIND)
        fx.setState_(_STATE_ACTIVE)
        fx.setWantsLayer_(True)
        fx.layer().setCornerRadius_(RADIUS)
        fx.layer().setMasksToBounds_(True)
        fx.setAutoresizingMask_(18)
        fx.setHidden_(not self.backdrop)
        view = _LyricsView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        view.ctl, view.lane_index = self, lane.k
        view.setAutoresizingMask_(18)
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        root.addSubview_(fx)
        root.addSubview_(view)
        win.setContentView_(root)
        win.makeFirstResponder_(view)
        lane.win, lane.view, lane.fx = win, view, fx

    @staticmethod
    def _on_screen(x, y, w, h) -> bool:
        for s in NSScreen.screens():
            f = s.visibleFrame()
            if (f.origin.x - 1 <= x and x + w <= f.origin.x + f.size.width + 1
                    and f.origin.y - 1 <= y and y + h <= f.origin.y + f.size.height + 1):
                return True
        return False

    def _load_positions(self) -> dict:
        try:
            data = json.loads(POS_FILE.read_text())
            return data.get("lanes", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_positions(self) -> None:
        data = {str(l.k): {"x": l.win.frame().origin.x, "y": l.win.frame().origin.y}
                for l in self.lanes}
        try:
            POS_FILE.write_text(json.dumps({"lanes": data}))
        except OSError:
            pass

    def _reset_positions(self) -> None:
        for lane in self.lanes:
            fr = lane.win.frame()
            lane.win.setFrameOrigin_(NSMakePoint(*self._default_origin(lane, fr.size.width, fr.size.height)))
        self.save_positions()

    # ------------------------------------------------------------ menus
    def _build_main_menu(self) -> None:
        bar = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        bar.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit Floating Lyrics", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        self.app.setMainMenu_(bar)

    def _build_context_menu(self):
        m = NSMenu.alloc().init()
        items = [("Play / Pause   (Space)", "toggle"), ("Restart   (R)", "restart"),
                 ("Back 5s   (←)", "back"), ("Forward 5s   (→)", "fwd"), None,
                 ("Bigger text   (+)", "bigger"), ("Smaller text   (−)", "smaller"),
                 ("Lyrics earlier   ([)", "earlier"), ("Lyrics later   (])", "later"),
                 ("Toggle background   (B)", "backdrop"),
                 ("Toggle particles   (S)", "snow"), ("Toggle loop   (L)", "loop"),
                 ("Reset positions   (C)", "center"), None, ("Quit   (Esc)", "quit")]
        for it in items:
            if it is None:
                m.addItem_(NSMenuItem.separatorItem())
                continue
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(it[0], "menuAction:", "")
            mi.setTarget_(self.lanes[0].view)
            mi.setRepresentedObject_(it[1])
            m.addItem_(mi)
        return m

    # ----------------------------------------------------------- input
    def key(self, event) -> bool:
        if event.modifierFlags() & NSEventModifierFlagCommand:
            return False
        chars = (event.charactersIgnoringModifiers() or "").lower()
        cmd = {123: "back", 124: "fwd", 126: "volup", 125: "voldown", 53: "quit"}.get(
            event.keyCode()) or {
            " ": "toggle", "r": "restart", "l": "loop", "c": "center", "s": "snow",
            "b": "backdrop", "+": "bigger", "=": "bigger", "-": "smaller", "[": "earlier",
            "]": "later", "q": "quit"}.get(chars)
        if cmd:
            self.command(cmd)
            return True
        return False

    def command(self, cmd: str) -> None:
        p = self.player
        if cmd == "toggle":
            p.toggle_pause(); self._say("⏯")
        elif cmd == "restart":
            p.play(0); self._say("⏮ restart")
        elif cmd == "back":
            p.seek(-5); self._say("⏪ −5s")
        elif cmd == "fwd":
            p.seek(5); self._say("⏩ +5s")
        elif cmd == "volup":
            self._say(f"volume {int(p.volume(0.1) * 100)}%")
        elif cmd == "voldown":
            self._say(f"volume {int(p.volume(-0.1) * 100)}%")
        elif cmd in ("bigger", "smaller"):
            self._resize(4 if cmd == "bigger" else -4)
        elif cmd in ("earlier", "later"):
            self.offset = round(self.offset + (0.1 if cmd == "earlier" else -0.1), 2)
            self._say(f"sync {self.offset:+.1f}s")
        elif cmd == "loop":
            self.loop = not self.loop; self._say(f"loop {'on' if self.loop else 'off'}")
        elif cmd == "backdrop":
            self.backdrop = not self.backdrop
            for lane in self.lanes:
                lane.fx.setHidden_(not self.backdrop)
                lane.win.invalidateShadow()
        elif cmd == "snow":
            self.snow = not self.snow; self._say(f"particles {'on' if self.snow else 'off'}")
        elif cmd == "center":
            self._reset_positions()
        elif cmd == "quit":
            self.quit()

    def _say(self, msg: str) -> None:
        self.toast, self.toast_until = msg, time.monotonic() + 1.3

    def _resize(self, d: int) -> None:
        self.fs = max(18, min(64, self.fs + d))
        h = self._height()
        for lane in self.lanes:
            fr = lane.win.frame()
            y = fr.origin.y + fr.size.height - h if lane.direction > 0 else fr.origin.y
            lane.win.setFrame_display_(NSMakeRect(fr.origin.x, y, fr.size.width, h), True)
            lane.particles, lane.layout_cache = [], {}
        self._say(f"Aa {self.fs}")

    # ---------------------------------------------------------- text layout
    @staticmethod
    def _wrap(tokens: List[str], size: float, limit: float) -> List[List[int]]:
        space = _width(" ", size)
        rows, row, width = [], [], 0.0
        for i, tok in enumerate(tokens):
            w = _width(tok, size)
            if row and width + space + w > limit:
                rows.append(row)
                row, width = [], 0.0
            width += (space if row else 0) + w
            row.append(i)
        if row:
            rows.append(row)
        return rows

    def _layout(self, lane: _Lane, line: Line, limit: float):
        key = (id(line), self.fs, int(limit))
        if key not in lane.layout_cache:
            tokens = [w.text.strip() for w in line.words] or line.text.split()
            size = float(self.fs)
            while True:
                rows = self._wrap(tokens, size, limit)
                if (len(rows) <= 2 and all(_width(t, size) <= limit for t in tokens)) or size <= 12:
                    break
                size -= 2
            lane.layout_cache[key] = (tokens, size, rows)
        return lane.layout_cache[key]

    def _draw_line(self, lane: _Lane, line: Line, ax: float, cy: float, limit: float,
                   scale: float, alpha: float, t: Optional[float]) -> None:
        tokens, base, rows = self._layout(lane, line, limit)
        size = _q(base * scale, 0.5)
        starts = [w.start for w in line.words] or [line.start] * len(tokens)
        ends = starts[1:] + [min(line.end, starts[-1] + 0.9) if starts else line.start]
        f = _font(size)
        lh = f.ascender() - f.descender() + f.leading() + 2
        space = _width(" ", size)
        y = cy - lh * len(rows) / 2
        th = lane.theme
        for row in rows:
            widths = [_width(tokens[i], size) for i in row]
            total = sum(widths) + space * (len(row) - 1)
            x = {"left": ax, "right": ax - total}.get(lane.align, ax - total / 2)
            for i, w in zip(row, widths):
                if t is None:  # neighbouring line: soft white
                    _attr(tokens[i], size, UNSUNG, alpha * 0.78).drawAtPoint_(NSMakePoint(x, y))
                else:
                    prog = _clamp((t - starts[i]) / max(0.12, min(ends[i] - starts[i], 1.2)))
                    if prog >= 1.0:
                        _attr(tokens[i], size, th["accent"], alpha, glow=th["glow"]).drawAtPoint_(
                            NSMakePoint(x, y))
                    else:
                        _attr(tokens[i], size, UNSUNG, alpha).drawAtPoint_(NSMakePoint(x, y))
                        if prog > 0:  # karaoke sweep: fill the word letter by letter
                            NSGraphicsContext.saveGraphicsState()
                            NSBezierPath.clipRect_(NSMakeRect(x - 2, y - lh, w * prog + 2, lh * 3))
                            _attr(tokens[i], size, th["accent"], alpha, glow=th["glow"]).drawAtPoint_(
                                NSMakePoint(x, y))
                            NSGraphicsContext.restoreGraphicsState()
                x += w + space
            y += lh

    @staticmethod
    def _phase(lyr: Lyrics, t: float) -> float:
        """Continuous position in line units (steady snowfall with soft ends)."""
        if not len(lyr):
            return 0.0
        idx = lyr.index_at(t)
        if idx < 0:
            first = lyr.lines[0].start
            return -1 + _smooth((t - (first - 3.0)) / 3.0)
        line = lyr.lines[idx]
        end = line.end
        if math.isinf(end):
            last = line.words[-1].start if line.words else line.start
            end = max(last + 3.0, line.start + 4.0)
        frac = _clamp((t - line.start) / max(0.3, end - line.start))
        return idx + 0.75 * frac + 0.25 * _smooth(frac)

    # ---------------------------------------------------------- card pieces
    def _draw_card(self, lane: _Lane, W: float, H: float) -> None:
        th = lane.theme
        card = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, W, H), RADIUS, RADIUS)
        # diagonal song gradient over the frosted glass
        angle = 65 if lane.direction > 0 else -115
        _gradient(tuple(th["bg"]), 0.62).drawInBezierPath_angle_(card, angle)
        NSGraphicsContext.saveGraphicsState()
        card.addClip()
        # glossy sheen across the top
        NSGradient.alloc().initWithColors_([
            NSColor.colorWithWhite_alpha_(1.0, 0.10), NSColor.colorWithWhite_alpha_(1.0, 0.0)]
        ).drawInRect_angle_(NSMakeRect(0, 0, W, H * 0.45), 90)
        # soft accent bloom (breathes with the beat) where the lyrics enter
        cxr = 0.85 if lane.align == "left" else -0.85
        _radial(th["glow"], _q(0.16 + 0.14 * self.level, 0.02)).drawInRect_relativeCenterPosition_(
            NSMakeRect(0, 0, W, H), (cxr, -0.9 * lane.direction))
        NSGraphicsContext.restoreGraphicsState()
        # border that breathes with the beat
        inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.75, 0.75, W - 1.5, H - 1.5), RADIUS, RADIUS)
        inner.setLineWidth_(1.5)
        _color(th["accent"], _q(0.35 + 0.35 * self.level, 0.05)).setStroke()
        inner.stroke()

    def _draw_particles(self, lane: _Lane, W: float, H: float, now: float) -> None:
        kind = lane.theme["particle"]
        pc = lane.theme["particle_color"]
        if not lane.particles:
            lane.particles = [_Particle(W, H, True, lane.direction, kind) for _ in range(PARTICLES)]
        boost = 1.0 + 0.8 * self.level
        for pt in lane.particles:
            pt.y += pt.v * self._dt * boost
            pt.rot += pt.spin * self._dt
            if pt.y > H + 8 or pt.y < -8:
                pt.__init__(W, H, False, lane.direction, kind)
            x = pt.x + math.sin(now * 0.6 + pt.phase) * pt.amp
            edge = _smooth(min(pt.y, H - pt.y) / 50)
            if kind == "ember":
                flicker = 0.75 + 0.25 * math.sin(now * 6 + pt.phase * 3)
                a = pt.alpha * edge * flicker
                r = pt.r * 3.2
                _radial(pc, _q(a * 0.55, 0.05)).drawInBezierPath_relativeCenterPosition_(
                    NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r, pt.y - r, 2 * r, 2 * r)), (0, 0))
                _color("#FFF4D6", _q(a, 0.05)).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x - pt.r * 0.7, pt.y - pt.r * 0.7, pt.r * 1.4, pt.r * 1.4)).fill()
            elif pt.crystal:
                s = _attr("❄", 7 + pt.r * 4, pc, _q(pt.alpha * edge, 0.05), weight=W_REG)
                sz = s.size()
                NSGraphicsContext.saveGraphicsState()
                tr = NSAffineTransform.transform()
                tr.translateXBy_yBy_(x, pt.y)
                tr.rotateByDegrees_(pt.rot)
                tr.concat()
                s.drawAtPoint_(NSMakePoint(-sz.width / 2, -sz.height / 2))
                NSGraphicsContext.restoreGraphicsState()
            else:
                _color(pc, _q(pt.alpha * edge * 0.85, 0.05)).setFill()
                NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(x - pt.r, pt.y - pt.r, pt.r * 2, pt.r * 2)).fill()

    def _draw_header(self, lane: _Lane, W: float, H: float, pad: float, t: float) -> float:
        """Title/artist + live equalizer. Returns header height."""
        th, fs = lane.theme, self.fs
        a = round(fs * 1.05)  # header block height
        hy = 18 if lane.direction > 0 else H - 18 - a
        left = lane.align != "right"

        # title + artist, flush with the card's text edge
        ttl = _attr(lane.lyr.meta.get("ti") or f"Song {lane.k + 1}", fs * 0.46, "#FFFFFF",
                    weight=W_HEAVY, kern=0.2)
        art = _attr((lane.lyr.meta.get("ar") or "").upper(), fs * 0.27, th["accent"], 0.95,
                    weight=W_BOLD, kern=1.6)
        for s, yy in ((ttl, hy + a * 0.04), (art, hy + a * 0.58)):
            sx = pad if left else W - pad - s.size().width
            s.drawAtPoint_(NSMakePoint(sx, yy))

        # live equalizer on the opposite side
        n_bars = 7
        bw, bg = max(3.0, fs * 0.09), max(3.0, fs * 0.08)
        eq_w = n_bars * bw + (n_bars - 1) * bg
        ex = W - pad - eq_w if left else pad
        vals = (self.spectrum.bands_at(t) if (self.spectrum is not None and self.player.playing)
                else [0.0] * n_bars)
        if len(lane.eq) != n_bars:
            lane.eq = [0.0] * n_bars
        for i in range(n_bars):
            target = float(vals[i % len(vals)])
            cur = lane.eq[i]
            lane.eq[i] = cur + (target - cur) * (0.6 if target > cur else 0.12)  # fast up, slow down
            bh = max(bw, lane.eq[i] * a * 0.8)
            bx = ex + i * (bw + bg)
            by = hy + a * 0.9 - bh
            bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(bx, by, bw, bh), bw / 2, bw / 2)
            _gradient(tuple(th["bar"]), 0.95).drawInBezierPath_angle_(bar, -90)
        return a + 18

    def _draw_progress(self, lane: _Lane, W: float, H: float, pad: float, pos: float) -> float:
        """Gradient progress bar + time labels on the side opposite the header."""
        if self.player.length <= 0:
            return 0
        th, fs = lane.theme, self.fs
        small = max(10, fs * 0.25)
        cur = _attr(_fmt(pos), small, "#FFFFFF", 0.75, weight=W_MED, mono=True)
        tot = _attr(_fmt(self.player.length), small, "#FFFFFF", 0.75, weight=W_MED, mono=True)
        lh = cur.size().height
        by = H - 20 - lh / 2 if lane.direction > 0 else 20 + lh / 2
        x0 = pad + cur.size().width + 12
        x1 = W - pad - tot.size().width - 12
        frac = _clamp(pos / self.player.length)
        track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x0, by - 2, x1 - x0, 4), 2, 2)
        NSColor.colorWithWhite_alpha_(1.0, 0.16).setFill()
        track.fill()
        if frac > 0:
            fill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x0, by - 2, max(4, (x1 - x0) * frac), 4), 2, 2)
            _gradient(tuple(th["bar"]), 1.0).drawInBezierPath_angle_(fill, 0)
            hx = x0 + (x1 - x0) * frac
            NSGraphicsContext.saveGraphicsState()
            _shadow(th["glow"], 0.9, 8.0, 0.0).set()
            _color("#FFFFFF").setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(hx - 4.5, by - 4.5, 9, 9)).fill()
            NSGraphicsContext.restoreGraphicsState()
        cur.drawAtPoint_(NSMakePoint(pad, by - lh / 2))
        tot.drawAtPoint_(NSMakePoint(W - pad - tot.size().width, by - lh / 2))
        return lh + 28

    # ---------------------------------------------------------- frame
    def draw(self, k: int, size) -> None:
        lane = self.lanes[k]
        W, H = size.width, size.height
        fs = self.fs
        now = time.monotonic()
        pos = self.player.position()
        t = pos + self.offset
        if k == 0:  # once per frame: timing + beat level
            self._dt = min(0.1, now - self._last_tick)
            self._last_tick = now
            target = self.spectrum.level_at(pos) if (self.spectrum and self.player.playing) else 0.0
            self.level += (target - self.level) * (0.5 if target > self.level else 0.08)

        pad = 28
        if self.backdrop:
            self._draw_card(lane, W, H)
        if self.snow:
            self._draw_particles(lane, W, H, now)
        head_h = self._draw_header(lane, W, H, pad, t)
        foot_h = self._draw_progress(lane, W, H, pad, pos)

        if lane.direction > 0:
            top, bottom = head_h + 8, H - foot_h
        else:
            top, bottom = foot_h, H - head_h - 8
        cy = (top + bottom) / 2
        anchor_x = {"left": pad, "right": W - pad}.get(lane.align, W / 2)
        limit = W - 2 * pad - fs * 0.5
        step = fs * SPACING
        lyr = lane.lyr
        p = self._phase(lyr, t)
        cur = lyr.index_at(t)
        pulse = 1.0 + 0.035 * self.level
        for i in range(max(0, int(p) - 3), min(len(lyr), int(p) + 4)):
            d = p - i - 0.5               # <0 upcoming, >0 past
            y = cy + d * step * lane.direction
            dist = abs(d)
            scale = MIN_SCALE + (1 - MIN_SCALE) * (1 - _smooth(dist / 1.1))
            if i == cur:
                scale *= pulse
            alpha = 1 - _smooth((dist - 0.5) / 1.1)
            edge = min(y - top, bottom - y)
            alpha *= _smooth((edge + fs * 0.3) / (fs * 1.3))
            if alpha <= 0.03:
                continue
            sway = math.sin(now * SWAY_SPEED + i * 1.9) * fs * 0.22
            self._draw_line(lane, lyr.lines[i], anchor_x + sway, y, limit, scale, alpha,
                            t if i == cur else None)

        # status pill (paused / toast) on the last card
        if k == len(self.lanes) - 1:
            msg = ("paused — press Space" if not self.player.playing
                   else self.toast if (self.toast and now < self.toast_until) else "")
            if msg:
                s = _attr(msg, max(11, fs * 0.3), "#FFFFFF", weight=W_BOLD)
                sw, sh = s.size().width + 24, s.size().height + 10
                px = (W - sw) / 2
                py = top + 4 if lane.direction < 0 else bottom - sh - 4
                pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(px, py, sw, sh), sh / 2, sh / 2)
                NSColor.colorWithWhite_alpha_(0.0, 0.45).setFill()
                pill.fill()
                s.drawAtPoint_(NSMakePoint(px + 12, py + 5))

    # -------------------------------------------------------- lifecycle
    def tick(self) -> None:
        if self.player.finished:
            if self.loop:
                self.player.play(0)
            else:
                self.quit()
                return
        for lane in self.lanes:
            lane.view.setNeedsDisplay_(True)

    def run(self) -> None:
        self.player.play(0)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 40, self.lanes[0].view, "tick:", None, True)  # 40 fps: smooth for slow drift, light on CPU
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        signal.signal(signal.SIGTERM, lambda *_: self.quit())
        for lane in reversed(self.lanes):
            lane.win.orderFrontRegardless()
        self.lanes[0].win.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)
        AppHelper.runEventLoop(installInterrupt=True)  # Ctrl+C friendly

    def quit(self) -> None:
        self.save_positions()
        try:
            self.player.close()
        except Exception:
            pass
        AppHelper.stopEventLoop()
        NSApp().terminate_(None)
