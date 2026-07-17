import copy
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betting_ledger import (
    ABNORMAL,
    LOST,
    PENDING,
    REFUNDED,
    WON,
    ingest_locked_plan,
    settle_pending,
    stable_bet_id,
    write_ledger_atomic,
)


BJT = timezone(timedelta(hours=8))
LOCKED_AT = datetime(2026, 7, 16, 13, 31, tzinfo=BJT)
SETTLED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=BJT)


def plan_row(**overrides):
    row = {
        "date": "2026-07-16",
        "strategy_version": "value-v4",
        "model_version": "model-3",
        "match_id": "1001",
        "team_a": "甲队",
        "team_b": "乙队",
        "kickoff_local": "2026-07-16T20:00:00+08:00",
        "play": "HAD",
        "market_type": "had",
        "market_line": "",
        "selection": "胜",
        "odds": "2.00",
        "locked_odds": "2.00",
        "odds_source": "sporttery",
        "odds_source_record_id": "odds-1001",
        "odds_captured_at_bjt": "2026-07-16T13:30:00+08:00",
        "raw_probability": "0.54",
        "calibrated_probability": "0.53",
        "official_market_probability": "0.50",
        "conservative_probability": "0.51",
        "edge": "0.01",
        "net_ev": "0.02",
        "full_kelly": "0.02",
        "kelly_fraction": "0.25",
        "data_quality_multiplier": "1.0",
        "volatility_multiplier": "1.0",
        "portfolio_rank": "1",
        "binding_limits": "daily",
        "stake": "20",
        "data_quality": "high",
        "volatility_band": "low",
    }
    row.update(overrides)
    return row


def lock(**overrides):
    payload = {
        "report_date": "2026-07-16",
        "locked_at_bjt": LOCKED_AT.isoformat(),
        "plan_sha256": "a" * 64,
        "odds_source": "sporttery",
    }
    payload.update(overrides)
    return payload


def finished(match_id, home, away, source_record_id=None):
    return {
        "match_id": match_id,
        "result_status": "finished",
        "home_goals": str(home),
        "away_goals": str(away),
        "result_source": "sporttery",
        "source_record_id": source_record_id or f"result-{match_id}",
        "captured_at_bjt": "2026-07-17T11:00:00+08:00",
    }


class IdentityAndIngestionTest(unittest.TestCase):
    def test_identity_uses_only_canonical_immutable_fields(self):
        first = plan_row()
        changed = plan_row(
            odds="9.99",
            locked_odds="9.99",
            stake="200",
            raw_probability="0.99",
            locked_at_bjt="2026-07-16T14:00:00+08:00",
            status=WON,
            return_amount="200.00",
        )

        identifier = stable_bet_id(first)
        self.assertRegex(identifier, r"^[0-9a-f]{64}$")
        self.assertEqual(identifier, stable_bet_id(changed))

        for field, value in (
            ("date", "2026-07-17"),
            ("strategy_version", "value-v5"),
            ("match_id", "1002"),
            ("play", "HHAD"),
            ("market_type", "hhad"),
            ("selection", "平"),
            ("market_line", "+1"),
        ):
            with self.subTest(field=field):
                candidate = plan_row(**{field: value})
                self.assertNotEqual(identifier, stable_bet_id(candidate))

    def test_parlay_identity_is_invariant_to_leg_and_json_key_order(self):
        legs = [
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": ""},
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": ""},
        ]
        first = plan_row(play="2-leg parlay", market_type="parlay", selection="展示标签", legs_json=json.dumps(legs, ensure_ascii=False))
        second = plan_row(play="2-leg parlay", market_type="parlay", selection="另一个标签", legs_json=json.dumps(list(reversed(legs)), ensure_ascii=False, sort_keys=True))

        self.assertEqual(stable_bet_id(first), stable_bet_id(second))

    def test_malformed_identity_fails_closed(self):
        for row in (
            plan_row(match_id=""),
            plan_row(date="not-a-date"),
            plan_row(play="2-leg parlay", market_type="parlay", legs_json="not-json"),
            plan_row(play="2-leg parlay", market_type="parlay", legs_json="[]"),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    stable_bet_id(row)

    def test_ingestion_migrates_legacy_keeps_first_row_and_never_overwrites_locked_values(self):
        legacy = {"date": "2026-07-16", "match": "甲队 vs 乙队", "play": "HAD", "selection": "胜", "odds": "1.80", "stake": "10", "legacy_note": "keep"}
        initial = ingest_locked_plan([legacy], [plan_row()], lock())
        self.assertEqual("keep", initial[0]["legacy_note"])
        self.assertRegex(initial[0]["bet_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(PENDING, initial[0]["status"])

        plan = plan_row(odds="2.00", locked_odds="2.00", stake="20")
        once = ingest_locked_plan([], [plan], lock())
        rerun = ingest_locked_plan(once, [plan_row(odds="7.00", locked_odds="7.00", stake="900")], lock())
        duplicate = copy.deepcopy(rerun[0])
        duplicate["locked_odds"] = "99.00"
        duplicate["stake"] = "999"
        deduplicated = ingest_locked_plan([rerun[0], duplicate], [], lock())

        self.assertEqual(1, len(rerun))
        self.assertEqual("2.00", rerun[0]["locked_odds"])
        self.assertEqual("20", rerun[0]["stake"])
        self.assertEqual(1, len(deduplicated))
        self.assertEqual("2.00", deduplicated[0]["locked_odds"])
        self.assertEqual(plan, plan_row())

    def test_ingestion_requires_a_valid_matching_domestic_lock(self):
        invalid_locks = (
            lock(report_date="2026-07-17"),
            lock(locked_at_bjt="2026-07-16T13:31:00"),
            lock(plan_sha256=""),
            lock(odds_source="external-market"),
        )
        for payload in invalid_locks:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ingest_locked_plan([], [plan_row()], payload)


class SettlementTest(unittest.TestCase):
    def settle_one(self, row, results):
        return settle_pending(ingest_locked_plan([], [row], lock()), results, SETTLED_AT)[0]

    def test_had_hhad_and_each_total_goal_bucket_settle_from_explicit_90_minute_scores(self):
        self.assertEqual(WON, self.settle_one(plan_row(selection="胜"), {"1001": finished("1001", 2, 1)})["status"])
        self.assertEqual(WON, self.settle_one(plan_row(play="HHAD", market_type="hhad", market_line="+1", selection="胜"), {"1001": finished("1001", 1, 1)})["status"])
        self.assertEqual(LOST, self.settle_one(plan_row(play="HHAD", market_type="hhad", market_line="-1", selection="胜"), {"1001": finished("1001", 1, 1)})["status"])
        for total in range(7):
            with self.subTest(total=total):
                row = plan_row(play="TTG", market_type="ttg", selection=f"{total}球")
                self.assertEqual(WON, self.settle_one(row, {"1001": finished("1001", total, 0)})["status"])
        self.assertEqual(WON, self.settle_one(plan_row(play="TTG", market_type="ttg", selection="7+球"), {"1001": finished("1001", 4, 3)})["status"])

    def test_two_leg_parlay_requires_both_legs_and_handles_loss_and_refunds(self):
        legs = [
            {"match_id": "1001", "market_type": "had", "selection": "胜", "line": "", "odds": "2.00"},
            {"match_id": "1002", "market_type": "ttg", "selection": "2球", "line": "", "odds": "3.00"},
        ]
        row = plan_row(play="2-leg parlay", market_type="parlay", legs_json=json.dumps(legs, ensure_ascii=False), locked_odds="6.00", stake="10")
        won = self.settle_one(row, {"1001": finished("1001", 1, 0), "1002": finished("1002", 2, 0)})
        self.assertEqual((WON, "60.00", "50.00"), (won["status"], won["return"], won["profit"]))

        lost = self.settle_one(row, {"1001": finished("1001", 0, 1), "1002": finished("1002", 2, 0)})
        self.assertEqual((LOST, "0.00", "-10.00"), (lost["status"], lost["return"], lost["profit"]))

        partial = self.settle_one(row, {"1001": finished("1001", 0, 1)})
        self.assertEqual(PENDING, partial["status"])

        refunded = {"match_id": "1002", "result_status": "refunded", "result_source": "sporttery", "source_record_id": "refund-1002", "captured_at_bjt": "2026-07-17T11:00:00+08:00"}
        mixed = self.settle_one(row, {"1001": finished("1001", 1, 0), "1002": refunded})
        self.assertEqual((WON, "20.00", "10.00"), (mixed["status"], mixed["return"], mixed["profit"]))

        fully_refunded = self.settle_one(plan_row(), {"1001": {**refunded, "match_id": "1001"}})
        self.assertEqual((REFUNDED, "20.00", "0.00"), (fully_refunded["status"], fully_refunded["return"], fully_refunded["profit"]))

    def test_unproven_results_do_not_mutate_pending_and_correction_is_explicit(self):
        pending = ingest_locked_plan([], [plan_row()], lock())
        baseline = copy.deepcopy(pending)
        cases = (
            {},
            {"1001": {**finished("1001", 1, 0), "result_status": "conflict"}},
            {"1001": {**finished("1001", 1, 0), "result_status": "unavailable"}},
            {"1001": {**finished("1001", "x", 0)}},
            {"wrong": finished("wrong", 1, 0)},
        )
        for results in cases:
            with self.subTest(results=results):
                self.assertEqual(baseline, settle_pending(pending, results, SETTLED_AT))

        invalid = settle_pending(pending, {"1001": {**finished("1001", 1, 0), "result_status": "invalid"}}, SETTLED_AT)
        self.assertEqual(ABNORMAL, invalid[0]["status"])
        unchanged = settle_pending(invalid, {"1001": finished("1001", 1, 0, "result-1001")}, SETTLED_AT, allow_correction=True)
        self.assertEqual(ABNORMAL, unchanged[0]["status"])
        reopened = settle_pending(invalid, {"1001": finished("1001", 1, 0, "changed")}, SETTLED_AT, allow_correction=True)
        self.assertEqual(PENDING, reopened[0]["status"])

    def test_settlement_is_byte_idempotent_and_only_changes_allowed_fields(self):
        pending = ingest_locked_plan([], [plan_row()], lock())
        settled = settle_pending(pending, {"1001": finished("1001", 1, 0)}, SETTLED_AT)
        second = settle_pending(settled, {"1001": finished("1001", 0, 1, "later-source")}, SETTLED_AT)
        self.assertEqual(settled, second)
        changed = {key for key in settled[0] if settled[0].get(key) != pending[0].get(key)}
        self.assertTrue(changed.issubset({"status", "result_status", "result_source", "source_record_id", "captured_at_bjt", "home_goals", "away_goals", "return", "profit", "result_legs_json", "settled_at_bjt"}))


class AtomicWriteTest(unittest.TestCase):
    def test_atomic_writer_is_deterministic_utf8_sig_and_preserves_unknown_fields(self):
        rows = ingest_locked_plan([], [plan_row(legacy_field="legacy")], lock())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.csv"
            self.assertEqual(path, write_ledger_atomic(path, rows))
            first = path.read_bytes()
            self.assertTrue(first.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", first)
            write_ledger_atomic(path, rows)
            self.assertEqual(first, path.read_bytes())
            with path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual("legacy", next(csv.DictReader(handle))["legacy_field"])


if __name__ == "__main__":
    unittest.main()
