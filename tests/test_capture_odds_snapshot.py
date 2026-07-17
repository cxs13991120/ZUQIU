import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import capture_odds_snapshot
import report_status
from report_status import FIXTURE_REQUIRED_FIELDS, OFFICIAL_FIXTURE_SOURCES


TARGET_DATE = "2026-07-16"
TARGET_DATE_VALUE = date.fromisoformat(TARGET_DATE)


class CaptureOddsSnapshotCliTest(unittest.TestCase):
    def run_main(self, root: Path, phase: str, capture_result):
        with patch.object(capture_odds_snapshot, "ROOT", root), patch.object(
            capture_odds_snapshot, "capture", return_value=capture_result
        ), patch.object(
            sys,
            "argv",
            ["capture_odds_snapshot.py", "--date", TARGET_DATE, "--phase", phase],
        ):
            return capture_odds_snapshot.main()

    def write_source_status(self, root: Path, payload: dict) -> None:
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "source_status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def write_fixtures(
        self,
        root: Path,
        rows=(),
        fieldnames=tuple(sorted(FIXTURE_REQUIRED_FIELDS)),
    ) -> None:
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        with (data / "fixtures.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_decision_returns_nonzero_when_capture_is_empty_and_zero_day_is_unproven(self):
        invalid_proofs = (
            None,
            {"target_date": TARGET_DATE, "fixture_count": 1, "no_fixtures": False},
            {"target_date": TARGET_DATE, "fixture_count": 0},
            {"target_date": "2026-07-15", "fixture_count": 0, "no_fixtures": True},
        )
        for source_status in invalid_proofs:
            with self.subTest(source_status=source_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if source_status is not None:
                    self.write_source_status(root, source_status)
                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_returns_zero_for_a_nonempty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json"
            snapshot.write_text(
                json.dumps({"matches": [{"match_id": "001"}]}),
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_rejects_an_unproven_empty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"matches": []}), encoding="utf-8")

            self.assertNotEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_accepts_a_proven_empty_written_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = next(iter(OFFICIAL_FIXTURE_SOURCES))
            self.write_source_status(root, {
                "source": source,
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"matches": []}), encoding="utf-8")

            self.assertEqual(0, self.run_main(root, "decision", snapshot))

    def test_decision_returns_zero_for_each_official_zero_fixture_proof(self):
        for source in sorted(OFFICIAL_FIXTURE_SOURCES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": source,
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                self.write_fixtures(root)
                self.assertEqual(0, self.run_main(root, "decision", None))

    def test_decision_rejects_nonofficial_zero_fixture_sources(self):
        for source in ("ESPN", "test", [], {}):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": source,
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                self.write_fixtures(root)
                try:
                    result = self.run_main(root, "decision", None)
                except TypeError as exc:
                    self.fail(
                        f"non-string source must return nonzero, not crash: {exc}"
                    )
                self.assertNotEqual(0, result)

    def test_decision_zero_fixture_proof_requires_a_readable_date_column(self):
        fixture_setups = (
            ("missing", None),
            ("bad header", ((), ("match_id",))),
        )
        for label, fixture_setup in fixture_setups:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "竞彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                })
                if fixture_setup is not None:
                    rows, fieldnames = fixture_setup
                    self.write_fixtures(root, rows=rows, fieldnames=fieldnames)
                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_zero_fixture_proof_rejects_a_target_date_csv_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_source_status(root, {
                "source": "竞彩网",
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root, rows=({"date": TARGET_DATE},))
            self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_zero_fixture_proof_rejects_conflicting_count_aliases(self):
        conflicts = (
            {"match_count": 1},
            {"fixtures_count": 1},
            {"fixtures_count": 0, "match_count": 1},
        )
        for aliases in conflicts:
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "竞彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                    **aliases,
                })
                self.write_fixtures(root)

                self.assertNotEqual(0, self.run_main(root, "decision", None))

    def test_decision_empty_capture_matches_report_zero_fixture_authority(self):
        self.assertTrue(
            hasattr(report_status, "verified_zero_fixture_day"),
            "report_status must expose the shared zero-fixture authority",
        )
        cases = (
            ({}, True),
            ({"match_count": 1}, False),
        )
        for aliases, expected in cases:
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_source_status(root, {
                    "source": "中国足彩网",
                    "target_date": TARGET_DATE,
                    "fixture_count": 0,
                    "no_fixtures": True,
                    **aliases,
                })
                self.write_fixtures(root)

                report_verified = report_status.verified_zero_fixture_day(
                    root, TARGET_DATE_VALUE
                )
                capture_verified = self.run_main(root, "decision", None) == 0

                self.assertEqual(expected, report_verified)
                self.assertEqual(report_verified, capture_verified)

    def test_decision_zero_fixture_proof_allows_rows_for_other_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_source_status(root, {
                "source": "中国足彩网",
                "target_date": TARGET_DATE,
                "fixture_count": 0,
                "no_fixtures": True,
            })
            self.write_fixtures(root, rows=({"date": "2026-07-15"},))
            self.assertEqual(0, self.run_main(root, "decision", None))

    def test_optional_phases_keep_empty_capture_success_semantics(self):
        for phase in ("opening", "monitoring"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(0, self.run_main(Path(tmp), phase, None))


class CaptureOddsSnapshotProductionTest(unittest.TestCase):
    def test_capture_keeps_an_empty_direct_schedule_without_fallback(self):
        captured_at = datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            capture_odds_snapshot, "SNAPSHOT_DIR", Path(tmp)
        ), patch.object(
            capture_odds_snapshot, "fetch_selling_matches", return_value=[]
        ) as fetch_direct, patch.object(
            capture_odds_snapshot, "fetch_zgzcw_matches"
        ) as fetch_fallback, patch.object(
            capture_odds_snapshot, "_load_odds", return_value={}
        ):
            output = capture_odds_snapshot.capture(date(2026, 7, 16), captured_at=captured_at)
            self.assertIsNotNone(output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("sporttery", payload["source"])
        self.assertEqual([], payload["matches"])
        fetch_direct.assert_called_once_with(date(2026, 7, 16))
        fetch_fallback.assert_not_called()

    def test_capture_preserves_direct_sporttery_market_eligibility(self):
        direct_match = {
            "matchId": "001",
            "matchNumStr": "001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "kickoff_at": "2026-07-16 20:00",
            "isSingleHad": True,
            "isSingleHhad": True,
            "isSingleTtg": True,
        }
        captured_at = datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            capture_odds_snapshot, "SNAPSHOT_DIR", Path(tmp)
        ), patch.object(
            capture_odds_snapshot,
            "fetch_selling_matches",
            return_value=[direct_match],
            create=True,
        ) as fetch_direct, patch.object(
            capture_odds_snapshot, "fetch_zgzcw_matches"
        ) as fetch_fallback, patch.object(
            capture_odds_snapshot,
            "_load_odds",
            return_value={"001": {"had": {}, "hhad": {}, "ttg": {}}},
        ):
            output = capture_odds_snapshot.capture(date(2026, 7, 16), captured_at=captured_at)
            self.assertIsNotNone(output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("sporttery", payload["source"])
        self.assertEqual(
            {"had": True, "hhad": True, "ttg": True},
            payload["matches"][0]["single_eligibility"],
        )
        fetch_direct.assert_called_once_with(date(2026, 7, 16))
        fetch_fallback.assert_not_called()

    def test_capture_uses_had_only_zgzcw_fallback_after_direct_failure(self):
        fallback_match = {
            "matchId": "001",
            "matchNumStr": "001",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "kickoff_at": "2026-07-16 20:00",
            "isSingleHad": True,
        }
        captured_at = datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            capture_odds_snapshot, "SNAPSHOT_DIR", Path(tmp)
        ), patch.object(
            capture_odds_snapshot,
            "fetch_selling_matches",
            side_effect=RuntimeError("Sporttery unavailable"),
            create=True,
        ) as fetch_direct, patch.object(
            capture_odds_snapshot, "fetch_zgzcw_matches", return_value=[fallback_match]
        ) as fetch_fallback, patch.object(
            capture_odds_snapshot,
            "_load_odds",
            return_value={"001": {"had": {}, "hhad": {}, "ttg": {}}},
        ):
            output = capture_odds_snapshot.capture(date(2026, 7, 16), captured_at=captured_at)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("zgzcw", payload["source"])
        self.assertEqual(
            {"had": True, "hhad": False, "ttg": False},
            payload["matches"][0]["single_eligibility"],
        )
        fetch_direct.assert_called_once_with(date(2026, 7, 16))
        fetch_fallback.assert_called_once_with(date(2026, 7, 16))


if __name__ == "__main__":
    unittest.main()
