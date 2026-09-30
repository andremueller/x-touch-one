"""Mackie Control Universal protocol for the Behringer X-Touch One.

Pure functions: every builder returns ``mido.Message`` objects and performs no I/O.
"""

from __future__ import annotations

import mido

SYSEX_ID = bytes([0x00, 0x00, 0x66, 0x14])  # Mackie Designs / MCU device id (F0/F7 added by mido)

FADER_NOTE, MUTE_NOTE, REC_NOTE = 104, 16, 0
#: "FADER BANK ◀/▶" buttons (MCU Bank Left/Right); the bridge maps them to tab switching.
BANK_PREV_NOTE, BANK_NEXT_NOTE = 46, 47
#: "CHANNEL ◀/▶" buttons (MCU Channel Left/Right). Local-only on the X-Touch One: they
#: select/arm the fader's channel and are intentionally left unmapped on the host.
CHANNEL_PREV_NOTE, CHANNEL_NEXT_NOTE = 48, 49
SHIFT_NOTE, REW_NOTE, STOP_NOTE, PLAY_NOTE = 57, 91, 93, 94
FF_NOTE, ZOOM_NOTE, SCRUB_NOTE = 92, 100, 101
LED_NOTE_MAX = 117

JOG_CC = 60

#: MCU drives its 9 faders on pitch-bend MIDI channels 0-8: fader 1 = ch 1, master = ch 9.
FADER_CHANNEL, MASTER_FADER_CHANNEL = 0, 8

LCD_ROW1_OFFSET, LCD_ROW2_OFFSET = 0x00, 0x38

COLOR_RED, COLOR_GREEN, COLOR_YELLOW, COLOR_BLUE, COLOR_WHITE = 1, 2, 3, 4, 7

PITCHWHEEL_BIAS = 8192  # mido pitch is signed -8192..8191


def _sysex(body: bytes) -> mido.Message:
    return mido.Message("sysex", data=list(SYSEX_ID + body))


def _pad7(text: str) -> str:
    return text[:7].ljust(7)


def lcd_row(offset: int, text: str) -> mido.Message:
    return _sysex(bytes([0x12, offset]) + _pad7(text).encode("ascii"))


def color(code: int) -> mido.Message:
    return _sysex(bytes([0x72, code]) + bytes(7))


def device_query() -> mido.Message:
    return _sysex(b"\x00")


def backlight_saver() -> mido.Message:
    return _sysex(b"\x0b\x7f")


def led(note: int, on: bool) -> mido.Message:
    return mido.Message("note_on", note=note, velocity=127 if on else 0, channel=0)


def all_leds_off() -> list[mido.Message]:
    return [led(n, False) for n in range(LED_NOTE_MAX + 1)]


def pitch_bend(percent: int) -> mido.Message:
    pb = round(clamp(percent, 0, 100) * 16383 / 100)
    return mido.Message("pitchwheel", pitch=pb - PITCHWHEEL_BIAS, channel=0)


def motor_bend(percent: int) -> list[mido.Message]:
    """Fader position for the motor.

    The One's single fader maps to fader 1 (ch 1) or the master fader (ch 9) depending on
    which channel is selected locally, so the motor write targets both. Sending only fader
    1 (ch 1) is why the fader never moved: the master fader is channel 8 (MIDI ch 9).
    """
    pb = round(clamp(percent, 0, 100) * 16383 / 100) - PITCHWHEEL_BIAS
    return [mido.Message("pitchwheel", pitch=pb, channel=channel)
            for channel in (FADER_CHANNEL, MASTER_FADER_CHANNEL)]


def pitch_bend_to_percent(pitch: int) -> int:
    return round((pitch + PITCHWHEEL_BIAS) * 100 / 16383)


def jog_delta(value: int) -> int:
    v = value & 0x3F
    return -v if value & 0x40 else v


def lcd_volume(percent: int) -> tuple[str, str]:
    return ("MASTER", f"VOL{percent:4d}")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
