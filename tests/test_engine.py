import logging
import unittest

import mido

from xtouch_one import mcu
from xtouch_one.backend import DummyBackend
from xtouch_one.engine import Engine

enc = lambda m: bytes(m.bin())  # noqa: E731


def setUpModule():
    logging.getLogger("xtouch.backend").setLevel(logging.WARNING)


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """Stands in for MidiIO: collects every send() batch as encoded bytes."""

    def __init__(self) -> None:
        self.batches: list[list[bytes]] = []
        self.messages: list[bytes] = []

    def send(self, messages) -> None:
        batch = [enc(m) for m in messages]
        self.batches.append(batch)
        self.messages.extend(batch)

    def has(self, raw: bytes) -> bool:
        return raw in self.messages


def note_on(note: int) -> mido.Message:
    return mido.Message("note_on", note=note, velocity=127)


def color_bytes(code: int) -> bytes:
    return enc(mcu.color(code))


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.backend = DummyBackend()
        self.out = Recorder()
        self.engine = Engine(self.out, self.backend, now=self.clock)
        self.engine.start()
        self.backend.calls.clear()
        self.out.batches.clear()
        self.out.messages.clear()

    def press(self, note: int) -> None:
        self.engine.handle(note_on(note))

    def lcd(self, offset: int, text: str) -> bytes:
        return enc(mcu.lcd_row(offset, text))


class TestStart(EngineTestCase):
    def test_initialises_device(self):
        clock = Clock()
        out = Recorder()
        engine = Engine(out, DummyBackend(), now=clock)
        engine.start()
        self.assertEqual(len(out.batches[0]), 118)  # all LEDs off in one send
        self.assertEqual(out.batches[0][0], b"\x90\x00\x00")
        self.assertEqual(out.batches[0][-1], b"\x90\x75\x00")
        self.assertEqual(out.batches[1], [enc(mcu.device_query())])
        self.assertEqual(out.batches[2], [enc(mcu.backlight_saver())])
        self.assertEqual(out.batches[3], [color_bytes(mcu.COLOR_WHITE)])
        self.assertEqual(out.batches[4], [self.lcd(mcu.LCD_ROW1_OFFSET, "DESKTOP")])
        self.assertEqual(out.batches[5], [self.lcd(mcu.LCD_ROW2_OFFSET, "READY")])
        self.assertEqual(len(out.batches), 6)
        self.assertIsNone(engine.volume)
        self.assertFalse(engine.muted)

    def test_start_is_idempotent(self):
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.engine.handle(note_on(mcu.MUTE_NOTE))
        self.out.batches.clear()
        self.engine.start()
        self.assertIsNone(self.engine.volume)
        self.assertFalse(self.engine.muted)
        self.assertEqual(len(self.out.batches[0]), 118)
        self.assertEqual(self.out.batches[0][0], b"\x90\x00\x00")
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "DESKTOP")))


class TestMute(EngineTestCase):
    def test_mute_toggles_backend_led_lcd_colour(self):
        self.press(mcu.MUTE_NOTE)
        self.assertEqual(self.backend.calls, [("set_muted", True)])
        self.assertTrue(self.out.has(b"\x90\x10\x7f"))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "SYSTEM")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "MUTED")))
        self.assertTrue(self.out.has(color_bytes(mcu.COLOR_YELLOW)))

        self.backend.calls.clear()
        self.press(mcu.MUTE_NOTE)
        self.assertEqual(self.backend.calls, [("set_muted", False)])
        self.assertTrue(self.out.has(b"\x90\x10\x00"))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "UNMUTE")))
        self.assertTrue(self.out.has(color_bytes(mcu.COLOR_WHITE)))

    def test_repeated_identical_led_write_is_suppressed(self):
        self.out.batches.clear()
        self.engine._set_led(mcu.MUTE_NOTE, True)
        self.assertEqual(self.out.batches, [[b"\x90\x10\x7f"]])
        self.engine._set_led(mcu.MUTE_NOTE, True)
        self.assertEqual(len(self.out.batches), 1)
        self.engine._set_led(mcu.MUTE_NOTE, False)
        self.assertEqual(self.out.batches[-1], [b"\x90\x10\x00"])


class TestInputMute(EngineTestCase):
    def test_rec_note(self):
        self.press(mcu.REC_NOTE)
        self.assertEqual(self.backend.calls, [("set_input_muted", True)])
        self.assertTrue(self.out.has(b"\x90\x00\x7f"))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "MIC")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "MUTED")))
        self.assertTrue(self.out.has(color_bytes(mcu.COLOR_RED)))

        self.backend.calls.clear()
        self.press(mcu.REC_NOTE)
        self.assertEqual(self.backend.calls, [("set_input_muted", False)])
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "LIVE")))
        self.assertTrue(self.out.has(color_bytes(mcu.COLOR_WHITE)))


class TestScroll(EngineTestCase):
    def test_scrub_toggles_axis(self):
        self.press(mcu.SCRUB_NOTE)
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "SCROLL")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "HORIZ")))
        self.assertTrue(self.out.has(b"\x90\x65\x7f"))

        self.engine.handle(mido.Message("control_change", control=mcu.JOG_CC, value=1))
        self.assertEqual(self.backend.calls, [("scroll", 1, True)])

        self.press(mcu.SCRUB_NOTE)
        self.engine.handle(mido.Message("control_change", control=mcu.JOG_CC, value=1))
        self.assertEqual(self.backend.calls[-1], ("scroll", 1, False))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "VERT")))

    def test_shift_multiplies_jog(self):
        self.press(mcu.SHIFT_NOTE)
        self.assertTrue(self.out.has(b"\x90\x39\x7f"))
        self.engine.handle(mido.Message("control_change", control=mcu.JOG_CC, value=1))
        self.assertEqual(self.backend.calls, [("scroll", 4, False)])

    def test_jog_direction_and_invert(self):
        self.engine.handle(mido.Message("control_change", control=mcu.JOG_CC, value=0x41))
        self.assertEqual(self.backend.calls, [("scroll", -1, False)])

        inverted = Engine(Recorder(), DummyBackend(), invert_jog=True, now=self.clock)
        inverted.start()
        inverted.handle(mido.Message("control_change", control=mcu.JOG_CC, value=0x41))
        self.assertEqual(inverted.backend.calls, [("scroll", 1, False)])

    def test_zero_detent_jog_is_ignored(self):
        self.engine.handle(mido.Message("control_change", control=mcu.JOG_CC, value=0x40))
        self.assertEqual(self.backend.calls, [])

    def test_other_controllers_ignored(self):
        self.engine.handle(mido.Message("control_change", control=16, value=64))
        self.engine.handle(mido.Message("control_change", control=7, value=64))
        self.assertEqual(self.backend.calls, [])


class TestTransport(EngineTestCase):
    def test_media_buttons_and_flash(self):
        self.press(mcu.PLAY_NOTE)
        self.assertEqual(self.backend.calls, [("media", "play")])
        self.assertTrue(self.out.has(b"\x90\x5e\x7f"))

        self.clock.advance(0.2)
        self.engine.tick()
        self.assertTrue(self.out.has(b"\x90\x5e\x00"))

    def test_stop_sends_play(self):
        self.press(mcu.STOP_NOTE)
        self.assertEqual(self.backend.calls, [("media", "play")])

    def test_rewind_and_ff(self):
        self.press(mcu.REW_NOTE)
        self.press(mcu.FF_NOTE)
        self.assertEqual(self.backend.calls, [("media", "prev"), ("media", "next")])

    def test_flash_survives_within_window(self):
        self.press(mcu.PLAY_NOTE)
        self.clock.advance(0.05)
        self.engine.tick()
        self.assertFalse(self.out.has(b"\x90\x5e\x00"))

    def test_tabs_and_zoom(self):
        self.press(mcu.CHANNEL_PREV_NOTE)
        self.press(mcu.CHANNEL_NEXT_NOTE)
        self.press(mcu.ZOOM_NOTE)
        self.assertEqual(self.backend.calls, [("tab_prev",), ("tab_next",), ("zoom_toggle",)])
        for note in (mcu.CHANNEL_PREV_NOTE, mcu.CHANNEL_NEXT_NOTE, mcu.ZOOM_NOTE):
            self.assertTrue(self.out.has(bytes([0x90, note, 0x7F])))

    def test_note_off_and_unknown_notes_ignored(self):
        self.engine.handle(mido.Message("note_off", note=mcu.MUTE_NOTE))
        self.engine.handle(note_on(95))          # transport RECORD: unassigned
        self.engine.handle(note_on(mcu.FADER_NOTE))  # fader touch: no action
        self.engine.handle(mido.Message("clock"))
        self.assertEqual(self.backend.calls, [])


class TestChannelButtons(EngineTestCase):
    def test_prev_shows_notice_then_restores(self):
        self.press(mcu.CHANNEL_PREV_NOTE)
        self.assertEqual(self.backend.calls, [("tab_prev",)])
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "TAB")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "< PREV")))

        self.clock.advance(0.5)
        self.engine.tick()
        self.assertFalse(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "DESKTOP")))  # still showing

        self.clock.advance(1.0)  # past LCD_NOTICE_MS
        self.engine.tick()
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "DESKTOP")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "READY")))

    def test_next_shows_notice(self):
        self.press(mcu.CHANNEL_NEXT_NOTE)
        self.assertEqual(self.backend.calls, [("tab_next",)])
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "NEXT >")))

    def test_notice_restores_the_previous_state_not_a_default(self):
        self.press(mcu.MUTE_NOTE)
        self.out.messages.clear()
        self.press(mcu.CHANNEL_NEXT_NOTE)
        self.clock.advance(1.3)
        self.engine.tick()
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW1_OFFSET, "SYSTEM")))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "MUTED")))

    def test_user_action_cancels_the_notice(self):
        self.press(mcu.CHANNEL_PREV_NOTE)
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "VOL  50")))
        self.clock.advance(1.3)
        self.out.messages.clear()
        self.engine.tick()
        # only the expired LED flash is sent; the notice is gone and nothing is re-rendered
        self.assertEqual(self.out.messages, [b"\x90\x30\x00"])

    def test_notice_does_not_repeat_unchanged_rows(self):
        self.press(mcu.CHANNEL_NEXT_NOTE)
        self.assertEqual(len(self.out.batches), 3)  # LED + two LCD rows
        self.clock.advance(0.2)
        self.engine.tick()  # let the LED flash expire
        batches = len(self.out.batches)
        self.press(mcu.CHANNEL_NEXT_NOTE)
        self.assertEqual(len(self.out.batches), batches + 1)  # LED only; rows unchanged


class TestFader(EngineTestCase):
    def test_deduplicates_rounded_percent(self):
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.assertEqual(self.backend.calls, [("set_volume", 50)])
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "VOL  50")))

    def test_full_sweep_emits_at_most_one_action_per_percent(self):
        seen = []
        for pitch in range(-8192, 8192, 64):
            before = len(self.backend.calls)
            self.engine.handle(mido.Message("pitchwheel", pitch=pitch, channel=0))
            if len(self.backend.calls) > before:
                seen.append(self.backend.calls[-1][1])
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(seen[0], 0)
        self.assertLessEqual(len(seen), 101)

    def test_tick_right_after_fader_move_emits_no_bend(self):
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.clock.advance(0.1)  # inside the 300 ms echo suppression window
        self.backend.audio_state = (33, False, False)
        self.engine.tick()
        self.assertFalse(self.out.has(b"\xe0\x1e\x2a"))
        self.assertEqual(self.backend.calls, [("set_volume", 50)])

    def test_external_change_moves_motor_without_calling_back(self):
        self.engine.handle(mido.Message("pitchwheel", pitch=0, channel=0))
        self.backend.calls.clear()
        self.clock.advance(0.4)  # past the echo suppression window
        self.backend.audio_state = (33, False, False)
        self.engine.tick()
        self.assertTrue(self.out.has(b"\xe0\x1e\x2a"))
        self.assertTrue(self.out.has(self.lcd(mcu.LCD_ROW2_OFFSET, "VOL  33")))
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.out.messages.count(b"\xe0\x1e\x2a"), 1)

        self.out.messages.clear()
        self.engine.tick()
        self.assertFalse(self.out.has(b"\xe0\x1e\x2a"))

    def test_external_mute_and_input_mute_never_call_backend(self):
        self.backend.audio_state = (50, True, True)
        self.engine.tick()
        self.assertEqual(self.backend.calls, [])
        self.assertTrue(self.out.has(b"\x90\x10\x7f"))   # LED 16
        self.assertTrue(self.out.has(b"\x90\x00\x7f"))   # LED 0
        self.assertTrue(self.engine.muted)
        self.assertTrue(self.engine.input_muted)


class TestTickIdle(EngineTestCase):
    def test_idle_sends_nothing_until_the_backlight_refresh(self):
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.out.messages, [])  # nothing changed, refresh not due

        self.clock.advance(Engine.BACKLIGHT_REFRESH_S + 0.1)
        self.out.messages.clear()
        self.engine.tick()
        self.assertEqual(self.out.messages, [enc(mcu.backlight_saver())])
        self.assertEqual(self.backend.calls, [])  # no volume/mute traffic while idle

    def test_backlight_refresh_is_periodic(self):
        self.out.messages.clear()
        self.clock.advance(Engine.BACKLIGHT_REFRESH_S + 0.1)
        self.engine.tick()
        self.clock.advance(Engine.BACKLIGHT_REFRESH_S + 0.1)
        self.engine.tick()
        self.assertEqual(self.out.messages, [enc(mcu.backlight_saver())] * 2)

    def test_backlight_refresh_does_not_touch_the_scribble_strip(self):
        self.out.messages.clear()
        self.clock.advance(Engine.BACKLIGHT_REFRESH_S * 10)
        self.engine.tick()
        self.assertEqual(self.out.messages, [enc(mcu.backlight_saver())])


if __name__ == "__main__":
    unittest.main()
