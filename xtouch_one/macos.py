"""macOS backend: synthetic input events (Quartz/AppKit) + system audio control.

Audio control binds the CoreAudio framework through ``ctypes`` rather than
``pyobjc-framework-CoreAudio``: pyobjc's bridge cannot express
``AudioObjectGetPropertyData``. Its out-data argument is an out-array whose length comes
from an ``inout`` size argument, so pyobjc passes a NULL buffer that CoreAudio rejects
with ``kAudioHardwareIllegalOperationError`` ('nope') for every shape that pyobjc
accepts (verified against pyobjc 12.2.2). Everything else on macOS stays pyobjc.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
import time
from typing import Callable

import AppKit
import Quartz

from .backend import ActionBackend

log = logging.getLogger("xtouch.macos")

kCGHIDEventTap = 0
NSSystemDefined = 14
SYSTEM_DEFINED_SUBTYPE = 8
NX_KEY = {"play": 16, "next": 17, "prev": 18}  # IOKit/hidsystem/ev_keymap.h
KEY_STATE_DOWN, KEY_STATE_UP = 0x0A, 0x0B
KEY_FLAGS_DOWN, KEY_FLAGS_UP = 0xA00, 0xB00
KEY_CODE = {"tab": 0x30, "equal": 0x18, "8": 0x1C}  # HIToolbox/Events.h
MOD = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000}  # IOLLEvent.h
SCROLL_STEP_PX = 10
MAX_DETENTS_PER_CALL = 20
OSASCRIPT_MAX_HZ = 20

_ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")


class _PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _code(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL, _SCOPE_OUTPUT, _SCOPE_INPUT = _code("glob"), _code("outp"), _code("inpt")
_SEL_VOLUME_SCALAR, _SEL_MUTE = _code("volm"), _code("mute")
_SEL_DEFAULT_OUTPUT, _SEL_DEFAULT_INPUT = _code("dOut"), _code("dIn ")

_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_ca.AudioObjectSetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p,
]
_ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
_PropertyListener = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.POINTER(_PropertyAddress), ctypes.c_void_p)
_ca.AudioObjectAddPropertyListener.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress),
                                               _PropertyListener, ctypes.c_void_p]
_ca.AudioObjectAddPropertyListener.restype = ctypes.c_int32
_ca.AudioObjectRemovePropertyListener.argtypes = _ca.AudioObjectAddPropertyListener.argtypes
_ca.AudioObjectRemovePropertyListener.restype = ctypes.c_int32


class CoreAudioControl:
    """Default-device volume/mute through CoreAudio property APIs."""

    def __init__(self) -> None:
        self._device: int | None = None
        self._notify: Callable[[], None] | None = None
        self._listeners: list[tuple[int, _PropertyAddress, object]] = []
        self._input_volume_backup: float | None = None

    # -- property plumbing -------------------------------------------------
    @staticmethod
    def _addr(selector: int, scope: int) -> _PropertyAddress:
        return _PropertyAddress(selector, scope, 0)

    def _get(self, obj: int, selector: int, scope: int, ctype) -> object | None:
        buf = ctype()
        size = ctypes.c_uint32(ctypes.sizeof(ctype))
        status = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(self._addr(selector, scope)), 0,
                                                None, ctypes.byref(size), ctypes.byref(buf))
        if status != 0:
            log.debug("GetPropertyData %#x/%#x on %s -> %s", selector, scope, obj, status)
            return None
        return buf.value

    def _set(self, obj: int, selector: int, scope: int, ctype, value) -> bool:
        buf = ctype(value)
        status = _ca.AudioObjectSetPropertyData(obj, ctypes.byref(self._addr(selector, scope)), 0,
                                                None, ctypes.sizeof(ctype), ctypes.byref(buf))
        if status != 0:
            log.warning("SetPropertyData %#x/%#x on %s -> %s", selector, scope, obj, status)
        return status == 0

    def _default_device(self, selector: int) -> int | None:
        dev = self._get(_SYSTEM_OBJECT, selector, _SCOPE_GLOBAL, ctypes.c_uint32)
        return int(dev) if dev else None

    @property
    def device(self) -> int | None:
        return self._default_device(_SEL_DEFAULT_OUTPUT)

    @property
    def input_device(self) -> int | None:
        return self._default_device(_SEL_DEFAULT_INPUT)

    # -- control surface ---------------------------------------------------
    def probe(self) -> bool:
        try:
            dev = self.device
            if dev is None:
                return False
            volume = self._get(dev, _SEL_VOLUME_SCALAR, _SCOPE_OUTPUT, ctypes.c_float)
            return volume is not None and 0.0 <= float(volume) <= 1.0
        except Exception:
            log.debug("coreaudio probe raised", exc_info=True)
            return False

    def get_volume(self) -> int | None:
        dev = self.device
        if dev is None:
            return None
        volume = self._get(dev, _SEL_VOLUME_SCALAR, _SCOPE_OUTPUT, ctypes.c_float)
        return None if volume is None else round(float(volume) * 100)

    def set_volume(self, percent: int) -> None:
        dev = self.device
        if dev is not None:
            self._set(dev, _SEL_VOLUME_SCALAR, _SCOPE_OUTPUT, ctypes.c_float, percent / 100.0)

    def get_muted(self) -> bool | None:
        dev = self.device
        if dev is None:
            return None
        muted = self._get(dev, _SEL_MUTE, _SCOPE_OUTPUT, ctypes.c_uint32)
        return None if muted is None else bool(muted)

    def set_muted(self, muted: bool) -> None:
        dev = self.device
        if dev is not None:
            self._set(dev, _SEL_MUTE, _SCOPE_OUTPUT, ctypes.c_uint32, int(muted))

    def get_input_muted(self) -> bool | None:
        dev = self.input_device
        if dev is None:
            return None
        muted = self._get(dev, _SEL_MUTE, _SCOPE_INPUT, ctypes.c_uint32)
        if muted is not None:
            return bool(muted)
        volume = self._get(dev, _SEL_VOLUME_SCALAR, _SCOPE_INPUT, ctypes.c_float)
        return None if volume is None else float(volume) == 0.0

    def set_input_muted(self, muted: bool) -> None:
        dev = self.input_device
        if dev is None:
            return
        if self._get(dev, _SEL_MUTE, _SCOPE_INPUT, ctypes.c_uint32) is not None:
            self._set(dev, _SEL_MUTE, _SCOPE_INPUT, ctypes.c_uint32, int(muted))
            return
        if muted:
            volume = self._get(dev, _SEL_VOLUME_SCALAR, _SCOPE_INPUT, ctypes.c_float)
            if volume is not None:
                self._input_volume_backup = float(volume)
            self._set(dev, _SEL_VOLUME_SCALAR, _SCOPE_INPUT, ctypes.c_float, 0.0)
        else:
            restore = self._input_volume_backup
            self._set(dev, _SEL_VOLUME_SCALAR, _SCOPE_INPUT, ctypes.c_float,
                      0.5 if restore is None else restore)

    def start_listening(self, cb: Callable[[], None]) -> None:
        self._notify = cb
        self.ensure_listeners()

    def ensure_listeners(self) -> None:
        """(Re)point listeners at the current default devices."""
        dev = self.device
        if dev == self._device:
            return
        self._remove_listeners()
        self._device = dev
        if dev is None:
            return
        targets = [(dev, _SEL_VOLUME_SCALAR, _SCOPE_OUTPUT),
                   (dev, _SEL_MUTE, _SCOPE_OUTPUT),
                   (_SYSTEM_OBJECT, _SEL_DEFAULT_OUTPUT, _SCOPE_GLOBAL),
                   (_SYSTEM_OBJECT, _SEL_DEFAULT_INPUT, _SCOPE_GLOBAL)]
        for obj, selector, scope in targets:
            address = self._addr(selector, scope)

            def callback(_obj, _n, _addresses, _ctx, address=address):
                del address
                if self._notify is not None:
                    try:
                        self._notify()
                    except Exception:
                        log.debug("listener callback failed", exc_info=True)
                return 0

            c_callback = _PropertyListener(callback)
            status = _ca.AudioObjectAddPropertyListener(obj, ctypes.byref(address), c_callback, None)
            if status == 0:
                self._listeners.append((obj, address, c_callback))
            else:
                log.debug("AddPropertyListener %#x/%#x on %s -> %s", selector, scope, obj, status)

    def _remove_listeners(self) -> None:
        for obj, address, c_callback in self._listeners:
            _ca.AudioObjectRemovePropertyListener(obj, ctypes.byref(address), c_callback, None)
        self._listeners.clear()

    def close(self) -> None:
        self._remove_listeners()


class OsascriptControl:
    """Fallback audio control: `osascript` volume commands, failures swallowed."""

    def __init__(self) -> None:
        self._last_input_volume: int | None = None
        self._settings: dict[str, str] = {}

    @staticmethod
    def _run(script: str) -> str | None:
        try:
            done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                                  timeout=2)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("osascript %r failed: %s", script, exc)
            return None
        if done.returncode != 0:
            log.warning("osascript %r failed: %s", script, done.stderr.strip())
            return None
        return done.stdout.strip()

    def _read(self) -> dict[str, str]:
        out = self._run("get volume settings")
        if out is None:
            return {}
        settings: dict[str, str] = {}
        for part in out.split(","):
            if ":" in part:
                key, _, value = part.partition(":")
                settings[key.strip()] = value.strip()
        self._settings = settings
        return settings

    def refresh(self) -> None:
        """Drop the cached `get volume settings` so the next read hits the system."""
        self._settings = {}

    def _setting(self, name: str) -> str | None:
        if not self._settings:
            self._read()
        return self._settings.get(name)

    def probe(self) -> bool:
        return self._setting("output volume") is not None

    def get_volume(self) -> int | None:
        value = self._setting("output volume")
        return int(value) if value is not None and value.isdigit() else None

    def set_volume(self, percent: int) -> None:
        self._settings = {}
        self._run(f"set volume output volume {percent}")

    def get_muted(self) -> bool | None:
        value = self._setting("output muted")
        return None if value is None else value == "true"

    def set_muted(self, muted: bool) -> None:
        self._settings = {}
        self._run(f"set volume output muted {'true' if muted else 'false'}")

    def get_input_muted(self) -> bool | None:
        value = self._setting("input volume")
        return None if value is None else value == "0"

    def set_input_muted(self, muted: bool) -> None:
        self._settings = {}
        self._run(f"set volume input volume {0 if muted else 100}")

    def close(self) -> None:
        pass


class MacOSBackend(ActionBackend):
    def __init__(self, audio_mode: str = "auto", invert_scroll: bool = False) -> None:
        self.invert_scroll = invert_scroll
        self._pending_volume: int | None = None
        self._last_write = 0.0
        self._last_poll = 0.0
        self._audio_changed = threading.Event()
        self._audio: CoreAudioControl | OsascriptControl
        if audio_mode == "coreaudio":
            self._audio = CoreAudioControl()
            if not self._audio.probe():
                log.warning("[bridge] audio: coreaudio (probe failed, forced mode)")
            else:
                log.info("[bridge] audio: coreaudio")
        elif audio_mode == "osascript":
            self._audio = OsascriptControl()
            log.info("[bridge] audio: osascript")
        else:
            coreaudio = CoreAudioControl()
            if coreaudio.probe():
                self._audio = coreaudio
                log.info("[bridge] audio: coreaudio")
            else:
                self._audio = OsascriptControl()
                log.info("[bridge] audio: osascript (coreaudio probe failed)")
        if isinstance(self._audio, CoreAudioControl):
            self._audio.start_listening(self._audio_changed.set)

    # -- synthetic input ---------------------------------------------------
    @property
    def audio_control(self) -> str:
        return type(self._audio).__name__

    def preflight(self) -> bool:
        if Quartz.CGPreflightPostEventAccess():
            return True
        return bool(Quartz.CGRequestPostEventAccess())

    def _tap(self, name: str, *mods: int) -> None:
        # pyobjc owns the CGEventRef returned by CGEventCreate*: releasing it by hand
        # double-frees (SIGTRAP/SIGSEGV). GC releases it.
        flags = 0
        for mod in mods:
            flags |= mod
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, KEY_CODE[name], down)
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(kCGHIDEventTap, event)

    def media(self, key: str) -> None:
        for down in (True, False):
            state = KEY_STATE_DOWN if down else KEY_STATE_UP
            event = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                NSSystemDefined, (0.0, 0.0), KEY_FLAGS_DOWN if down else KEY_FLAGS_UP, 0.0, 0, None,
                SYSTEM_DEFINED_SUBTYPE, (NX_KEY[key] << 16) | (state << 8), -1)
            if event is None:
                log.warning("media key %r: NSEvent creation failed", key)
                return
            # -[NSEvent CGEvent] hands out a borrowed reference owned by the NSEvent.
            Quartz.CGEventPost(kCGHIDEventTap, event.CGEvent())

    def scroll(self, detents: int, horizontal: bool) -> None:
        if not detents:
            return
        if self.invert_scroll:
            detents = -detents
        sign = 1 if detents > 0 else -1
        for _ in range(min(abs(detents), MAX_DETENTS_PER_CALL)):
            wheel1 = 0 if horizontal else -SCROLL_STEP_PX * sign
            wheel2 = SCROLL_STEP_PX * sign if horizontal else 0
            event = Quartz.CGEventCreateScrollWheelEvent2(None, Quartz.kCGScrollEventUnitPixel, 1,
                                                          wheel1, wheel2, 0)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGScrollWheelEventIsContinuous, 1)
            Quartz.CGEventPost(kCGHIDEventTap, event)

    def tab_next(self) -> None:
        self._tap("tab", MOD["ctrl"])

    def tab_prev(self) -> None:
        self._tap("tab", MOD["ctrl"], MOD["shift"])

    def zoom_toggle(self) -> None:
        self._tap("8", MOD["alt"], MOD["cmd"])

    # -- audio -------------------------------------------------------------
    def set_volume(self, percent: int) -> None:
        self._pending_volume = percent

    def get_volume(self) -> int | None:
        return self._audio.get_volume()

    def set_muted(self, muted: bool) -> None:
        self._audio.set_muted(muted)

    def get_muted(self) -> bool | None:
        return self._audio.get_muted()

    def set_input_muted(self, muted: bool) -> None:
        self._audio.set_input_muted(muted)

    def get_input_muted(self) -> bool | None:
        return self._audio.get_input_muted()

    def poll_audio_state(self) -> tuple[int, bool, bool] | None:
        if isinstance(self._audio, CoreAudioControl):
            self._audio.ensure_listeners()
            if not self._audio_changed.is_set():
                return None
            self._audio_changed.clear()
        else:
            now = time.monotonic()
            if now - self._last_poll < 1.0:
                return None
            self._last_poll = now
            self._audio.refresh()
        volume = self._audio.get_volume()
        muted = self._audio.get_muted()
        input_muted = self._audio.get_input_muted()
        if volume is None or muted is None or input_muted is None:
            return None
        return (volume, muted, input_muted)

    def tick(self) -> None:
        if self._pending_volume is None:
            return
        if isinstance(self._audio, OsascriptControl):
            now = time.monotonic()
            if now - self._last_write < 1.0 / OSASCRIPT_MAX_HZ:
                return
            self._last_write = now
        volume, self._pending_volume = self._pending_volume, None
        self._audio.set_volume(volume)

    def close(self) -> None:
        self._audio.close()
