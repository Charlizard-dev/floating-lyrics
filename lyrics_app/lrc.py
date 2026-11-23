"""Parsing / writing of (enhanced) LRC lyric files.

Supported syntax:
    [mm:ss.xx]plain line
    [mm:ss.xx]<mm:ss.xx>word <mm:ss.xx>word ...     (enhanced / word-level)
    [offset:+250]                                    (global offset, ms)
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_WORD = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>([^<]*)")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\]", re.I)
_META = re.compile(r"\[([a-z]+):(.*)\]$", re.I)


def _secs(m: str, s: str) -> float:
    return int(m) * 60 + float(s)


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


@dataclass
class Word:
    start: float
    text: str


@dataclass
class Line:
    start: float
    text: str
    words: List[Word] = field(default_factory=list)
    end: float = float("inf")


class Lyrics:
    """Immutable, time-sorted lyric timeline with O(log n) lookup."""

    def __init__(self, lines: List[Line], meta: Optional[dict] = None):
        self.meta = dict(meta or {})
        self.lines = sorted(lines, key=lambda l: l.start)
        for cur, nxt in zip(self.lines, self.lines[1:]):
            cur.end = nxt.start
        self._starts = [l.start for l in self.lines]

    def __len__(self) -> int:
        return len(self.lines)

    def index_at(self, t: float) -> int:
        """Index of the active line at time t, or -1 before the first line."""
        return bisect.bisect_right(self._starts, t) - 1

    def line(self, i: int) -> Optional[Line]:
        return self.lines[i] if 0 <= i < len(self.lines) else None

    # ---------------------------------------------------------------- I/O
    @classmethod
    def parse(cls, text: str) -> "Lyrics":
        offset = 0.0
        meta: dict = {}
        lines: List[Line] = []
        for raw in text.splitlines():
            raw = raw.strip()
            if m := _OFFSET.match(raw):
                offset = int(m.group(1)) / 1000.0
                continue
            stamps = []
            pos = 0
            while (m := _TAG.match(raw, pos)):
                stamps.append(_secs(m.group(1), m.group(2)))
                pos = m.end()
            if not stamps:
                if m := _META.match(raw):
                    meta[m.group(1).lower()] = m.group(2).strip()
                continue
            body = raw[pos:]
            words = [Word(_secs(a, b), w) for a, b, w in _WORD.findall(body)]
            plain = re.sub(r"<[^>]*>", "", body).strip()
            for st in stamps:  # one line may carry several timestamps
                shift = st - stamps[0]
                lines.append(Line(
                    st - offset, plain,
                    [Word(w.start + shift - offset, w.text) for w in words],
                ))
        return cls(lines, meta)

    @property
    def caption(self) -> str:
        return " · ".join(v for v in (self.meta.get("ti"), self.meta.get("ar")) if v)

    @classmethod
    def load(cls, path: Path) -> "Lyrics":
        return cls.parse(Path(path).read_text(encoding="utf-8"))

    def dumps(self, header: Optional[dict] = None) -> str:
        out = [f"[{k}:{v}]" for k, v in {**self.meta, **(header or {})}.items()]
        for l in self.lines:
            if l.words:
                body = "".join(f"<{fmt_ts(w.start)}>{w.text}" for w in l.words)
            else:
                body = l.text
            out.append(f"[{fmt_ts(l.start)}]{body}")
        return "\n".join(out) + "\n"
