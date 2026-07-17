import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import import_sporttery


class OfficialMarketImportTest(unittest.TestCase):
    def test_fixtures_persist_each_explicit_single_market_flag(self):
        match = {
            "matchId": "001",
            "matchNumStr": "001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "isSingleHad": True,
            "isSingleHhad": "yes",
            "isSingleTtg": "false",
        }
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(import_sporttery, "DATA_DIR", Path(folder)):
                fixtures = import_sporttery.write_fixtures([match], date(2026, 7, 12))

            with fixtures.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual("true", row["is_single_had"])
        self.assertEqual("true", row["is_single_hhad"])
        self.assertEqual("false", row["is_single_ttg"])

    def test_zgzcw_dg_only_marks_had_as_single_eligible(self):
        parser = import_sporttery.ZgzcwMatchParser(date(2026, 7, 12))
        parser.feed(
            '<table><tr id="tr_001" t="2026-07-12 18:00" dg="1">'
            '<td class="wh-4"><a href="/soccer/team/1">Home</a></td>'
            '<td class="wh-6"><a href="/soccer/team/2">Away</a></td>'
            "</tr></table>"
        )

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(import_sporttery, "DATA_DIR", Path(folder)):
                fixtures = import_sporttery.write_fixtures(parser.matches, date(2026, 7, 12))

            with fixtures.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual("true", row["is_single_had"])
        self.assertEqual("false", row["is_single_hhad"])
        self.assertEqual("false", row["is_single_ttg"])


if __name__ == "__main__":
    unittest.main()
