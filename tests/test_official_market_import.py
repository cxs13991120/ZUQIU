import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import import_sporttery
import predict_today


class OfficialMarketImportTest(unittest.TestCase):
    def test_fixture_writer_preserves_match_number_and_exact_kickoff_at(self):
        kickoff_at = "2026-07-18 20:00"
        match = {
            "matchId": "001",
            "matchNumStr": "Saturday 001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "kickoff_at": kickoff_at,
        }

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(import_sporttery, "DATA_DIR", Path(folder)):
                fixtures = import_sporttery.write_fixtures(
                    [match], date(2026, 7, 18)
                )

            with fixtures.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual("Saturday 001", row["kickoff_local"])
        self.assertEqual("Saturday 001", row["match_num"])
        self.assertEqual(kickoff_at, row["kickoff_at"])

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

    def test_fixture_prediction_and_snapshot_share_exact_kickoff_identity(self):
        target_date = date(2026, 7, 18)
        match = {
            "matchId": "001",
            "matchNumStr": "001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "leagueNameAbbr": "Test League",
            "kickoff_at": "2026-07-18 20:00",
            "h": "2.00",
            "d": "3.20",
            "a": "3.50",
        }
        ratings = {
            team: predict_today.TeamRating(
                team=team,
                elo=1850,
                attack=0,
                defense=0,
                form=0,
                injury=0,
                rest_days=4,
                home_adv=0.08 if team == "Home" else 0,
            )
            for team in ("Home", "Away")
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(import_sporttery, "DATA_DIR", root / "data"),
                patch.object(predict_today, "DATA_DIR", root / "data"),
                patch.object(predict_today, "OUTPUT_DIR", root / "output"),
            ):
                (root / "data").mkdir()
                import_sporttery.write_fixtures([match], target_date)
                fixture = predict_today.load_fixtures()[0]
                prediction = predict_today.predict_fixture(
                    fixture, ratings, predict_today.read_config()
                )
                prediction_path = predict_today.write_csv([prediction], target_date)

            with prediction_path.open(encoding="utf-8-sig", newline="") as handle:
                persisted = next(csv.DictReader(handle))

        snapshot_match = {
            "match_id": "001",
            "team_a": "Home",
            "team_b": "Away",
            "kickoff_at": "2026-07-18 20:00",
        }
        expected_identity = tuple(
            snapshot_match[key] for key in ("team_a", "team_b", "kickoff_at")
        )
        self.assertEqual(
            expected_identity,
            (fixture.team_a, fixture.team_b, fixture.kickoff_at),
        )
        self.assertEqual(
            expected_identity,
            tuple(prediction[key] for key in ("team_a", "team_b", "kickoff_at")),
        )
        self.assertEqual(
            expected_identity,
            tuple(persisted[key] for key in ("team_a", "team_b", "kickoff_at")),
        )

    def test_fixture_loading_rejects_missing_blank_or_invalid_kickoff_at(self):
        cases = {
            "missing": None,
            "blank": "",
            "invalid": "not-a-kickoff",
        }
        for label, kickoff_at in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                data_dir = Path(folder)
                fields = [
                    "date",
                    "kickoff_local",
                    "stage",
                    "team_a",
                    "team_b",
                    "neutral",
                    "venue",
                ]
                if kickoff_at is not None:
                    fields.insert(2, "kickoff_at")
                with (data_dir / "fixtures.csv").open(
                    "w", encoding="utf-8-sig", newline=""
                ) as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    row = {
                        "date": "2026-07-18",
                        "kickoff_local": "Saturday 001",
                        "stage": "Test League",
                        "team_a": "Home",
                        "team_b": "Away",
                        "neutral": "false",
                        "venue": "Test Venue",
                    }
                    if kickoff_at is not None:
                        row["kickoff_at"] = kickoff_at
                    writer.writerow(row)

                with patch.object(predict_today, "DATA_DIR", data_dir):
                    with self.assertRaisesRegex(ValueError, "kickoff_at"):
                        predict_today.load_fixtures()


if __name__ == "__main__":
    unittest.main()
