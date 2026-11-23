"""Audio playback with an accurate, seekable position clock (pygame.mixer)."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402


class AudioPlayer:
    """Thin wrapper around pygame.mixer.music.

    pygame's get_pos() reports ms since the last play() call and ignores the
    `start` argument, so we keep our own base offset to get absolute time.
    """

    def __init__(self, path: Path, volume: float = 0.8):
        pygame.mixer.pre_init(44100, -16, 2, 1024)
        pygame.mixer.init()
        self.path = str(path)
        pygame.mixer.music.load(self.path)
        pygame.mixer.music.set_volume(volume)
        self.length = pygame.mixer.Sound(self.path).get_length()
        self._base = 0.0
        self._paused = False
        self._started = False

    # --------------------------------------------------------------- state
    @property
    def playing(self) -> bool:
        return self._started and not self._paused

    @property
    def finished(self) -> bool:
        return self._started and not self._paused and not pygame.mixer.music.get_busy()

    def position(self) -> float:
        if not self._started:
            return self._base
        ms = pygame.mixer.music.get_pos()
        return self._base + max(ms, 0) / 1000.0

    # ------------------------------------------------------------ controls
    def play(self, start: float = 0.0) -> None:
        start = min(max(0.0, start), max(0.0, self.length - 0.05))
        self._base = start
        pygame.mixer.music.play(start=start)
        self._started, self._paused = True, False

    def toggle_pause(self) -> None:
        if not self._started:
            self.play(self._base)
        elif self._paused:
            pygame.mixer.music.unpause()
            self._paused = False
        else:
            pygame.mixer.music.pause()
            self._paused = True

    def seek(self, delta: float) -> None:
        was_paused = self._paused
        self.play(self.position() + delta)
        if was_paused:
            pygame.mixer.music.pause()
            self._paused = True

    def volume(self, delta: float) -> float:
        v = min(1.0, max(0.0, pygame.mixer.music.get_volume() + delta))
        pygame.mixer.music.set_volume(v)
        return v

    def close(self) -> None:
        pygame.mixer.music.stop()
        pygame.mixer.quit()
