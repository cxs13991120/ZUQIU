import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from plan_lock import lock_plan, main, read_valid_lock


BJT = timezone(timedelta(hours=8))


class PlanLockTest(unittest.TestCase):
    def make_artifacts(self, root: Path) -> None:
        (root / "output").mkdir()
        (root / "data").mkdir()
        with (root / "output" / "betting_plan_2026-07-16.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "match", "stake"])
            writer.writeheader()
            writer.writerow({"date": "2026-07-16", "match": "A vs B", "stake": 20})
        (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
            json.dumps({"001": {"had": {"h": "2.00"}}}), encoding="utf-8"
        )

    def test_lock_is_valid_only_while_plan_and_odds_hashes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            self.assertIsNotNone(read_valid_lock(root, date(2026, 7, 16)))
            (root / "output" / "betting_plan_2026-07-16.csv").write_text(
                "changed", encoding="utf-8"
            )
            self.assertIsNone(read_valid_lock(root, date(2026, 7, 16)))

    def test_relocking_an_unchanged_plan_preserves_the_first_lock_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            first = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            second = lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 14, 5, tzinfo=BJT),
                "sporttery",
            )
            self.assertEqual(first, second)
            self.assertEqual("2026-07-16T13:31:00+08:00", second["locked_at_bjt"])

    def test_is_locked_cli_returns_zero_for_a_valid_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(0, main())

    def test_is_locked_cli_returns_one_for_a_missing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(1, main())

    def test_is_locked_cli_returns_one_for_an_invalid_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            lock_plan(
                root,
                date(2026, 7, 16),
                datetime(2026, 7, 16, 13, 31, tzinfo=BJT),
                "sporttery",
            )
            (root / "data" / "sporttery_odds_2026-07-16.json").write_text(
                "changed", encoding="utf-8"
            )
            with patch.object(sys, "argv", [
                "plan_lock.py", "is-locked", "--date", "2026-07-16"
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertEqual(1, main())

    def test_lock_cli_returns_nonzero_when_an_artifact_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "output").mkdir()
            (root / "data").mkdir()
            with patch.object(sys, "argv", [
                "plan_lock.py",
                "lock",
                "--date",
                "2026-07-16",
                "--locked-at",
                "2026-07-16T13:31:00+08:00",
                "--source",
                "sporttery",
            ]), patch.object(os, "getcwd", return_value=str(root)):
                self.assertNotEqual(0, main())

    def test_lock_cli_rejects_a_naive_locked_at_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_artifacts(root)
            with patch.object(sys, "argv", [
                "plan_lock.py",
                "lock",
                "--date",
                "2026-07-16",
                "--locked-at",
                "2026-07-16T13:31:00",
                "--source",
                "sporttery",
            ]), patch.object(os, "getcwd", return_value=str(root)):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertNotEqual(0, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
