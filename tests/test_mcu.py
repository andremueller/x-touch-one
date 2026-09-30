import unittest

from xtouch_one import mcu

enc = lambda m: bytes(m.bin())  # noqa: E731 - Message.bytes() returns a list of ints

SYSEX = b"\xf0\x00\x00\x66\x14"


class TestLcd(unittest.TestCase):
    def test_row1(self):
        self.assertEqual(enc(mcu.lcd_row(0x00, "MASTER")), SYSEX + b"\x12\x00MASTER \xf7")

    def test_row2(self):
        self.assertEqual(enc(mcu.lcd_row(0x38, "VOL  42")), SYSEX + b"\x12\x38VOL  42\xf7")

    def test_truncates_and_pads_to_seven(self):
        for text in ("ABC", "1234567", "9012345678"):
            payload = enc(mcu.lcd_row(0x00, text))
            self.assertEqual(len(payload), len(SYSEX) + 2 + 7 + 1)
            self.assertEqual(payload[-1], 0xF7)
        self.assertEqual(enc(mcu.lcd_row(0x00, "ABCDEFGHI"))[7:14], b"ABCDEFG")

    def test_lcd_volume(self):
        self.assertEqual(mcu.lcd_volume(42), ("MASTER", "VOL  42"))
        self.assertEqual(mcu.lcd_volume(7), ("MASTER", "VOL   7"))


class TestSysex(unittest.TestCase):
    def test_color_has_eight_slots(self):
        self.assertEqual(enc(mcu.color(mcu.COLOR_YELLOW)),
                         SYSEX + b"\x72\x03" + bytes(7) + b"\xf7")
        self.assertEqual(mcu.COLOR_RED, 1)
        self.assertEqual(mcu.COLOR_GREEN, 2)
        self.assertEqual(mcu.COLOR_BLUE, 4)
        self.assertEqual(mcu.COLOR_WHITE, 7)

    def test_device_query(self):
        self.assertEqual(enc(mcu.device_query()), SYSEX + b"\x00\xf7")

    def test_backlight_saver(self):
        self.assertEqual(enc(mcu.backlight_saver()), SYSEX + b"\x0b\x7f\xf7")

    def test_sysex_id(self):
        self.assertEqual(mcu.SYSEX_ID, bytes([0x00, 0x00, 0x66, 0x14]))


class TestLeds(unittest.TestCase):
    def test_led_on_off(self):
        self.assertEqual(enc(mcu.led(16, True)), b"\x90\x10\x7f")
        self.assertEqual(enc(mcu.led(16, False)), b"\x90\x10\x00")

    def test_all_leds_off_sweeps_0_to_117(self):
        messages = mcu.all_leds_off()
        self.assertEqual(len(messages), 118)
        self.assertEqual(enc(messages[0]), b"\x90\x00\x00")
        self.assertEqual(enc(messages[-1]), b"\x90\x75\x00")
        self.assertEqual(mcu.LED_NOTE_MAX, 117)


class TestJog(unittest.TestCase):
    def test_signed_bit(self):
        self.assertEqual(mcu.jog_delta(0x01), 1)
        self.assertEqual(mcu.jog_delta(0x41), -1)
        self.assertEqual(mcu.jog_delta(0x02), 2)
        self.assertEqual(mcu.jog_delta(0x40), 0)

    def test_ignores_upper_bit(self):
        self.assertEqual(mcu.jog_delta(0x81), 1)


class TestPitchBend(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(enc(mcu.pitch_bend(0)), b"\xe0\x00\x00")
        self.assertEqual(enc(mcu.pitch_bend(100)), b"\xe0\x7f\x7f")

    def test_percent_to_pitch(self):
        self.assertEqual(mcu.pitch_bend(0).pitch, -mcu.PITCHWHEEL_BIAS)
        self.assertEqual(mcu.pitch_bend(100).pitch, 8191)

    def test_round_trip(self):
        for percent in (0, 1, 33, 37, 50, 99, 100):
            with self.subTest(percent=percent):
                self.assertEqual(mcu.pitch_bend_to_percent(mcu.pitch_bend(percent).pitch), percent)

    def test_clamped(self):
        self.assertEqual(mcu.pitch_bend(-20).pitch, mcu.pitch_bend(0).pitch)
        self.assertEqual(mcu.pitch_bend(300).pitch, mcu.pitch_bend(100).pitch)

    def test_percent_from_pitch(self):
        self.assertEqual(mcu.pitch_bend_to_percent(-8192), 0)
        self.assertEqual(mcu.pitch_bend_to_percent(8191), 100)
        self.assertEqual(mcu.pitch_bend_to_percent(0), 50)


class TestNoteMap(unittest.TestCase):
    def test_notes(self):
        self.assertEqual(mcu.FADER_NOTE, 104)
        self.assertEqual(mcu.MUTE_NOTE, 16)
        self.assertEqual(mcu.REC_NOTE, 0)
        self.assertEqual((mcu.CHANNEL_PREV_NOTE, mcu.CHANNEL_NEXT_NOTE), (48, 49))
        self.assertEqual((mcu.SHIFT_NOTE, mcu.REW_NOTE, mcu.STOP_NOTE, mcu.PLAY_NOTE), (57, 91, 93, 94))
        self.assertEqual((mcu.FF_NOTE, mcu.ZOOM_NOTE, mcu.SCRUB_NOTE), (92, 100, 101))
        self.assertEqual(mcu.JOG_CC, 60)
        self.assertEqual((mcu.LCD_ROW1_OFFSET, mcu.LCD_ROW2_OFFSET), (0x00, 0x38))


if __name__ == "__main__":
    unittest.main()
