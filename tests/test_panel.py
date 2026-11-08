import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import panel  # noqa: E402

TODAY = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
SESSION = ("you are back: clicks paused | session: 78.380 -> 78.586, +206 toques in 0m 17s (11.5/s), "
           "0 chests | since launch: +206 toques, 0 chests in 1m 08s")


def at(minutes, message, day=TODAY):
    return day + timedelta(minutes=minutes), message


class ParseHelpersTest(unittest.TestCase):
    def test_to_int(self):
        self.assertEqual(panel.to_int("74.739"), 74739)
        self.assertEqual(panel.to_int("-120"), -120)
        self.assertIsNone(panel.to_int("?"))

    def test_parse_duration(self):
        self.assertEqual(panel.parse_duration("0m 17s"), 17)
        self.assertEqual(panel.parse_duration("1h 05m"), 3900)
        self.assertEqual(panel.parse_duration("nonsense"), 0)

    def test_login_users_picks_most_recent(self):
        vdf = '''"users"
{
    "76561198000000001"
    {
        "AccountName"    "first"
        "PersonaName"    "First"
        "MostRecent"     "0"
    }
    "76561198000000002"
    {
        "AccountName"    "second"
        "PersonaName"    "Second"
        "MostRecent"     "1"
    }
}'''
        self.assertEqual(panel.parse_login_users(vdf), ("76561198000000002", "second", "Second"))
        self.assertEqual(panel.parse_login_users(vdf.replace('"1"', '"0"'))[0], "76561198000000001")

    def test_login_users_without_accounts_fails(self):
        with self.assertRaises(RuntimeError):
            panel.parse_login_users('"users"\n{\n}')


class ScriptStateTest(unittest.TestCase):
    def test_latest_state_line_wins(self):
        entries = [at(0, "running: clicks after 15s idle"), at(1, "idle: clicking the cat | counter 1.000"),
                   at(2, "TIME_LEFT ready: click sent, chest claimed")]
        self.assertEqual(panel.script_state(entries)[0], "clicking")
        self.assertEqual(panel.script_state(entries + [at(3, SESSION)])[0], "paused")
        self.assertEqual(panel.script_state(entries + [at(3, "counter stuck: moving the mouse away")])[0], "stuck")
        self.assertEqual(panel.script_state(entries + [at(3, "another window on top: clicks paused")])[0], "paused")

    def test_empty_log(self):
        self.assertEqual(panel.script_state([])[0], "unknown")

    def test_refusal_and_closed_game_hide_the_previous_run(self):
        entries = [at(0, "idle: clicking the cat | counter 1.000")]
        refused = at(5, "Bongo Cat is not open: not starting (waited 30s). Open the game, then start the farmer again")
        self.assertEqual(panel.script_state(entries + [refused])[0], "stopped")
        closed = at(5, "Bongo Cat closed: clicks paused until it is back | session: counter unreadable, 0m 30s")
        self.assertEqual(panel.script_state(entries + [closed])[0], "paused")


class ClaimsTest(unittest.TestCase):
    def test_retries_within_five_minutes_count_once(self):
        entries = [at(0, "TIME_LEFT ready: click sent"), at(1, "TIME_LEFT ready: click sent"),
                   at(2, "EMOTE_CHEST_TIME_LEFT ready: click sent, chest claimed"),
                   at(3, "TIME_LEFT ready: click sent")]
        claims = panel.claims_today(entries)
        self.assertEqual([c["chest"] for c in claims], ["Items", "Emotes"])
        self.assertEqual(claims[0]["time"], "12:03")

    def test_unclaimed_and_old_clicks_are_ignored(self):
        entries = [at(0, "TIME_LEFT ready: click sent", day=TODAY - timedelta(days=1)),
                   at(0, "TIME_LEFT ready: click sent, not claimed (not enough toques?)"),
                   at(1, "TIME_LEFT ready: another window on top, skipped")]
        self.assertEqual(panel.claims_today(entries), [])


class CounterStatsTest(unittest.TestCase):
    def test_reads_counter_rate_and_totals(self):
        entries = [at(0, "running: clicks after 15s idle"), at(0, "counter at launch: 78.375"),
                   at(1, "idle: clicking the cat | counter 78.380"), at(2, SESSION)]
        stats = panel.counter_stats(entries)
        self.assertEqual(stats["counter"], 78586)
        self.assertEqual(stats["rate"], 11.5)
        self.assertEqual(stats["launch"]["toques"], 206)
        self.assertEqual(stats["launch"]["chests"], 0)
        self.assertEqual(stats["launch"]["duration"], "1m 08s")

    def test_a_new_launch_resets_the_totals(self):
        entries = [at(0, "running: clicks"), at(2, SESSION), at(5, "running: clicks")]
        stats = panel.counter_stats(entries)
        self.assertEqual(stats["launch"]["toques"], 0)
        self.assertIsNone(stats["rate"])


class TodayListsTest(unittest.TestCase):
    def test_sessions(self):
        sessions = panel.sessions_today([at(2, SESSION)])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["gain"], 206)
        self.assertEqual(sessions[0]["seconds"], 17)
        self.assertEqual(sessions[0]["start"], "12:01:43")
        self.assertEqual(sessions[0]["end"], "12:02:00")

    def test_drops(self):
        drops = panel.drops_today([at(0, "new item: Brown Bear (Hat) $0.12"),
                                   at(5, "new item: Face Base Emoji (Emote) no price")])
        self.assertEqual(drops[0], {"time": "12:05", "name": "Face Base Emoji", "type": "Emote", "usd": None})
        self.assertEqual(drops[1]["usd"], 0.12)

    def test_activity_pairs_starts_with_stops(self):
        entries = [at(0, "idle: clicking the cat"), at(5, SESSION),
                   at(10, "idle: clicking the cat"), at(12, "another window on top: clicks paused")]
        segments = panel.activity_today(entries)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0][1] - segments[0][0], 5 * 60 * 1000)

    def test_activity_still_clicking_runs_until_now(self):
        start = datetime.now() - timedelta(minutes=1)
        segments = panel.activity_today([(start, "idle: clicking the cat")])
        self.assertAlmostEqual(segments[0][1] - segments[0][0], 60 * 1000, delta=5000)


class SteamPagingTest(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(panel, "steam_account", return_value=("765", "acc", "Acc")),
                      mock.patch.object(panel.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def description(classid, name, marketable=1):
        return {"classid": classid, "instanceid": "0", "market_hash_name": name, "name": name,
                "type": "Hat", "icon_url": "icon" + classid, "marketable": marketable}

    def test_inventory_follows_more_items(self):
        pages = [
            {"assets": [{"assetid": "1", "classid": "10", "instanceid": "0", "amount": "1"},
                        {"assetid": "2", "classid": "10", "instanceid": "0", "amount": "1"}],
             "descriptions": [self.description("10", "Bear")], "more_items": 1, "last_assetid": "2"},
            {"assets": [{"assetid": "3", "classid": "20", "instanceid": "0", "amount": "1"}],
             "descriptions": [self.description("20", "Cap", marketable=0)]},
        ]
        with mock.patch.object(panel, "http_json", side_effect=pages) as http:
            items, assets = panel.fetch_inventory()
        self.assertEqual(http.call_count, 2)
        self.assertIn("start_assetid=2", http.call_args_list[1][0][0])
        self.assertEqual([(i["name"], i["count"], i["marketable"]) for i in items],
                         [("Bear", 2, True), ("Cap", 1, False)])
        self.assertEqual(set(assets), {"1", "2", "3"})

    def test_market_reads_every_page(self):
        def page(start, count, total):
            return {"total_count": total,
                    "results": [{"hash_name": f"item{n}", "sell_price": n, "sell_listings": 1}
                                for n in range(start, start + count)]}
        with mock.patch.object(panel, "http_json", side_effect=[page(0, 100, 150), page(100, 50, 150)]), \
                mock.patch.dict(panel.prices, {"items": {}, "updated": None, "error": None}):
            found = panel.fetch_market()
        self.assertEqual(len(found), 150)
        self.assertEqual(found["item120"], {"usd": 1.2, "listings": 1})


class NewItemsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = os.path.join(self.dir.name, "bongo_auto.log")
        known = os.path.join(self.dir.name, "known_assets.json")
        for patch in (mock.patch.object(panel, "LOG_PATH", self.log),
                      mock.patch.object(panel, "KNOWN_ASSETS_PATH", known),
                      mock.patch.dict(panel.prices, {"items": {"Bear": {"usd": 0.5, "listings": 3}}})):
            patch.start()
            self.addCleanup(patch.stop)

    def logged(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_first_run_only_records_then_new_assets_are_logged(self):
        panel.log_new_items({"1": ("Bear", "Hat", "Bear")})
        self.assertEqual(self.logged(), [])
        panel.log_new_items({"1": ("Bear", "Hat", "Bear"), "2": ("Bear", "Hat", "Bear"),
                             "3": ("Cap", "Hat", "Cap")})
        lines = self.logged()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("new item: Bear (Hat) $0.50"))
        self.assertTrue(lines[1].endswith("new item: Cap (Hat) no price"))

    def test_known_assets_survive_between_runs(self):
        panel.log_new_items({"1": ("Bear", "Hat", "Bear")})
        panel.log_new_items({"2": ("Bear", "Hat", "Bear")})  # "1" was sold: still remembered
        panel.log_new_items({"1": ("Bear", "Hat", "Bear"), "2": ("Bear", "Hat", "Bear")})
        self.assertEqual(len(self.logged()), 1)
        with open(panel.KNOWN_ASSETS_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["1", "2"])


if __name__ == "__main__":
    unittest.main()
