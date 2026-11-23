"""Transparent, always-on-top, draggable karaoke overlay (Tkinter).

Renders one or more *lanes* side by side (a mashup = one lane per song).
Each lane shows: caption, previous line, the current line with word-by-word
karaoke highlight, and the next line.
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

from .audio import AudioPlayer
from .lrc import Line, Lyrics

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
FAMILY = "Helvetica Neue" if IS_MAC else "Segoe UI" if IS_WIN else "DejaVu Sans"

THEME = {
    "sung": "#FFD166",       # words already sung
    "unsung": "#FFFFFF",     # current line, not yet sung
    "dim": "#B9B9C8",        # previous / next lines
    "caption": "#FF8FB1",
    "shadow": "#000000",
    "accent": "#FF5C8A",     # progress bar
    "track": "#3A3A48",
    "panel": "#101018",      # used only when real transparency is unavailable
}
LANE_ACCENTS = ["#FFD166", "#7FDBFF", "#B8F2A0", "#FFB3E6"]
FPS = 60
SLIDE_MS = 240


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


@lru_cache(maxsize=256)
def _font(size: int, bold: bool = True) -> tkfont.Font:
    return tkfont.Font(family=FAMILY, size=max(10, size), weight="bold" if bold else "normal")


class _LaneState:
    def __init__(self) -> None:
        self.idx = -2
        self.t0 = 0.0


class FloatingLyrics:
    def __init__(self, lanes: Sequence[Lyrics], player: AudioPlayer, title: str = "",
                 width: Optional[int] = None, font_size: int = 36, loop: bool = True,
                 offset: float = 0.0):
        self.lanes = list(lanes)
        self.player, self.title = player, title
        self.loop, self.offset = loop, offset
        self.width = width or (980 if len(self.lanes) == 1 else 560 * len(self.lanes))
        self.state = [_LaneState() for _ in self.lanes]
        self._toast: Optional[str] = None
        self._toast_until = 0.0

        self.root = tk.Tk()
        self.root.title("Floating Lyrics")
        self.font_size = font_size
        self.height = self._calc_height()
        self._setup_window()
        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height,
                                bg=self._bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._bind()

    # ------------------------------------------------------------ window
    def _calc_height(self) -> int:
        return int(self.font_size * 7.2)

    def _setup_window(self) -> None:
        r = self.root
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x, y = (sw - self.width) // 2, sh - self.height - 110
        r.geometry(f"{self.width}x{self.height}+{x}+{y}")
        r.attributes("-topmost", True)
        self._bg = THEME["panel"]
        transparent = False
        if IS_MAC:
            try:  # borderless but still keyboard-focusable
                r.tk.call("::tk::unsupported::MacWindowStyle", "style", r._w, "plain", "none")
            except tk.TclError:
                r.overrideredirect(True)
            try:
                r.attributes("-transparent", True)
                r.config(bg="systemTransparent")
                self._bg, transparent = "systemTransparent", True
            except tk.TclError:
                pass
        elif IS_WIN:
            r.overrideredirect(True)
            key = "#010203"
            r.attributes("-transparentcolor", key)
            self._bg, transparent = key, True
        else:
            r.overrideredirect(True)
        if not transparent:
            r.attributes("-alpha", 0.88)
        r.config(bg=self._bg)

    def _resize(self, d: int) -> None:
        self.font_size = max(16, min(80, self.font_size + d))
        self.height = self._calc_height()
        self.root.geometry(f"{self.width}x{self.height}")
        self.canvas.config(height=self.height)
        self._toast_msg(f"Aa {self.font_size}")

    def _bind(self) -> None:
        r, p = self.root, self.player
        r.bind("<ButtonPress-1>", self._drag_start)
        r.bind("<B1-Motion>", self._drag_move)
        r.bind("<space>", lambda e: self._do(p.toggle_pause, "⏯"))
        r.bind("<Left>", lambda e: self._do(lambda: p.seek(-5), "⏪ −5s"))
        r.bind("<Right>", lambda e: self._do(lambda: p.seek(5), "⏩ +5s"))
        r.bind("<Up>", lambda e: self._toast_msg(f"vol {int(p.volume(0.1) * 100)}%"))
        r.bind("<Down>", lambda e: self._toast_msg(f"vol {int(p.volume(-0.1) * 100)}%"))
        for k in ("<plus>", "<equal>"):
            r.bind(k, lambda e: self._resize(+4))
        r.bind("<minus>", lambda e: self._resize(-4))
        r.bind("<bracketleft>", lambda e: self._nudge(-0.1))
        r.bind("<bracketright>", lambda e: self._nudge(+0.1))
        r.bind("<l>", lambda e: self._toggle_loop())
        r.bind("<r>", lambda e: self._do(lambda: p.play(0), "⏮ restart"))
        for k in ("<Escape>", "<q>"):
            r.bind(k, lambda e: self.close())
        r.bind("<Button-2>" if IS_MAC else "<Button-3>", self._menu)
        if IS_MAC:
            r.bind("<Control-Button-1>", self._menu)
        r.protocol("WM_DELETE_WINDOW", self.close)

        m = self.menu = tk.Menu(r, tearoff=0)
        m.add_command(label="Play / Pause   (Space)", command=p.toggle_pause)
        m.add_command(label="Restart   (R)", command=lambda: p.play(0))
        m.add_command(label="Back 5s   (←)", command=lambda: p.seek(-5))
        m.add_command(label="Forward 5s   (→)", command=lambda: p.seek(5))
        m.add_separator()
        m.add_command(label="Bigger text   (+)", command=lambda: self._resize(+4))
        m.add_command(label="Smaller text   (−)", command=lambda: self._resize(-4))
        m.add_command(label="Lyrics earlier   ([)", command=lambda: self._nudge(-0.1))
        m.add_command(label="Lyrics later   (])", command=lambda: self._nudge(+0.1))
        m.add_command(label="Toggle loop   (L)", command=self._toggle_loop)
        m.add_separator()
        m.add_command(label="Quit   (Esc)", command=self.close)

    # ----------------------------------------------------------- actions
    def _do(self, fn, msg: str) -> None:
        fn()
        self._toast_msg(msg)

    def _toast_msg(self, msg: str, secs: float = 1.2) -> None:
        self._toast, self._toast_until = msg, time.monotonic() + secs

    def _nudge(self, d: float) -> None:
        self.offset = round(self.offset - d, 2)  # "[" shows lyrics earlier
        self._toast_msg(f"sync {self.offset:+.1f}s")

    def _toggle_loop(self) -> None:
        self.loop = not self.loop
        self._toast_msg(f"loop {'on' if self.loop else 'off'}")

    def _menu(self, e) -> None:
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x, e.y
        self.root.focus_force()

    def _drag_move(self, e) -> None:
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    # ----------------------------------------------------------- layout
    def _text(self, x, y, s, font, fill, anchor="center") -> None:
        c = self.canvas
        o = max(1, int(font.cget("size")) // 18)
        for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o), (o + 1, o + 1)):
            c.create_text(x + dx, y + dy, text=s, font=font, fill=THEME["shadow"], anchor=anchor)
        c.create_text(x, y, text=s, font=font, fill=fill, anchor=anchor)

    @staticmethod
    def _wrap(tokens: List[str], font: tkfont.Font, limit: float) -> List[List[int]]:
        """Greedy word-wrap; returns rows of token indices."""
        space = font.measure(" ")
        rows, row, width = [], [], 0.0
        for i, tok in enumerate(tokens):
            w = font.measure(tok)
            if row and width + space + w > limit:
                rows.append(row)
                row, width = [], 0.0
            width += (space if row else 0) + w
            row.append(i)
        if row:
            rows.append(row)
        return rows

    def _layout(self, tokens: List[str], size: int, limit: float,
                max_rows: int) -> Tuple[tkfont.Font, List[List[int]]]:
        """Largest font (≤ size) at which the tokens wrap into ≤ max_rows."""
        while True:
            font = _font(size)
            rows = self._wrap(tokens, font, limit)
            if (len(rows) <= max_rows and all(font.measure(t) <= limit for t in tokens)) or size <= 12:
                return font, rows
            size -= 2

    def _draw_line(self, line: Line, cx: float, cy: float, limit: float, size: int,
                   t: Optional[float], color: str, max_rows: int) -> None:
        """Draw a (possibly wrapped) line centred on (cx, cy).

        When `t` is given, words are coloured karaoke-style.
        """
        tokens = [w.text.strip() for w in line.words] or line.text.split()
        starts = [w.start for w in line.words] or [line.start] * len(tokens)
        font, rows = self._layout(tokens, size, limit, max_rows)
        lh = font.metrics("linespace")
        space = font.measure(" ")
        y = cy - lh * (len(rows) - 1) / 2
        for row in rows:
            widths = [font.measure(tokens[i]) for i in row]
            x = cx - (sum(widths) + space * (len(row) - 1)) / 2
            for i, w in zip(row, widths):
                fill = color if t is None else (THEME["sung"] if t >= starts[i] else THEME["unsung"])
                self._text(x, y, tokens[i], font, fill, anchor="w")
                x += w + space
            y += lh

    def _draw_lane(self, k: int, lyr: Lyrics, t: float, x0: float, x1: float, now: float) -> None:
        fs, H = self.font_size, self.height
        cx, limit = (x0 + x1) / 2, (x1 - x0) - 28
        st = self.state[k]
        idx = lyr.index_at(t)
        if idx != st.idx:
            st.idx, st.t0 = idx, now
        slide = (1 - _ease((now - st.t0) * 1000 / SLIDE_MS)) * fs * 0.9

        if lyr.caption:
            accent = LANE_ACCENTS[k % len(LANE_ACCENTS)] if len(self.lanes) > 1 else THEME["caption"]
            self._text(cx, fs * 0.55, "♪ " + lyr.caption, _font(int(fs * 0.42)), accent)

        cy = H * 0.50 + slide
        side = int(fs * 0.55)
        prev, cur, nxt = lyr.line(idx - 1), lyr.line(idx), lyr.line(idx + 1)
        if prev:
            self._draw_line(prev, cx, cy - fs * 1.9, limit, side, None, THEME["dim"], 1)
        if cur:
            self._draw_line(cur, cx, cy, limit, fs, t, "", 2)
        elif nxt:
            self._text(cx, cy, "♪", _font(fs), THEME["dim"])
        if nxt:
            self._draw_line(nxt, cx, cy + fs * 1.9, limit, side, None, THEME["dim"], 1)

    def _frame(self) -> None:
        if self.player.finished:
            if self.loop:
                self.player.play(0)
                for st in self.state:
                    st.idx = -2
            else:
                self.close()
                return
        now = time.monotonic()
        pos = self.player.position()
        t = pos + self.offset
        c, W, H, fs = self.canvas, self.width, self.height, self.font_size
        c.delete("all")

        n = max(1, len(self.lanes))
        for k, lyr in enumerate(self.lanes):
            self._draw_lane(k, lyr, t, W * k / n, W * (k + 1) / n, now)
        for k in range(1, n):  # thin lane divider
            x = W * k / n
            c.create_line(x, H * 0.22, x, H * 0.82, fill=THEME["track"], width=2)

        if self.player.length > 0:
            frac = min(1.0, pos / self.player.length)
            by, bx0, bx1 = H - 10, W * 0.3, W * 0.7
            c.create_line(bx0, by, bx1, by, fill=THEME["track"], width=4, capstyle="round")
            c.create_line(bx0, by, bx0 + (bx1 - bx0) * frac, by, fill=THEME["accent"],
                          width=4, capstyle="round")
        meta = _font(max(11, int(fs * 0.32)), bold=False)
        if not self.player.playing:
            self._text(W / 2, H - 30, "paused — Space to play", meta, THEME["dim"])
        elif self.title and n == 1 and not self.lanes[0].caption:
            self._text(W / 2, fs * 0.55, self.title, meta, THEME["dim"])
        if self._toast and now < self._toast_until:
            self._text(W - 14, H - 30, self._toast, meta, THEME["sung"], anchor="e")

        self.root.after(int(1000 / FPS), self._frame)

    # --------------------------------------------------------- lifecycle
    def run(self) -> None:
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):  # Ctrl+C / kill → clean exit
            signal.signal(sig, lambda *_: self.root.after(0, self.close))
        self.player.play(0)
        self.root.after(0, self._frame)
        self.root.lift()
        self.root.focus_force()
        self.root.mainloop()

    def close(self) -> None:
        try:
            self.player.close()
        finally:
            self.root.destroy()
