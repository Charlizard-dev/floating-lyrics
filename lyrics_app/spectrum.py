"""Pre-computed audio analysis for audio-reactive visuals.

Decodes the song once with ffmpeg, then computes, at a fixed frame rate:
  * `bands[f, b]` — log-spaced frequency band energies, normalised 0..1
  * `level[f]`    — overall loudness, normalised 0..1
so the renderer can look values up by playback time at zero runtime cost.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np

SR = 22050
FPS = 30
N_FFT = 2048


class Spectrum:
    def __init__(self, bands: np.ndarray, level: np.ndarray, fps: int = FPS):
        self.bands, self.level, self.fps = bands, level, fps
        self.n_bands = bands.shape[1]

    @classmethod
    def from_file(cls, path: Path, n_bands: int = 7) -> Optional["Spectrum"]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        try:
            raw = subprocess.run(
                [ffmpeg, "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                 "-f", "s16le", "-"], check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        if x.size < N_FFT:
            return None

        hop = SR // FPS
        n_frames = 1 + (x.size - N_FFT) // hop
        idx = np.arange(N_FFT)[None, :] + hop * np.arange(n_frames)[:, None]
        frames = x[idx] * np.hanning(N_FFT)[None, :].astype(np.float32)
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        freqs = np.fft.rfftfreq(N_FFT, 1 / SR)

        edges = np.geomspace(60, 9000, n_bands + 1)
        bands = np.stack([power[:, (freqs >= lo) & (freqs < hi)].mean(axis=1)
                          for lo, hi in zip(edges[:-1], edges[1:])], axis=1)
        bands = np.log10(bands + 1e-10)
        lo, hi = np.percentile(bands, 15, axis=0), np.percentile(bands, 99, axis=0)
        bands = np.clip((bands - lo) / np.maximum(hi - lo, 1e-6), 0, 1)

        rms = np.sqrt((frames ** 2).mean(axis=1))
        level = np.log10(rms + 1e-6)
        lo, hi = np.percentile(level, 10), np.percentile(level, 99)
        level = np.clip((level - lo) / max(hi - lo, 1e-6), 0, 1)
        return cls(bands.astype(np.float32), level.astype(np.float32))

    def _frame(self, t: float) -> int:
        return int(min(max(t * self.fps, 0), len(self.level) - 1))

    def bands_at(self, t: float) -> np.ndarray:
        return self.bands[self._frame(t)]

    def level_at(self, t: float) -> float:
        return float(self.level[self._frame(t)])
