#!/usr/bin/env python3
"""Floating Lyrics — transparent, always-on-top karaoke lyrics synced to audio.

First run:  downloads the song/video from --url, builds word-timed lyrics,
            then plays the audio with a floating lyrics overlay.
              1. Lyric videos  → reads the on-screen lyrics (macOS Vision OCR)
              2. Anything else → transcribes the vocals locally (Whisper)
Later runs: reuse the cache in ./media (edit media/lyrics*.lrc freely).

Examples:
    python floating_lyrics.py
    python floating_lyrics.py --url "https://vt.tiktok.com/XXXX/" --refresh
    python floating_lyrics.py --audio song.mp3 --lyrics song.lrc
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Tuple

from lyrics_app.lrc import Lyrics
from lyrics_app.pipeline import (PipelineError, download_audio, extract_audio,
                                 ocr_available, ocr_lyrics, title_from_meta, transcribe)

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "media"
AUDIO = MEDIA / "song.mp3"
VIDEO = MEDIA / "clip.mp4"
DEFAULT_URL = "https://vt.tiktok.com/ZSs8WoYQ9/"

log = logging.getLogger("floating_lyrics")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL, help="song/video URL (TikTok, YouTube, …)")
    p.add_argument("--audio", type=Path, help="use a local audio/video file instead of --url")
    p.add_argument("--lyrics", type=Path, nargs="+", help="use specific .lrc file(s), one per lane")
    p.add_argument("--refresh", action="store_true", help="re-download and rebuild lyrics")
    p.add_argument("--resync", action="store_true", help="rebuild lyrics from the cached media")
    p.add_argument("--source", choices=("auto", "ocr", "whisper"), default="auto",
                   help="lyrics source (auto = OCR for lyric videos, else Whisper)")
    p.add_argument("--model", default="small.en", help="Whisper model: tiny.en/base.en/small.en/medium.en")
    p.add_argument("--prompt", default=None, help="vocabulary hint for Whisper")
    p.add_argument("--font-size", type=int, default=46)
    p.add_argument("--width", type=int, default=None, help="window width in px")
    p.add_argument("--offset", type=float, default=0.0, help="lyrics offset in seconds (+ = earlier)")
    p.add_argument("--no-loop", action="store_true", help="quit when the song ends")
    p.add_argument("--no-snow", action="store_true", help="hide the drifting snowflakes")
    p.add_argument("--no-backdrop", action="store_true", help="no background box (fully transparent)")
    p.add_argument("--title", default=None, help="caption shown above the lyrics")
    p.add_argument("--prepare-only", action="store_true", help="build media + lyrics, don't open the window")
    return p.parse_args(argv)


def _cached_lrcs() -> List[Path]:
    lanes = sorted(MEDIA.glob("lyrics_*.lrc"))
    single = MEDIA / "lyrics.lrc"
    return lanes or ([single] if single.exists() else [])


def build_lyrics(args: argparse.Namespace, video_ok: bool) -> None:
    for old in MEDIA.glob("lyrics*.lrc"):
        old.unlink()
    tag = {"by": "floating_lyrics"}
    if args.source in ("auto", "ocr") and video_ok and ocr_available():
        lanes = ocr_lyrics(VIDEO, log=log.info)
        if lanes:
            for i, lyr in enumerate(lanes, 1):
                path = MEDIA / f"lyrics_{i}.lrc"
                path.write_text(lyr.dumps(tag), encoding="utf-8")
                log.info("Wrote %s — %s (%d lines)", path.name, lyr.caption or f"lane {i}", len(lyr))
            return
        if args.source == "ocr":
            raise PipelineError("No on-screen lyrics found in the video.")
        log.info("No on-screen lyrics detected — falling back to Whisper.")
    elif args.source == "ocr":
        raise PipelineError("OCR needs macOS + pyobjc-framework-Vision and a video source.")

    lyrics = transcribe(AUDIO, model_size=args.model, prompt=args.prompt, log=log.info)
    if not len(lyrics):
        raise PipelineError("No vocals recognised — supply lyrics with --lyrics file.lrc")
    (MEDIA / "lyrics.lrc").write_text(lyrics.dumps(tag), encoding="utf-8")
    log.info("Wrote lyrics.lrc (%d lines)", len(lyrics))


def prepare(args: argparse.Namespace) -> Tuple[Path, List[Lyrics], str]:
    MEDIA.mkdir(exist_ok=True)
    rebuild = args.refresh or args.resync
    video_ok = VIDEO.exists()

    if args.audio:
        if not args.audio.exists():
            raise PipelineError(f"File not found: {args.audio}")
        log.info("Converting %s → %s", args.audio, AUDIO.name)
        extract_audio(args.audio, AUDIO)
        video_ok, rebuild = False, True
        if args.audio.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
            import shutil
            shutil.copyfile(args.audio, VIDEO)
            video_ok = True
    elif args.refresh or not AUDIO.exists():
        log.info("Downloading %s", args.url)
        download_audio(args.url, MEDIA)
        video_ok, rebuild = VIDEO.exists(), True

    if args.lyrics:
        missing = [p for p in args.lyrics if not p.exists()]
        if missing:
            raise PipelineError(f"Lyrics file not found: {missing[0]}")
        paths = list(args.lyrics)
    else:
        if rebuild or not _cached_lrcs():
            build_lyrics(args, video_ok)
        paths = _cached_lrcs()

    lanes = [Lyrics.load(p) for p in paths]
    title = args.title if args.title is not None else title_from_meta(MEDIA)[:80]
    return AUDIO, lanes, title


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args = parse_args(argv)
    try:
        audio, lanes, title = prepare(args)
    except PipelineError as e:
        log.error("Error: %s", e)
        return 1
    except Exception as e:  # download / decoder / model failures
        log.error("Setup failed: %s: %s", type(e).__name__, e)
        return 1
    if args.prepare_only:
        return 0

    from lyrics_app.audio import AudioPlayer

    player = AudioPlayer(audio)
    opts = dict(title=title, width=args.width, font_size=args.font_size,
                loop=not args.no_loop, offset=args.offset)
    overlay = None
    if sys.platform == "darwin":
        try:  # native AppKit window — Apple's bundled Tk 8.5 can't draw transparent windows
            from lyrics_app.mac_overlay import MacFloatingLyrics
            overlay = MacFloatingLyrics(lanes, player, snow=not args.no_snow,
                                        backdrop=not args.no_backdrop, **opts)
        except ImportError as e:
            log.warning("Native macOS overlay unavailable (%s) — using Tk.", e)
    if overlay is None:
        from lyrics_app.overlay import FloatingLyrics
        overlay = FloatingLyrics(lanes, player, **opts)

    log.info("▶ Playing — Colors floats top-left (falling), Bags bottom-right (rising).")
    log.info("  Space pause · ←/→ seek · ↑/↓ volume · +/− size · [ ] sync · S snow · B background · "
             "C reset positions · drag to move · right-click menu · Esc quit")
    overlay.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
