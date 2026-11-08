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


class FindCatTest(unittest.TestCase):
    """The chest and counter boxes as measured in multiplayer, drawn on a dark background."""

    LIGHT = (232, 232, 230)
    ITEMS, EMOTES, COUNTER = (32, 52, 70, 98), (137, 52, 175, 96), (48, 102, 126, 124)

    def scene(self, *boxes):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (280, 205), (24, 24, 26))
        draw = ImageDraw.Draw(image)
        for box in boxes:
            draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=self.LIGHT)
        draw.text((60, 106), "78 713", fill=(70, 60, 60))  # digits split the counter's pixels a little
        return image

    def test_light_boxes_finds_each_box(self):
        boxes = bongo_auto.light_boxes(self.scene(self.ITEMS, self.EMOTES, self.COUNTER))
        self.assertIn(self.ITEMS, boxes)
        self.assertIn(self.EMOTES, boxes)
        self.assertEqual(len(boxes), 3)

    def test_cat_points_come_from_the_boxes(self):
        found = bongo_auto.cat_from_boxes([self.ITEMS, self.EMOTES, self.COUNTER])
        self.assertEqual(found["chests"], {"TIME_LEFT_h": (51, 75), "EMOTE_CHEST_TIME_LEFT_h": (156, 74)})
        self.assertEqual(found["head"], (103, 74))
        self.assertEqual(found["counter"], (50, 104, 124, 123))

    def test_a_name_tag_is_not_taken_for_the_counter(self):
        name_tag = (150, 10, 260, 32)  # another player's cat name, as wide as a counter
        found = bongo_auto.cat_from_boxes([name_tag, self.ITEMS, self.EMOTES, self.COUNTER])
        self.assertEqual(found["counter"], (50, 104, 124, 123))

    def test_nothing_found_without_both_chests(self):
        self.assertIsNone(bongo_auto.cat_from_boxes([self.ITEMS, self.COUNTER]))
        self.assertIsNone(bongo_auto.cat_from_boxes([]))


class ElevationTest(unittest.TestCase):
    def test_reads_our_own_process(self):
        self.assertIn(bongo_auto.is_elevated(os.getpid()), (True, False))

    def test_only_an_elevated_game_over_a_normal_farmer_blocks(self):
        cases = {(True, False): True, (True, True): False, (False, False): False, (None, False): False}
        for (game, farmer), blocked in cases.items():
            with mock.patch.object(bongo_auto, "is_elevated", side_effect=[game, farmer]):
                self.assertEqual(bongo_auto.game_runs_as_admin_over_us(0), blocked, (game, farmer))


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


if __name__ == "__main__":
    unittest.main()
