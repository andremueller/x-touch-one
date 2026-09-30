"""mido/rtmidi port discovery, open/close, callback -> queue, send, rescan.

The MIDI layer is OS-independent: mido drives RtMidi, which picks CoreMIDI on macOS,
ALSA/JACK on Linux and WinMM on Windows.
"""

from __future__ import annotations

import logging
import queue
from typing import Iterable

import mido

log = logging.getLogger("xtouch.midi")


class DeviceNotFound(RuntimeError):
    pass


def _match(names: list[str], sub: str) -> str | None:
    for name in names:
        if sub.lower() in name.lower():
            return name
    return None


class MidiIO:
    def __init__(self, port_substring: str = "x-touch", queue_size: int = 1024):
        self.port_substring = port_substring
        self._q: queue.Queue[mido.Message] = queue.Queue(maxsize=queue_size)
        self._in: mido.ports.BaseInput | None = None
        self._out: mido.ports.BaseOutput | None = None
        self._in_name: str | None = None
        self._out_name: str | None = None

    def sources(self) -> list[str]:
        return mido.get_input_names()

    def destinations(self) -> list[str]:
        return mido.get_output_names()

    def open(self) -> None:
        sub = self.port_substring
        in_name = _match(self.sources(), sub)
        out_name = _match(self.destinations(), sub)
        if in_name is None or out_name is None:
            raise DeviceNotFound(
                f"no MIDI port matching {sub!r}; "
                f"inputs={self.sources()} outputs={self.destinations()}"
            )
        self._in = mido.open_input(in_name, callback=self._on_message)
        self._out = mido.open_output(out_name)
        self._in_name, self._out_name = in_name, out_name
        log.info("[bridge] opened %r -> %r", in_name, out_name)

    def _on_message(self, msg: mido.Message) -> None:
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            log.debug("input queue full, dropped %r", msg)

    def drain(self) -> list[mido.Message]:
        out: list[mido.Message] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def send(self, messages: Iterable[mido.Message]) -> None:
        if self._out is None:
            return
        for m in messages:
            self._out.send(m)

    def rescan(self) -> bool:
        return self._in_name in mido.get_input_names() and self._out_name in mido.get_output_names()

    def close(self) -> None:
        for port in (self._in, self._out):
            if port is not None:
                try:
                    port.close()
                except Exception:  # pragma: no cover - device already gone
                    log.debug("close failed", exc_info=True)
        self._in = self._out = None
