"""Platform action seam.

`mcu.py`, `midi.py` and `engine.py` never import OS APIs; every system action goes
through this ABC. macOS is `macos.py`; Windows/Linux only need a new module registered
in `get_backend`.
"""

from __future__ import annotations

import abc
import logging
import sys

log = logging.getLogger("xtouch.backend")


class ActionBackend(abc.ABC):
    @abc.abstractmethod
    def preflight(self) -> bool:
        """True when synthetic events may be posted (Accessibility granted)."""

    @abc.abstractmethod
    def tick(self) -> None:
        """Flush coalesced volume writes. Called once per engine tick."""

    @abc.abstractmethod
    def set_volume(self, percent: int) -> None: ...

    @abc.abstractmethod
    def get_volume(self) -> int | None: ...

    @abc.abstractmethod
    def set_muted(self, muted: bool) -> None: ...

    @abc.abstractmethod
    def get_muted(self) -> bool | None: ...

    @abc.abstractmethod
    def set_input_muted(self, muted: bool) -> None: ...

    @abc.abstractmethod
    def get_input_muted(self) -> bool | None: ...

    @abc.abstractmethod
    def media(self, key: str) -> None:
        """key in {"play", "next", "prev"}."""

    @abc.abstractmethod
    def scroll(self, detents: int, horizontal: bool) -> None:
        """Signed detents, +1 = clockwise / jog right."""

    @abc.abstractmethod
    def tab_next(self) -> None: ...

    @abc.abstractmethod
    def tab_prev(self) -> None: ...

    @abc.abstractmethod
    def zoom_toggle(self) -> None: ...

    @abc.abstractmethod
    def poll_audio_state(self) -> tuple[int, bool, bool] | None:
        """(volume, muted, input_muted) when it may have changed, else None."""

    @abc.abstractmethod
    def close(self) -> None: ...


class DummyBackend(ActionBackend):
    """Records every action; drives tests and `--backend dummy`."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.audio_state: tuple[int, bool, bool] | None = None

    def _record(self, name: str, *args) -> None:
        self.calls.append((name,) + args)
        log.info("[dummy] %s%s", name, args or "")

    def preflight(self) -> bool:
        return True

    def tick(self) -> None:
        pass

    def set_volume(self, percent: int) -> None:
        self._record("set_volume", percent)

    def get_volume(self) -> int | None:
        return self.audio_state[0] if self.audio_state else None

    def set_muted(self, muted: bool) -> None:
        self._record("set_muted", muted)

    def get_muted(self) -> bool | None:
        return self.audio_state[1] if self.audio_state else None

    def set_input_muted(self, muted: bool) -> None:
        self._record("set_input_muted", muted)

    def get_input_muted(self) -> bool | None:
        return self.audio_state[2] if self.audio_state else None

    def media(self, key: str) -> None:
        self._record("media", key)

    def scroll(self, detents: int, horizontal: bool) -> None:
        self._record("scroll", detents, horizontal)

    def tab_next(self) -> None:
        self._record("tab_next")

    def tab_prev(self) -> None:
        self._record("tab_prev")

    def zoom_toggle(self) -> None:
        self._record("zoom_toggle")

    def poll_audio_state(self) -> tuple[int, bool, bool] | None:
        return self.audio_state

    def close(self) -> None:
        self._record("close")


def get_backend(audio_mode: str = "auto", backend: str = "auto",
                invert_scroll: bool = False) -> ActionBackend:
    if backend == "dummy":
        return DummyBackend()
    if sys.platform == "darwin":
        from .macos import MacOSBackend

        return MacOSBackend(audio_mode, invert_scroll=invert_scroll)
    raise RuntimeError("no backend for platform " + sys.platform)
