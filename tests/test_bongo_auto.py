import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bongo_auto  # noqa: E402


class LayoutTest(unittest.TestCase):
    def test_matches_positions_measured_on_2560x1440(self):
        layout = bongo_auto.layout_for(0, 0, 2560, 1440)
        self.assertEqual(layout["grab"], (1286, 692))
        self.assertEqual(layout["drag_to"], (2429, 1347))
        self.assertEqual(layout["head"], (2395, 1325))
        self.assertEqual(layout["chests"], {
            "TIME_LEFT_h": (2345, 1327),
            "EMOTE_CHEST_TIME_LEFT_h": (2450, 1327),
        })
        self.assertEqual(layout["counter"], (2340, 1362, 2420, 1381))
        self.assertEqual(layout["view"], (2290, 1230, 2510, 1390))

    def test_follows_a_window_on_a_monitor_left_of_the_primary(self):
        layout = bongo_auto.layout_for(-2560, 0, 0, 1440)
        self.assertEqual(layout["head"], (-165, 1325))
        self.assertEqual(layout["grab"], (-1274, 692))

    def test_follows_a_smaller_screen(self):
        layout = bongo_auto.layout_for(0, 0, 1920, 1080)
        self.assertEqual(layout["head"], (1755, 965))
        self.assertEqual(layout["grab"], (966, 512))


class FormatTest(unittest.TestCase):
    def test_count_uses_dots_for_thousands(self):
        self.assertEqual(bongo_auto.fmt_count(74739), "74.739")
        self.assertEqual(bongo_auto.fmt_count(1234567), "1.234.567")
        self.assertEqual(bongo_auto.fmt_count(-120), "-120")
        self.assertEqual(bongo_auto.fmt_count(None), "?")

    def test_duration(self):
        self.assertEqual(bongo_auto.fmt_duration(17), "0m 17s")
        self.assertEqual(bongo_auto.fmt_duration(95), "1m 35s")
        self.assertEqual(bongo_auto.fmt_duration(3725), "1h 02m")


class RotateLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "bongo_auto.log")
        patches = [
            mock.patch.object(bongo_auto, "LOG_PATH", self.path),
            mock.patch.object(bongo_auto, "LOG_MAX_BYTES", 10),
            mock.patch.object(bongo_auto, "LOG_BACKUPS", 3),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.dir.cleanup)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_small_log_is_left_alone(self):
        self.write(self.path, "short")
        bongo_auto.rotate_log()
        self.assertEqual(self.read(self.path), "short")
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_big_log_shifts_and_keeps_three_backups(self):
        for n, text in ((1, "one"), (2, "two"), (3, "three")):
            self.write(f"{self.path}.{n}", text)
        self.write(self.path, "current log, past the limit")
        bongo_auto.rotate_log()
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.read(self.path + ".1"), "current log, past the limit")
        self.assertEqual(self.read(self.path + ".2"), "one")
        self.assertEqual(self.read(self.path + ".3"), "two")
        self.assertFalse(os.path.exists(self.path + ".4"))

    def test_missing_log_is_not_an_error(self):
        bongo_auto.rotate_log()

    def test_log_writes_after_rotating(self):
        self.write(self.path, "current log, past the limit")
        with mock.patch("builtins.print"):
            bongo_auto.log("hello")
        self.assertTrue(self.read(self.path).endswith(" hello\n"))
        self.assertEqual(self.read(self.path + ".1"), "current log, past the limit")


class StartupTest(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(bongo_auto.user32, "SetProcessDPIAware"),
                      mock.patch.object(bongo_auto.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_refuses_to_start_without_the_game(self):
        with mock.patch.object(bongo_auto, "game_window", return_value=0), \
                mock.patch.object(bongo_auto, "WAIT_FOR_GAME_SECONDS", 0), \
                mock.patch.object(bongo_auto.user32, "MessageBoxW") as box, \
                mock.patch.object(bongo_auto, "log") as log, \
                mock.patch.object(bongo_auto, "place_cat") as place:
            with self.assertRaises(SystemExit) as stop:
                bongo_auto.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("not open", log.call_args[0][0])
        self.assertFalse(place.called)
        flags = box.call_args[0][3]
        for flag in (bongo_auto.MB_ICONWARNING, bongo_auto.MB_SETFOREGROUND, bongo_auto.MB_TOPMOST):
            self.assertTrue(flags & flag, hex(flag))  # on top: Start.bat runs us minimized

    def test_waits_for_a_game_window_that_appears_late(self):
        with mock.patch.object(bongo_auto, "game_window", side_effect=[0, 0, 42]):
            self.assertEqual(bongo_auto.wait_for_game(30), 42)

    def test_gives_up_waiting_after_the_timeout(self):
        clock = iter(range(0, 100, 10))
        with mock.patch.object(bongo_auto, "game_window", return_value=0), \
                mock.patch.object(bongo_auto.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(bongo_auto.wait_for_game(30), 0)


if __name__ == "__main__":
    unittest.main()
