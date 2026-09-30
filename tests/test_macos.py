"""macOS backend: scroll axis construction (needs the pyobjc stack, not the hardware)."""

import sys
import unittest


@unittest.skipUnless(sys.platform == "darwin", "macOS backend")
class TestScrollAxes(unittest.TestCase):
    def setUp(self):
        from xtouch_one import macos

        self.macos = macos

    def test_vertical_uses_one_axis(self):
        step = self.macos.SCROLL_STEP_PX
        self.assertEqual(self.macos.scroll_axes(1, False), (1, -step, 0, 0))
        self.assertEqual(self.macos.scroll_axes(-1, False), (1, step, 0, 0))

    def test_horizontal_carries_two_axes(self):
        # Regression: with wheelCount=1 CoreGraphics discards wheel2, so a horizontal
        # event scrolled nothing at all.
        axes = self.macos.scroll_axes(1, True)
        self.assertEqual(axes[0], 2)
        self.assertEqual(axes, (2, 0, self.macos.HORIZONTAL_SIGN * self.macos.SCROLL_STEP_PX, 0, 0))

    def test_variadic_arity_matches_pyobjc(self):
        # pyobjc's variadic bridge demands wheelCount + 2 values after wheelCount; measured
        # against pyobjc 12.2.2. Fewer values raise before CoreGraphics is reached.
        for horizontal in (False, True):
            for detents in (1, -1):
                with self.subTest(horizontal=horizontal, detents=detents):
                    axes = self.macos.scroll_axes(detents, horizontal)
                    wheel_count, values = axes[0], axes[1:]
                    self.assertEqual(len(values), wheel_count + 2)
                    self.assertEqual(len(axes), wheel_count + 3)

    def test_axes_never_leak_into_each_other(self):
        self.assertEqual(self.macos.scroll_axes(3, True)[1], 0)
        self.assertEqual(self.macos.scroll_axes(-3, False)[2], 0)

    def test_positive_detents_move_along_the_axis_both_ways(self):
        # Measured: positive detents scroll towards the document end on either axis, so
        # both wheel values are negative (HORIZONTAL_SIGN keeps them symmetric).
        self.assertLess(self.macos.scroll_axes(1, False)[1], 0)
        self.assertLess(self.macos.scroll_axes(1, True)[2], 0)
        self.assertGreater(self.macos.scroll_axes(-1, True)[2], 0)
        self.assertGreater(self.macos.scroll_axes(-1, False)[1], 0)


if __name__ == "__main__":
    unittest.main()
