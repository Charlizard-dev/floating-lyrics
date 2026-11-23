"""Media pipeline: URL -> audio file -> time-synced (word-level) LRC."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .lrc import Line, Lyrics, Word


class PipelineError(RuntimeError):
    pass


def _need(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise PipelineError(f"'{tool}' not found on PATH. Install it with: brew install {tool}")
    return exe


def download_audio(url: str, out_dir: Path) -> Path:
    """Download a TikTok/YouTube/etc. URL with yt-dlp and convert to MP3."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ytdlp, ffmpeg = _need("yt-dlp"), _need("ffmpeg")
    video = out_dir / "clip.mp4"
    subprocess.run(
        [ytdlp, "-q", "--no-warnings", "--no-playlist", "-f", "best",
         "-o", str(video), "--force-overwrites", url],
        check=True,
    )
    meta = subprocess.run(
        [ytdlp, "-j", "--no-warnings", "--no-playlist", url],
        check=True, capture_output=True, text=True,
    ).stdout
    (out_dir / "meta.json").write_text(meta, encoding="utf-8")
    return extract_audio(video, out_dir / "song.mp3", ffmpeg)


def extract_audio(src: Path, dst: Path, ffmpeg: Optional[str] = None) -> Path:
    ffmpeg = ffmpeg or _need("ffmpeg")
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-y", "-i", str(src), "-vn",
         "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-q:a", "2", str(dst)],
        check=True,
    )
    return dst


def ocr_lyrics(video: Path, log=print) -> List[Lyrics]:
    """Read synced lyrics off a lyric video (macOS Vision). [] if none found."""
    from . import video_ocr

    lanes = video_ocr.detect_lanes(video)
    log(f"Reading on-screen lyrics ({len(lanes)} lane(s)) with macOS Vision OCR…")
    titles = video_ocr.lane_titles(video, lanes)
    result = []
    for lyr, title in zip(video_ocr.extract(video, lanes, log=log), titles):
        if len(lyr):
            if title:
                lyr.meta["ti"] = title[0]
            if len(title) > 1:
                lyr.meta["ar"] = title[1]
            result.append(lyr)
    return result


def ocr_available() -> bool:
    try:
        import Vision  # noqa: F401
        import Quartz  # noqa: F401
        return True
    except ImportError:
        return False


def title_from_meta(out_dir: Path) -> str:
    try:
        d = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
        return d.get("title") or ""
    except (OSError, ValueError):
        return ""


def _group_words(words: Iterable[Tuple[Word, float]], max_words: int, max_gap: float) -> List[Line]:
    """Break a flat word stream into display lines on pauses / length."""
    lines: List[Line] = []
    cur: List[Word] = []
    last_end = None
    for w, end in words:
        gap = (w.start - last_end) if last_end is not None else 0.0
        ends_phrase = cur and cur[-1].text.rstrip().endswith((".", "?", "!", ","))
        if cur and (len(cur) >= max_words or gap > max_gap or ends_phrase):
            lines.append(Line(cur[0].start, "".join(x.text for x in cur).strip(), cur))
            cur = []
        cur.append(w)
        last_end = end
    if cur:
        lines.append(Line(cur[0].start, "".join(x.text for x in cur).strip(), cur))
    return lines


def transcribe(audio: Path, model_size: str = "small.en", language: str = "en",
               prompt: Optional[str] = None, max_words: int = 7,
               max_gap: float = 0.7, log=print) -> Lyrics:
    """Run faster-whisper locally and return word-timed Lyrics."""
    from faster_whisper import WhisperModel  # heavy import, keep lazy

    log(f"Loading Whisper model '{model_size}' (first run downloads it)…")
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    log("Transcribing…")
    segments, _ = model.transcribe(
        str(audio), language=language, word_timestamps=True,
        vad_filter=False, beam_size=5, initial_prompt=prompt,
        condition_on_previous_text=False,
    )
    stream = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word if w.word.startswith(" ") else " " + w.word
            stream.append((Word(round(w.start, 2), text), w.end))
            log(f"  {w.start:6.2f}s {w.word.strip()}")
    for line in (lines := _group_words(stream, max_words, max_gap)):
        line.words[0].text = line.words[0].text.lstrip()
    return Lyrics(lines)
