"""CLI single-instance lock (no hardware, no MIDI)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xtouch_one.__main__ import (AlreadyRunning, _is_locked, acquire_lock, default_lock_file,
                                 lock_report, stop_instance)


class TestLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "bridge.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_missing_directories_and_records_pid(self):
        fd = acquire_lock(self.path)
        try:
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
            self.assertTrue(lock_report(self.path).endswith(f"held by PID {os.getpid()}"))
        finally:
            os.close(fd)

    def test_second_holder_is_refused_and_named(self):
        fd = acquire_lock(self.path)
        try:
            with self.assertRaises(AlreadyRunning) as caught:
                acquire_lock(self.path)
            self.assertEqual(caught.exception.pid, os.getpid())
            self.assertEqual(caught.exception.path, self.path)
            self.assertIn(f"PID {os.getpid()}", str(caught.exception))
        finally:
            os.close(fd)

    def test_lock_is_released_with_the_fd(self):
        fd = acquire_lock(self.path)
        os.close(fd)
        self.assertTrue(lock_report(self.path).endswith("free"))
        fd2 = acquire_lock(self.path)  # a second start succeeds once the holder is gone
        os.close(fd2)

    def test_stale_pid_does_not_block_a_start(self):
        # A crashed holder leaves its PID behind; the kernel drops the flock, so the next
        # start must succeed and overwrite the number.
        self.path.parent.mkdir(parents=True)
        self.path.write_text("999999\n")
        fd = acquire_lock(self.path)
        try:
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        finally:
            os.close(fd)

    def test_report_on_free_lock_without_pid_file(self):
        self.assertIn(": free", lock_report(self.path))


class TestDefaultLockPath(unittest.TestCase):
    def test_honours_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-state"}, clear=False):
            self.assertEqual(default_lock_file(),
                             Path("/tmp/xdg-state/xtouch-one/bridge.lock"))

    def test_not_in_the_per_boot_temp_directory(self):
        # Regression: tempfile.gettempdir() is $TMPDIR on macOS (/var/folders/…/T), so a
        # foreground run and the LaunchAgent would have held different locks.
        with mock.patch.dict(os.environ, {}, clear=True):
            path = default_lock_file()
        self.assertNotIn(str(Path(tempfile.gettempdir())), str(path))
        self.assertTrue(str(path).startswith(str(Path.home())))

    @unittest.skipUnless(sys.platform == "darwin", "macOS layout")
    def test_macos_location(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_lock_file(),
                             Path.home() / "Library" / "Application Support" / "xtouch-one"
                             / "bridge.lock")


class TestStopInstance(unittest.TestCase):
    HOLDER = ("import sys, time\n"
              "from pathlib import Path\n"
              "from xtouch_one.__main__ import acquire_lock\n"
              "acquire_lock(Path(sys.argv[1]))\n"
              "print('held', flush=True)\n"
              "time.sleep(60)\n")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "bridge.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def test_signals_the_holder_and_waits_for_the_lock(self):
        holder = subprocess.Popen([sys.executable, "-c", self.HOLDER, str(self.path)],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            holder.stdout.close()
            self.assertEqual(stop_instance(self.path, timeout=5.0), 0)
            holder.wait(timeout=5)  # SIGTERM reached the holder (its sleep is interrupted)
            self.assertFalse(_is_locked(self.path))
        finally:
            if holder.stdout and not holder.stdout.closed:
                holder.stdout.close()
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)

    def test_free_lock_is_a_no_op(self):
        self.assertEqual(stop_instance(self.path), 0)

    def test_refuses_to_signal_a_foreign_pid(self):
        # Hold the lock ourselves but pretend a foreign process owns it: the guard must not
        # deliver a signal to whatever PID happens to be in the file.
        fd = acquire_lock(self.path)
        try:
            self.path.write_text("1\n")  # PID 1 = launchd, its command line is not the bridge
            self.assertEqual(stop_instance(self.path, timeout=0.5), 1)
            self.assertTrue(_is_locked(self.path))
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
