import csv
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from audit_shadow_portfolio import (
    audit_generated_portfolios,
    run_audit,
    validate_audit_payload,
)
from betting_ledger import stable_bet_id
from official_markets import THREE_WAY_SELECTIONS


BEIJING = timezone(timedelta(hours=8))


def config(mode="shadow"):
    return {
        "max_daily_budget": 500,
        "value_strategy": {
            "activation_mode": mode,
            "stake_unit": 2,
            "max_match_exposure": 200,
            "max_daily_combo_stake": 30,
        },
        "simulation_account": {
            "mode": "simulation",
            "monthly_budget_cap": 5000,
            "real_money_automation": False,
        },
    }


def single_row(
    report_date="2026-07-11",
    match_id="m1",
    *,
    market_type="had",
    play="HAD",
    selection=None,
    line="",
    stake=20,
    source="sporttery",
    expected_value=0.10,
):
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "value-v4",
        "match_id": match_id,
        "play": play,
        "market_type": market_type,
        "market_line": line,
        "selection": selection or THREE_WAY_SELECTIONS["h"],
        "legs_json": "[]",
        "odds_source": source,
        "odds_source_record_id": f"snapshot#{match_id}:{market_type}",
        "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_odds": "2.00",
        "odds": "2.00",
        "conservative_probability": 0.55,
        "expected_value": expected_value,
        "net_ev": expected_value,
        "stake": stake,
        "profit": -float(stake),
        "return": 0,
    }
    row["bet_id"] = stable_bet_id(row)
    return row


def parlay_row(
    report_date="2026-07-11",
    *,
    match_ids=("p1", "p2"),
    stake=20,
    source="sporttery",
    expected_value=0.10,
):
    legs = [
        {
            "match_id": match_id,
            "market_type": "had",
            "selection": THREE_WAY_SELECTIONS["h"],
            "line": "",
            "odds": "2.00",
            "odds_source": source,
            "odds_source_record_id": f"snapshot#{match_id}:had",
            "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
        }
        for match_id in match_ids
    ]
    row = {
        "date": report_date,
        "report_date": report_date,
        "strategy_version": "value-v4",
        "model_version": "value-v4",
        "match_id": "",
        "play": "PARLAY",
        "market_type": "parlay",
        "market_line": "",
        "selection": " + ".join(leg["selection"] for leg in legs),
        "legs_json": json.dumps(legs, ensure_ascii=False, sort_keys=True),
        "odds_source": source,
        "odds_source_record_id": json.dumps(
            sorted(leg["odds_source_record_id"] for leg in legs)
        ),
        "odds_captured_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_at_bjt": f"{report_date}T12:00:00+08:00",
        "locked_odds": str(2 ** len(legs)),
        "odds": str(2 ** len(legs)),
        "conservative_probability": 0.30,
        "expected_value": expected_value,
        "net_ev": expected_value,
        "stake": stake,
        "profit": -float(stake),
        "return": 0,
    }
    try:
        row["bet_id"] = stable_bet_id(row)
    except ValueError:
        row["bet_id"] = f"invalid-parlay-{len(legs)}"
    return row


def violation_codes(payload):
    return {item["code"] for item in payload["violations"]}


class MechanicalPortfolioAuditTest(unittest.TestCase):
    def test_mechanical_gate_does_not_require_positive_historical_roi(self):
        payload = audit_generated_portfolios(
            {"2026-07-11": [single_row()]}, config()
        )

        self.assertTrue(payload["passed"])
        self.assertEqual([], payload["violations"])
        self.assertNotIn("roi", json.dumps(payload).lower())

    def test_zero_checked_dates_fails_closed(self):
        payload = audit_generated_portfolios({}, config())

        self.assertFalse(payload["passed"])
        self.assertIn("zero_checked_dates", violation_codes(payload))

    def test_each_required_row_violation_fails_the_gate(self):
        cases = {}

        forbidden = single_row()
        forbidden.update(play="SCORE", market_type="score", selection="1-0")
        forbidden["bet_id"] = stable_bet_id(forbidden)
        cases["forbidden_play"] = [forbidden]

        cases["parlay_leg_count"] = [parlay_row(match_ids=("p1", "p2", "p3"))]

        non_domestic = single_row(source="professional")
        cases["non_domestic_odds"] = [non_domestic]

        cases["nonpositive_configured_ev"] = [single_row(expected_value=0)]
        cases["stake_unit"] = [single_row(stake=3)]

        exposure_a = single_row(match_id="same", stake=102)
        exposure_b = single_row(
            match_id="same", market_type="ttg", play="TTG", selection="2球", stake=102
        )
        cases["match_exposure"] = [exposure_a, exposure_b]

        cases["parlay_stake"] = [parlay_row(stake=32)]

        daily_rows = [single_row(match_id=f"daily-{index}", stake=100) for index in range(6)]
        cases["daily_stake"] = daily_rows

        duplicate = single_row()
        cases["duplicate_bet_id"] = [duplicate, deepcopy(duplicate)]

        for expected_code, rows in cases.items():
            with self.subTest(expected_code=expected_code):
                payload = audit_generated_portfolios({"2026-07-11": rows}, config())
                self.assertFalse(payload["passed"])
                self.assertIn(expected_code, violation_codes(payload))

    def test_non_exact_parlays_and_non_domestic_legs_fail(self):
        one_leg = parlay_row(match_ids=("only",))
        bad_leg_source = parlay_row()
        legs = json.loads(bad_leg_source["legs_json"])
        legs[1]["odds_source"] = "professional"
        bad_leg_source["legs_json"] = json.dumps(legs, ensure_ascii=False, sort_keys=True)

        one_leg_result = audit_generated_portfolios(
            {"2026-07-11": [one_leg]}, config()
        )
        source_result = audit_generated_portfolios(
            {"2026-07-11": [bad_leg_source]}, config()
        )

        self.assertIn("parlay_leg_count", violation_codes(one_leg_result))
        self.assertIn("non_domestic_odds", violation_codes(source_result))

    def test_invalid_paid_market_identity_is_detected_before_maxima(self):
        row = single_row(selection="not-a-had-selection")
        row["bet_id"] = stable_bet_id(row)

        payload = audit_generated_portfolios({"2026-07-11": [row]}, config())

        self.assertIn("invalid_market_identity", violation_codes(payload))
        self.assertEqual(0, payload["maxima"]["match_exposure"])

    def test_monthly_stake_over_cap_fails(self):
        portfolios = {}
        for offset in range(11):
            report_date = (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            portfolios[report_date] = [
                single_row(report_date, match_id=f"m-{offset}-{index}", stake=100)
                for index in range(5)
            ]

        payload = audit_generated_portfolios(portfolios, config())

        self.assertIn("monthly_stake", violation_codes(payload))
        self.assertEqual(5500, payload["maxima"]["monthly_stake"])


class RepositoryAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data" / "odds_snapshots").mkdir(parents=True)
        (self.root / "output").mkdir()
        (self.root / "betting_config.json").write_text(
            json.dumps(config()), encoding="utf-8"
        )
        with (self.root / "data" / "fixtures.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=["date", "match_id"])
            writer.writeheader()
            for report_date in ("2026-07-11", "2026-07-12", "2026-07-13"):
                writer.writerow({"date": report_date, "match_id": f"match-{report_date}"})

    def tearDown(self):
        self.temp.cleanup()

    def _write_common_evidence(self, report_date):
        match_id = f"match-{report_date}"
        (self.root / "output" / f"predictions_{report_date}.csv").write_text(
            f"date,match_id\n{report_date},{match_id}\n", encoding="utf-8"
        )
        (self.root / "data" / f"sporttery_odds_{report_date}.json").write_text(
            json.dumps({match_id: {"had": {"h": "2.00", "d": "3.20", "a": "3.50"}}}),
            encoding="utf-8",
        )

    def _write_snapshot(self, report_date, *, source="sporttery", valid=True):
        match_id = f"match-{report_date}"
        payload = {
            "target_date": report_date,
            "captured_at": f"{report_date}T12:00:00+08:00",
            "capture_phase": "decision",
            "source": source,
            "matches": [
                {
                    "match_id": match_id,
                    "team_a": "Home",
                    "team_b": "Away",
                    "kickoff_at": f"{report_date}T18:00:00+08:00",
                    "markets": {
                        "had": {"h": "2.00", "d": "3.20", "a": "3.50"},
                        "hhad": {},
                        "ttg": {},
                    },
                    "single_eligibility": {"had": True, "hhad": False, "ttg": False},
                }
            ],
        }
        if not valid:
            del payload["matches"][0]["match_id"]
        path = (
            self.root
            / "data"
            / "odds_snapshots"
            / f"{report_date}-120000-decision.json"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_repository_audit_checks_and_classifies_dates_deterministically(self):
        for report_date in ("2026-07-11", "2026-07-12", "2026-07-13"):
            self._write_common_evidence(report_date)
        self._write_snapshot("2026-07-11")
        self._write_snapshot("2026-07-13", valid=False)
        calls = []

        def builder(target_date, *, locked_at):
            calls.append((target_date, locked_at))
            return [single_row(target_date.isoformat(), match_id=f"match-{target_date}")], []

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 13),
            plan_builder=builder,
        )

        self.assertTrue(payload["passed"])
        self.assertEqual(["2026-07-11"], payload["checked_dates"])
        self.assertEqual(["2026-07-12"], payload["excluded_missing"])
        self.assertEqual(["2026-07-13"], payload["excluded_invalid"])
        self.assertEqual(
            [(date(2026, 7, 11), datetime(2026, 7, 11, 12, tzinfo=BEIJING))],
            calls,
        )
        coverage = {item["date"]: item for item in payload["source_coverage"]}
        self.assertEqual("checked", coverage["2026-07-11"]["status"])
        self.assertTrue(coverage["2026-07-11"]["sporttery"])
        self.assertEqual("excluded_missing", coverage["2026-07-12"]["status"])
        self.assertEqual("excluded_invalid", coverage["2026-07-13"]["status"])
        validate_audit_payload(payload)
        persisted = json.loads(
            (self.root / "output" / "shadow_portfolio_activation_audit.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(payload, persisted)

    def test_verified_domestic_fallback_is_reported_separately(self):
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11", source="zgzcw")

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, locked_at: (
                [single_row(source="zgzcw")],
                [],
            ),
        )

        coverage = payload["source_coverage"][0]
        self.assertFalse(coverage["sporttery"])
        self.assertTrue(coverage["verified_domestic_fallback"])

    def test_audit_in_active_mode_preserves_historical_plan_lock_and_ledger_bytes(self):
        (self.root / "betting_config.json").write_text(
            json.dumps(config(mode="active")), encoding="utf-8"
        )
        self._write_common_evidence("2026-07-11")
        self._write_snapshot("2026-07-11")
        protected = {
            self.root / "output" / "betting_plan_2026-07-10.csv": b"old-plan\n",
            self.root / "output" / "plan_lock_2026-07-10.json": b'{"locked":true}\n',
            self.root / "output" / "betting_ledger.csv": (
                b"strategy_version,locked_odds,stake,bet_id\n"
                b"legacy-v3,2.10,20,old-id\n"
            ),
        }
        for path, content in protected.items():
            path.write_bytes(content)

        payload = run_audit(
            self.root,
            date(2026, 7, 11),
            date(2026, 7, 11),
            plan_builder=lambda target_date, locked_at: ([single_row()], []),
        )

        self.assertTrue(payload["historical_artifacts_unchanged"])
        for path, content in protected.items():
            self.assertEqual(content, path.read_bytes())

    def test_schema_rejects_passed_payload_without_checked_dates(self):
        payload = audit_generated_portfolios({}, config())
        payload["passed"] = True

        with self.assertRaises(ValueError):
            validate_audit_payload(payload)

    def test_repository_activation_is_simulation_only(self):
        root = Path(__file__).resolve().parents[1]
        repository_config = json.loads(
            (root / "betting_config.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            "active", repository_config["value_strategy"]["activation_mode"]
        )
        self.assertEqual("simulation", repository_config["simulation_account"]["mode"])
        self.assertIs(
            False,
            repository_config["simulation_account"]["real_money_automation"],
        )


if __name__ == "__main__":
    unittest.main()
