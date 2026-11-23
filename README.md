# Floating Lyrics 🎤

A transparent, always-on-top **karaoke lyrics overlay** that plays the song and
lights up each word as it's sung. It's preloaded with
**Halsey × Clairo – "Colors & Bags" (Mashup)** from
https://vt.tiktok.com/ZSs8WoYQ9/, with each song in its own lane, side by side.

## Run it

```bash
~/Desktop/lyrics/run.command          # or just double-click run.command in Finder
```

On first launch it builds `.venv/`. Media and lyrics are already cached in
`media/`, so it starts playing right away.

## Controls

| Key / action        | Effect                              |
|---------------------|-------------------------------------|
| Drag                | Move the overlay anywhere           |
| Space               | Play / pause                        |
| ← / →               | Seek −5s / +5s                      |
| ↑ / ↓               | Volume                              |
| + / −               | Bigger / smaller text               |
| [ / ]               | Nudge lyric sync by 0.1s            |
| R                   | Restart                             |
| L                   | Toggle loop (on by default)         |
| S                   | Toggle snow crystals / embers       |
| B                   | Toggle the background boxes         |
| C                   | Reset windows to their corners      |
| Right-click         | Menu with all of the above          |
| Esc / Q             | Quit                                |

Each song is its own frosted-glass card with a themed gradient:

- **Colors (Halsey)** is a midnight-blue card at the **top-left**. Lyrics **fall** with
  slowly spinning snow crystals.
- **Bags (Clairo)** is a sunset-amber card at the **bottom-right**. Lyrics **rise**
  with glowing embers.

Design details:
- A header with the title, the artist, and a
  **live equalizer** driven by the song's actual frequency content.
- A **karaoke sweep** that fills each word letter by letter as it's sung, with a
  soft glow on sung words.
- The current line, the card border and the corner bloom **pulse with the beat**,
  and particles drift faster on loud parts.
- A gradient progress bar with a glowing playhead and time labels, all set in
  the rounded system font.

Both cards float above all apps, on every desktop and over full-screen windows.
Drag either one to move it, and it remembers where you put it.

## Use another song

```bash
./run.command --url "https://vt.tiktok.com/XXXX/" --refresh    # any TikTok / YouTube link
./run.command --audio song.mp3 --lyrics song.lrc               # your own files
./run.command --resync                                         # rebuild lyrics from cached media
./run.command --no-loop --font-size 48 --offset 0.2
```

How the lyrics are built (`--source auto`):

1. **Lyric videos** (Apple Music-style, like this TikTok). Frames are sampled
   10× per second. Only the bright "lit" text is kept and read with macOS Vision
   OCR, so each word gets the time it lights up. Split-screen mashups become two
   lanes, and song/artist names come from the header.
   Spell-check voting fixes OCR typos, and credits captions are filtered out.
2. **Everything else** is transcribed locally with **Whisper**
   (`faster-whisper`, word timestamps). Use `--model medium.en` for better
   accuracy.

The results are plain **enhanced LRC** files (`media/lyrics_*.lrc`). Edit them in
any text editor to fix a word or a timestamp: `[mm:ss.xx]<mm:ss.xx>word <mm:ss.xx>word`.

## Project layout

```
floating_lyrics.py     CLI entry point / orchestration
lyrics_app/
  lrc.py               enhanced-LRC parser/writer, O(log n) timeline lookup
  audio.py             pygame playback with an accurate seekable clock
  mac_overlay.py       native macOS overlay: transparent window per song, falling / rising lyrics
  overlay.py           Tk fallback renderer for Windows/Linux
  video_ocr.py         lyric-video → word-timed LRC (macOS Vision)
  spectrum.py          audio analysis (FFT bands + loudness) for reactive visuals
  pipeline.py          download (yt-dlp/ffmpeg), OCR, Whisper fallback
media/                 cached clip, audio, metadata and .lrc lyrics
```

Requirements: macOS, Python 3.9+ with Tk (`/usr/bin/python3`), `ffmpeg`, and
`yt-dlp` (`run.command` installs any that are missing via Homebrew).
The audio and lyrics are for personal use.
