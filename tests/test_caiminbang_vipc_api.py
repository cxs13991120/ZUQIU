import json
import unittest
from datetime import date
from unittest.mock import patch

from import_sporttery import (
    fetch_caiminbang_matches,
    fetch_vipc_odds,
)
from update_sporttery_results import FiveHundredResultParser


class TestCaiminbangVipcApi(unittest.TestCase):
    def test_fetch_caiminbang_matches_parsing(self):
        mock_payload = {
            "data": [
                {
                    "matchId": "10001",
                    "number": "周五001",
                    "homeName": "皇马",
                    "guestName": "巴萨",
                    "matchTime": "2026-07-24 23:00:00",
                    "matchName": "西甲",
                    "single": "1",
                }
            ]
        }
        with patch("import_sporttery.post_json", return_value=mock_payload):
            matches = fetch_caiminbang_matches(date(2026, 7, 24))
            self.assertEqual(len(matches), 1)
            item = matches[0]
            self.assertEqual(item["matchId"], "10001")
            self.assertEqual(item["matchNumStr"], "周五001")
            self.assertEqual(item["homeTeam"], "皇马")
            self.assertEqual(item["awayTeam"], "巴萨")
            self.assertEqual(item["isSingleHad"], "1")

    def test_fetch_vipc_odds_parsing(self):
        mock_payload = {
            "data": {
                "jyykSpf": {"h": "2.10", "d": "3.20", "a": "3.10"},
                "jyykRqspf": {"goal": "-1", "h": "4.10", "d": "3.80", "a": "1.70"},
            }
        }
        with patch("import_sporttery.fetch_json", return_value=mock_payload):
            odds = fetch_vipc_odds("10001")
            self.assertEqual(odds["had"], {"h": "2.10", "d": "3.20", "a": "3.10"})
            self.assertEqual(
                odds["hhad"],
                {"goalLine": "-1", "h": "4.10", "d": "3.80", "a": "1.70"},
            )

    def test_five_hundred_result_parser(self):
        sample_html = """
        <html>
        <body>
            <table>
                <tr>
                    <td>周日012</td>
                    <td>阿根廷</td>
                    <td>2:1</td>
                    <td>法国</td>
                </tr>
            </table>
        </body>
        </html>
        """
        parser = FiveHundredResultParser()
        results = parser.parse(sample_html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_num"], "周日012")
        self.assertEqual(results[0]["score"], "2:1")
        self.assertEqual(results[0]["home_goals"], "2")
        self.assertEqual(results[0]["away_goals"], "1")


if __name__ == "__main__":
    unittest.main()
