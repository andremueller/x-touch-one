"""Device state machine: mido messages -> feedback (mido out) + backend actions."""

from __future__ import annotations

import logging
import time

import mido

from . import mcu

log_default = logging.getLogger("xtouch.engine")


class Engine:
    TICK_HZ = 60
    LED_FLASH_MS = 120
    LCD_NOTICE_MS = 1200
    #: `0B 7F` = "LCD backlight on, 127 min timeout" (MCU backlight saver). There is no
    #: "never", so the bridge re-sends it well inside the timeout to keep the display lit.
    #: Re-sending scribble-strip *text* instead is what blanked the X-Touch One display.
    BACKLIGHT_REFRESH_S = 60.0
    FADER_ECHO_SUPPRESS_MS = 300.0
    SHIFT_SCROLL_MULTIPLIER = 4

    def __init__(self, out, backend, *, invert_jog: bool = False,
                 now=time.monotonic, log: logging.Logger = log_default):
        self.out = out
        self.backend = backend
        self.invert_jog = invert_jog
        self.now = now
        self.log = log
        self.volume: int | None = None
        self.muted = False
        self.input_muted = False
        self.scrub_horizontal = False
        self.shift = False
        self.led_state: dict[int, bool] = {}
        self.flash_until: dict[int, float] = {}
        self.lcd_state: list[str] = ["DESKTOP", "READY"]
        self.lcd_shown: list[str | None] = [None, None]
        self.lcd_notice: tuple[list[str], float] | None = None
        self.backlight_at = 0.0
        self.last_fader_at = 0.0  # seconds, same clock as `now`

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.volume = None
        self.muted = False
        self.input_muted = False
        self.scrub_horizontal = False
        self.shift = False
        self.led_state.clear()
        self.flash_until.clear()
        self.lcd_state = ["DESKTOP", "READY"]
        self.lcd_shown = [None, None]
        self.lcd_notice = None
        self.last_fader_at = 0.0

        self.out.send(mcu.bridge_leds_off())
        self.out.send([mcu.device_query()])
        self.out.send([mcu.backlight_saver()])
        self.backlight_at = self.now()
        self.out.send([mcu.color(mcu.COLOR_WHITE)])
        self._set_lcd("DESKTOP", "READY")
        self.backend.preflight()

    # -- input -------------------------------------------------------------
    def handle(self, msg: mido.Message) -> None:
        if msg.type == "pitchwheel":
            percent = mcu.pitch_bend_to_percent(msg.pitch)
            if percent != self.volume:
                self.volume = percent
                self.last_fader_at = self.now()
                self.backend.set_volume(percent)
                self._set_lcd(*mcu.lcd_volume(percent))
                # Echo the position straight back: the One restores its fader to the last
                # value the host wrote when the fader is released, so without this it snaps
                # back to whatever the host wrote last (e.g. the boot-time value).
                self.out.send([msg])
            return
        if msg.type == "control_change":
            if msg.control == mcu.JOG_CC:
                delta = mcu.jog_delta(msg.value)
                if self.invert_jog:
                    delta = -delta
                if self.shift:
                    delta *= self.SHIFT_SCROLL_MULTIPLIER
                if delta:
                    self.backend.scroll(delta, self.scrub_horizontal)
            return
        if msg.type == "note_on" and msg.velocity > 0:
            self._note(msg.note)
            return
        self.log.debug("ignored %s", msg)

    def _note(self, note: int) -> None:
        if note == mcu.MUTE_NOTE:
            self.muted = not self.muted
            self.backend.set_muted(self.muted)
            self._set_led(note, self.muted)
            self._set_lcd("SYSTEM", "MUTED" if self.muted else "UNMUTE")
            self._color(mcu.COLOR_YELLOW if self.muted else mcu.COLOR_WHITE)
        elif note == mcu.REC_NOTE:
            self.input_muted = not self.input_muted
            self.backend.set_input_muted(self.input_muted)
            self._set_led(note, self.input_muted)
            self._set_lcd("MIC", "MUTED" if self.input_muted else "LIVE")
            self._color(mcu.COLOR_RED if self.input_muted else mcu.COLOR_WHITE)
        elif note == mcu.SCRUB_NOTE:
            self.scrub_horizontal = not self.scrub_horizontal
            self._set_led(note, self.scrub_horizontal)
            self._set_lcd("SCROLL", "HORIZ" if self.scrub_horizontal else "VERT")
        elif note == mcu.SHIFT_NOTE:
            self.shift = not self.shift
            self._set_led(note, self.shift)
        elif note == mcu.PLAY_NOTE:
            self.backend.media("play")
            self._flash(note)
        elif note == mcu.STOP_NOTE:
            self.backend.media("play")  # macOS has no NX_KEYTYPE_STOP
            self._flash(note)
        elif note == mcu.REW_NOTE:
            self.backend.media("prev")
            self._flash(note)
        elif note == mcu.FF_NOTE:
            self.backend.media("next")
            self._flash(note)
        elif note == mcu.BANK_PREV_NOTE:
            self.backend.tab_prev()
            self._flash(note)
            self._notice_lcd("TAB", "< PREV")
        elif note == mcu.BANK_NEXT_NOTE:
            self.backend.tab_next()
            self._flash(note)
            self._notice_lcd("TAB", "NEXT >")
        # CHANNEL ◀/▶ (48/49) are local-only: they arm the fader's channel and are ignored here.
        elif note == mcu.ZOOM_NOTE:
            self.backend.zoom_toggle()
            self._flash(note)
        else:
            self.log.debug("unmapped note %d", note)

    # -- periodic ----------------------------------------------------------
    def tick(self) -> None:
        now = self.now()
        for note, deadline in list(self.flash_until.items()):
            if deadline <= now:
                del self.flash_until[note]
                self._set_led(note, False)
        if self.lcd_notice is not None and self.lcd_notice[1] <= now:
            self.lcd_notice = None
            self._render_lcd()
        if now - self.backlight_at >= self.BACKLIGHT_REFRESH_S:
            self.backlight_at = now
            self.out.send([mcu.backlight_saver()])
        self.backend.tick()
        state = self.backend.poll_audio_state()
        if state is None:
            return
        volume, muted, input_muted = state
        self.log.debug("audio state vol=%s muted=%s in_muted=%s (engine vol=%s muted=%s)",
                       volume, muted, input_muted, self.volume, self.muted)
        if muted != self.muted:
            self.muted = muted
            self._set_led(mcu.MUTE_NOTE, muted)
            self._set_lcd("SYSTEM", "MUTED" if muted else "UNMUTE")
            self._color(mcu.COLOR_YELLOW if muted else mcu.COLOR_WHITE)
        if input_muted != self.input_muted:
            self.input_muted = input_muted
            self._set_led(mcu.REC_NOTE, input_muted)
            self._set_lcd("MIC", "MUTED" if input_muted else "LIVE")
            self._color(mcu.COLOR_RED if input_muted else mcu.COLOR_WHITE)
        if volume != self.volume:
            self.volume = volume
            self._set_lcd(*mcu.lcd_volume(volume))
            if (now - self.last_fader_at) * 1000.0 > self.FADER_ECHO_SUPPRESS_MS:
                # ch 1 only: the One's fader hangs off fader 1. Also writing the master fader
                # (ch 9) makes the unit bounce the fader back to a stale position.
                self.log.debug("motor write %s %% (polled)", volume)
                self.out.send([mcu.pitch_bend(volume)])

    # -- feedback ----------------------------------------------------------
    def _set_led(self, note: int, on: bool) -> None:
        if self.led_state.get(note) == on:
            return
        self.led_state[note] = on
        self.out.send([mcu.led(note, on)])

    def _flash(self, note: int) -> None:
        self._set_led(note, True)
        self.flash_until[note] = self.now() + self.LED_FLASH_MS / 1000.0

    def _color(self, code: int) -> None:
        self.out.send([mcu.color(code)])

    def _set_lcd(self, row1: str, row2: str) -> None:
        """Persistent LCD content; a running notice is dropped."""
        self.lcd_state = [row1, row2]
        self.lcd_notice = None
        self._render_lcd()

    def _notice_lcd(self, row1: str, row2: str) -> None:
        """Transient LCD content (e.g. what the channel buttons just sent)."""
        self.lcd_notice = ([row1, row2], self.now() + self.LCD_NOTICE_MS / 1000.0)
        self._render_lcd()

    def _render_lcd(self) -> None:
        rows = self.lcd_notice[0] if self.lcd_notice else self.lcd_state
        for index in (0, 1):
            if self.lcd_shown[index] != mcu._pad7(rows[index]):
                self._write_lcd_row(index, rows[index])

    def _write_lcd_row(self, index: int, text: str) -> None:
        offset = mcu.LCD_ROW1_OFFSET if index == 0 else mcu.LCD_ROW2_OFFSET
        self.lcd_shown[index] = mcu._pad7(text)
        self.out.send([mcu.lcd_row(offset, text)])
