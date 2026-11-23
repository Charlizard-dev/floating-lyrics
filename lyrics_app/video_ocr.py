"""Extract time-synced lyrics from a lyric video using macOS Vision OCR.

Lyric videos (Apple-Music style) draw the line being sung in bright white,
filling word-by-word, while other lines are translucent. We sample frames,
keep only near-white pixels, OCR them, and turn the growing "lit prefix"
into word-level timestamps. Split-screen mashups are handled as lanes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .lrc import Line, Lyrics, Word

_IGNORE = re.compile(r"^(wr\w*t\w*\s*b[vyu]|music\s*vibes|produced\s*b|composed\s*b|lyrics\s*b)")


@dataclass
class Lane:
    name: str
    x0: float  # fractions of frame width/height
    x1: float
    y0: float = 0.22
    y1: float = 0.78


def _probe(video: Path) -> Tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(video)],
        check=True, capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def _frames(video: Path, fps: float, w: int, h: int):
    proc = subprocess.Popen(
        [shutil.which("ffmpeg") or "ffmpeg", "-loglevel", "error", "-i", str(video),
         "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait()


def _ocr(gray: np.ndarray, rel_height: float = 0.65, sep: str = " ") -> str:
    """OCR a single-channel uint8 image with Vision.

    Only text whose glyph height is close to the largest on screen is kept,
    which drops small captions such as songwriter credits.
    """
    import Quartz
    import Vision

    h, w = gray.shape
    data = np.ascontiguousarray(gray).tobytes()
    provider = Quartz.CGDataProviderCreateWithData(None, data, len(data), None)
    img = Quartz.CGImageCreate(w, h, 8, 8, w, Quartz.CGColorSpaceCreateDeviceGray(),
                               Quartz.kCGImageAlphaNone, provider, None, False,
                               Quartz.kCGRenderingIntentDefault)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)  # partially-lit words must stay partial
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    handler.performRequests_error_([req], None)
    found = []
    for o in req.results() or []:
        s = o.topCandidates_(1)[0].string().strip()
        if s:
            box = o.boundingBox()
            found.append((1 - box.origin.y - box.size.height, box.size.height, s))
    found.sort()  # top → bottom
    # A credits caption ("Written by: …") ends the lyric block: drop it and below.
    for i, (_, _, s) in enumerate(found):
        if _IGNORE.match(re.sub(r"[^a-z ]", "", s.lower())):
            found = found[:i]
            break
    if not found:
        return ""
    tallest = max(f[1] for f in found)
    return sep.join(s for _, hgt, s in found if hgt >= rel_height * tallest)


def _lit_mask(rgb: np.ndarray, thresh: int) -> np.ndarray:
    """Near-white, unsaturated pixels → black text on white background."""
    lo, hi = rgb.min(axis=2), rgb.max(axis=2)
    lit = (lo >= thresh) & (hi.astype(np.int16) - lo < 40)
    out = np.full(lo.shape, 255, np.uint8)
    out[lit] = 0
    # pad so Vision has margins
    return np.pad(out, 24, constant_values=255)


_CONFUSABLE = {"1": "i", "l": "i", "|": "i", "!": "i"}


def _norm(s: str) -> List[str]:
    toks = re.sub(r"[^\w'|! ]", " ", s.lower()).split()
    return [_CONFUSABLE.get(t, t) for t in toks if t]


def _clean(text: str) -> str:
    """Fix common OCR confusions in the final display text."""
    return re.sub(r"(?<!\S)[1|l](?=\s|'|$)", "I", text).strip()


class _Speller:
    """macOS NSSpellChecker wrapper (no-op where unavailable)."""

    def __init__(self):
        try:
            from AppKit import NSSpellChecker
            self._sc = NSSpellChecker.sharedSpellChecker()
        except Exception:  # pragma: no cover - non-mac / missing pyobjc
            self._sc = None

    def ok(self, word: str) -> bool:
        w = re.sub(r"[^\w']", "", word)
        if not self._sc or not w or w.isdigit():
            return True
        return self._sc.checkSpellingOfString_startingAt_(w, 0).length == 0

    def misspelled(self, text: str) -> int:
        return sum(not self.ok(w) for w in text.split())

    def fix(self, word: str) -> str:
        core = re.sub(r"[^\w']", "", word)
        if not core or self.ok(core):
            return word
        corr = self._sc.correctionForWordRange_inString_language_inSpellDocumentWithTag_(
            (0, len(core)), core, "en", 0)
        return word.replace(core, corr) if corr else word


SPELL = _Speller()


def _contain(a: List[str], b: List[str]) -> float:
    """How much of the shorter token string is (fuzzily) inside the longer."""
    x, y = " ".join(a), " ".join(b)
    if len(x) > len(y):
        x, y = y, x
    if not x:
        return 0.0
    blocks = SequenceMatcher(None, x, y, autojunk=False).get_matching_blocks()
    return sum(bl.size for bl in blocks) / len(x)


def _similar(a: List[str], b: List[str]) -> float:
    """Similarity of the common prefix (last token may be partially lit)."""
    n = max(1, min(len(a), len(b)) - 1)
    return SequenceMatcher(None, " ".join(a[:n]), " ".join(b[:n])).ratio()


@dataclass
class _Seg:
    samples: List[Tuple[float, List[str], str]]

    @property
    def start(self) -> float:
        return self.samples[0][0]

    @property
    def end(self) -> float:
        return self.samples[-1][0]

    def ref(self) -> List[str]:
        return max(self.samples, key=lambda s: len(s[1]))[1]

    def finalize(self) -> Optional[Line]:
        # Longest reading seen in >= 2 frames = the fully lit line (single-frame
        # outliers are OCR noise); then majority-vote the exact spelling.
        counts = Counter(len(s[1]) for s in self.samples)
        stable = [n for n, c in counts.items() if c >= 2] or list(counts)
        n = max(stable)
        base = Counter(s[2] for s in self.samples if len(s[1]) == n).most_common(1)[0][0]
        base_toks = _norm(base)
        # Allow longer one-off readings that extend the stable text (the last
        # word of a wrapped line is often only fully lit for a frame or two);
        # prefer the latest such reading since it is the most completely lit.
        ext = [s for s in self.samples
               if len(s[1]) >= n and _similar(s[1][:n], base_toks) >= 0.8]
        if ext:
            m = max(len(s[1]) for s in ext)
            longest = [s[2] for s in ext if len(s[1]) == m]
        else:
            longest = [base]
        votes = Counter(longest)
        order = {t: i for i, t in enumerate(longest)}  # last index = latest
        # Rank: fewest misspellings, then most frequent, then most recent.
        text = min(votes, key=lambda t: (SPELL.misspelled(t), -votes[t], -order[t]))
        words = [SPELL.fix(w) for w in text.split()]
        stamps = []
        for k in range(len(words)):
            hit = next((t for t, toks, _ in self.samples if len(toks) > k), None)
            stamps.append(hit if hit is not None else (stamps[-1] if stamps else self.start))
        for i in range(1, len(stamps)):  # enforce monotonic word times
            stamps[i] = max(stamps[i], stamps[i - 1])
        # Sung words light up one by one; a block of >=3 words appearing in the
        # same frame mid-line is an on-screen caption (credits etc.) → cut it.
        for i in range(1, len(stamps) - 2):
            if stamps[i] > stamps[0] and stamps[i] == stamps[i + 1] == stamps[i + 2] \
                    and stamps[i] - stamps[i - 1] > 0.5:
                words, stamps = words[:i], stamps[:i]
                break
        return Line(self.start, " ".join(words), [Word(round(t, 2), (" " if i else "") + w)
                                        for i, (t, w) in enumerate(zip(stamps, words))])


def _lane_lyrics(samples: Sequence[Tuple[float, str]], step: float,
                 min_dur: float = 0.5, gap: float = 0.5) -> List[Line]:
    """Segment (time, lit-text) samples into lines with word timings."""
    segs: List[_Seg] = []
    cur: Optional[_Seg] = None
    last_seen = -1e9
    for t, raw in samples:
        toks = _norm(raw)
        if not toks:
            if cur and t - last_seen >= gap:
                segs.append(cur)
                cur = None
            continue
        if cur is not None:
            prev = cur.samples[-1][1]
            restart = len(toks) <= 2 and len(prev) >= len(toks) + 2  # same text sung again
            if restart or _contain(toks, cur.ref()) < 0.6:
                segs.append(cur)
                cur = None
        if cur is None:
            cur = _Seg([])
        cur.samples.append((t, toks, raw))
        last_seen = t
    if cur:
        segs.append(cur)

    lines: List[Line] = []
    prev_seg: Optional[_Seg] = None
    for seg in segs:
        short = seg.end - seg.start + step < min_dur
        if short and lines and prev_seg and seg.start - prev_seg.end <= 2.5 * step:
            # Last word(s) of a wrapped line lit on their own row → append.
            tail = seg.finalize()
            new = [w for w in tail.text.split() if _norm(w) and _norm(w)[0] not in _norm(lines[-1].text)]
            if tail and 0 < len(new) <= 3 and len(new) == len(tail.text.split()):
                last = lines[-1]
                last.words += [Word(round(seg.start, 2), " " + w) for w in new]
                last.text = f"{last.text} {' '.join(new)}"
                prev_seg = seg
                continue
        if short:
            continue  # transition noise (fade in/out, scrolling)
        line = seg.finalize()
        if line and len(line.text) > 1:
            lines.append(line)
            prev_seg = seg
    for line in lines:
        line.text = _clean(line.text)
        for w in line.words:
            w.text = (" " if w.text.startswith(" ") else "") + _clean(w.text)
    return lines


# A word only reads as "lit" once the karaoke sweep has filled it to near-white,
# which happens ~0.5 s after the singer starts it (measured against Whisper word
# onsets on the bundled clip). Shift every timestamp earlier by this much.
LIGHT_LAG = 0.5


def _shift(lines: List[Line], lag: float) -> List[Line]:
    for line in lines:
        line.start = round(max(0.0, line.start - lag), 2)
        for w in line.words:
            w.start = round(max(0.0, w.start - lag), 2)
    return lines


def extract(video: Path, lanes: Sequence[Lane], fps: float = 10.0,
            thresh: int = 238, min_dur: float = 0.5, lag: float = LIGHT_LAG,
            log=print) -> List[Lyrics]:
    w, h = _probe(video)
    samples: List[List[Tuple[float, str]]] = [[] for _ in lanes]
    for t, frame in _frames(video, fps, w, h):
        for i, ln in enumerate(lanes):
            crop = frame[int(ln.y0 * h):int(ln.y1 * h), int(ln.x0 * w):int(ln.x1 * w)]
            text = _ocr(_lit_mask(crop, thresh))
            samples[i].append((t, text))
        if int(t * fps) % int(fps) == 0:
            log(f"  OCR {t:5.1f}s  " + " | ".join(s[-1][1][:40] for s in samples))
    return [Lyrics(_shift(_lane_lyrics(s, 1.0 / fps, min_dur), lag)) for s in samples]


def lane_titles(video: Path, lanes: Sequence[Lane], at: float = 1.0) -> List[List[str]]:
    """OCR each lane's header strip (song / artist, e.g. 'Colors' 'Halsey')."""
    w, h = _probe(video)
    for t, frame in _frames(video, 1.0, w, h):
        if t < at:
            continue
        out = []
        for ln in lanes:
            crop = frame[int(0.03 * h):int(ln.y0 * h), int(ln.x0 * w):int(ln.x1 * w)]
            gray = 255 - crop.max(axis=2)  # light text → dark text on light bg
            text = _ocr(np.pad(gray, 24, constant_values=255), rel_height=0.0, sep="\n")
            out.append([p.strip() for p in text.split("\n") if p.strip()])
        return out
    return [[] for _ in lanes]


def detect_lanes(video: Path) -> List[Lane]:
    """Wide (≈2:1) videos are treated as left/right split-screen mashups."""
    w, h = _probe(video)
    if w >= 1.6 * h:
        return [Lane("left", 0.0, 0.5), Lane("right", 0.5, 1.0)]
    return [Lane("main", 0.0, 1.0)]
