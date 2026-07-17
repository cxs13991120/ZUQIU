import csv
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import generate_betting_plan as strategy
from official_markets import normalize_market
from value_candidates import ValueCandidate


TARGET_DATE = date(2026, 7, 18)
BEIJING = timezone(timedelta(hours=8))
LOCKED_AT = datetime(2026, 7, 18, 13, 30, tzinfo=BEIJING)
CAPTURED_AT = "2026-07-18T13:20:00+08:00"
KICKOFF_AT = "2026-07-18T20:00:00+08:00"


def value_config(mode: str = "shadow") -> dict:
    return {
        "strategy_version": "value-v4",
        "max_daily_budget": 500,
        "value_strategy": {
            "activation_mode": mode,
            "strict_until_samples": 100,
            "settled_samples": 0,
            "strict_min_probability_edge": 0.01,
            "min_probability_edge": 0.01,
            "strict_min_ev": 0.06,
            "min_ev": 0.03,
            "strict_model_edge_weight_base": 1.0,
            "strict_model_edge_weight_max": 1.0,
            "model_edge_weight_base": 1.0,
            "model_edge_weight_max": 1.0,
            "strict_min_combo_leg_edge": 0.02,
            "min_combo_leg_edge": 0.01,
            "strict_min_combo_leg_ev": 0.02,
            "min_combo_leg_ev": 0.01,
            "strict_min_combo_ev": 0.10,
            "min_combo_ev": 0.03,
            "strict_kelly_fraction": 0.25,
            "kelly_fraction": 0.25,
            "reference_bankroll": 5000,
            "stake_unit": 2,
            "max_match_exposure": 200,
            "max_single_count": 2,
            "combo_min_legs": 2,
            "combo_max_legs": 2,
            "max_daily_combo_stake": 30,
            "min_combo_leg_probability": 0.10,
            "observation_count": 20,
            "calibration_prior": 100,
        },
        "league_calibration": {
            "min_samples": 30,
            "prior_samples": 60,
            "max_adjustment": 0.05,
            "validation_fraction": 0.25,
        },
        "simulation_account": {
            "mode": "simulation",
            "required_settled_days": 30,
            "monthly_budget_cap": 5000,
            "monthly_stop_loss": 5000,
            "real_money_automation": False,
        },
        "learning_policy": {
            "case_study_policy": "regression_only",
            "minimum_rule_samples": 30,
        },
    }


def prediction(match_id: str) -> dict:
    return {
        "date": TARGET_DATE.isoformat(),
        "match_id": match_id,
        "stage": "Test League",
        "team_a": f"Home {match_id}",
        "team_b": f"Away {match_id}",
        "kickoff_at": KICKOFF_AT,
        "p_a": "0.70",
        "p_draw": "0.20",
        "p_b": "0.10",
        "xg_a": "2.00",
        "xg_b": "0.50",
    }


def market_fixture(match_id: str, market_type: str):
    prices = {
        "had": {"h": "3.00", "d": "3.00", "a": "3.00"},
        "hhad": {"h": "3.00", "d": "3.00", "a": "3.00", "goalLine": "+1"},
        "ttg": {f"s{index}": "8.00" for index in range(8)},
    }[market_type]
    raw = {
        **prices,
        "source": "sporttery",
        "source_record_id": f"decision-{match_id}-{market_type}",
        "captured_at_bjt": CAPTURED_AT,
    }
    market = normalize_market(match_id, market_type, raw)
    assert market is not None
    snapshot = {
        "target_date": TARGET_DATE.isoformat(),
        "capture_phase": "decision",
        "captured_at": CAPTURED_AT,
        "source": "sporttery",
        "matches": [{
            **prediction(match_id),
            "markets": {market_type: prices},
            "single_eligibility": {"had": True, "hhad": True, "ttg": True},
        }],
    }
    return {match_id: {market_type: market}}, snapshot


def candidate(match_id: str, *, market_type: str = "had", play: str | None = None) -> ValueCandidate:
    line = 1 if market_type == "hhad" else None
    selection = "2球" if market_type == "ttg" else "胜"
    return ValueCandidate(
        candidate_id=f"{match_id}:{market_type}:{selection}",
        date=TARGET_DATE.isoformat(),
        match_id=match_id,
        stage="Test League",
        team_a=f"Home {match_id}",
        team_b=f"Away {match_id}",
        kickoff_at=KICKOFF_AT,
        market_type=market_type,
        play=play or market_type.upper(),
        selection=selection,
        line=line,
        official_odds=3.0,
        official_market_probability=1 / 3,
        raw_model_probability=0.60,
        calibrated_model_probability=0.60,
        conservative_probability=0.60,
        probability_edge=0.60 - 1 / 3,
        expected_value=0.80,
        single_eligible=True,
        data_quality="medium",
        data_quality_multiplier=0.60,
        volatility_band="stable",
        volatility_multiplier=1.0,
        odds_source="sporttery",
        source_record_id=f"decision-{match_id}",
        captured_at_bjt=CAPTURED_AT,
        correlation_tags=(f"match:{match_id}",),
        paid_eligible=True,
        value_gate_reasons=(),
        calibration_samples=0,
    )


class ValueV4PlanIntegrationTest(unittest.TestCase):
    def run_v4(self, market_type: str):
        markets, snapshot = market_fixture(f"match-{market_type}", market_type)
        row = prediction(f"match-{market_type}")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(strategy, "ROOT", root),
                patch.object(strategy, "OUTPUT_DIR", root / "output"),
                patch.object(strategy, "DATA_DIR", root / "data"),
                patch.object(strategy, "read_json", return_value=value_config()),
                patch.object(strategy, "load_predictions", return_value=[row]),
                patch.object(strategy, "load_value_snapshot", return_value=snapshot, create=True),
                patch.object(strategy, "load_official_decision_markets", return_value=markets, create=True),
                patch.object(strategy, "load_draw_training_samples", return_value=[]),
            ):
                return strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)

    def test_had_hhad_and_ttg_can_each_independently_qualify(self):
        for market_type in ("had", "hhad", "ttg"):
            with self.subTest(market_type=market_type):
                plan, observations = self.run_v4(market_type)
                self.assertEqual([market_type], [row["market_type"] for row in plan])
                self.assertTrue(observations)
                self.assertTrue(all(float(row["stake"]) == 0 for row in observations))

    def test_unsupported_play_never_enters_plan_and_is_audited(self):
        invalid = replace(candidate("bad"), play="SCORE")
        with self.strategy_context(value_config()):
            with patch.object(strategy, "build_candidates", return_value=[invalid], create=True):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.shadow_plan)
        self.assertTrue(any("unsupported_play" in reason for reason in outputs.audit["rejection_reasons"]))

    def test_shadow_and_active_modes_route_only_the_selected_strategy(self):
        for mode, active_version, shadow_count in (("shadow", "legacy-v3", 1), ("active", "value-v4", 0)):
            with self.subTest(mode=mode), self.strategy_context(value_config(mode)):
                with (
                    patch.object(strategy, "build_legacy_value_plan", return_value=([{"strategy_version": "legacy-v3", "stake": 10}], []), create=True),
                    patch.object(strategy, "build_value_v4_plan", return_value=([{"strategy_version": "value-v4", "stake": 20}], [{"strategy_version": "value-v4", "stake": 0}]), create=True),
                ):
                    outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

            self.assertEqual(active_version, outputs.active_plan[0]["strategy_version"])
            self.assertEqual(shadow_count, len(outputs.shadow_plan))
            self.assertTrue(all(row["strategy_version"] == "value-v4" for row in outputs.observations))

    def test_locked_rerun_preserves_v4_odds_and_bet_ids(self):
        row = candidate("locked")
        with self.strategy_context(value_config()):
            with patch.object(strategy, "build_candidates", return_value=[row], create=True):
                first, _ = strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)
                second, _ = strategy.build_value_v4_plan(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual(
            [(item["bet_id"], item["locked_odds"]) for item in first],
            [(item["bet_id"], item["locked_odds"]) for item in second],
        )

    def test_invalid_activation_mode_fails_closed(self):
        with self.strategy_context(value_config("paper")):
            with self.assertRaises(ValueError):
                strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

    def test_zero_candidates_make_valid_no_bet_outputs_and_zero_paid_stake(self):
        with self.strategy_context(value_config()):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], []), create=True),
                patch.object(strategy, "build_candidates", return_value=[], create=True),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        self.assertEqual([], outputs.active_plan)
        self.assertEqual([], outputs.shadow_plan)
        self.assertEqual(0, outputs.audit["comparison"]["active_paid_stake"])
        self.assertEqual(0, outputs.audit["comparison"]["shadow_paid_stake"])

    def test_allocator_limits_survive_daily_integration(self):
        candidates = [candidate(f"m{index}") for index in range(6)]
        with self.strategy_context(value_config()):
            with (
                patch.object(strategy, "build_legacy_value_plan", return_value=([], []), create=True),
                patch.object(strategy, "build_candidates", return_value=candidates, create=True),
            ):
                outputs = strategy.build_strategy_outputs(TARGET_DATE, locked_at=LOCKED_AT)

        selected = outputs.shadow_plan
        self.assertLessEqual(sum(int(row["stake"]) for row in selected), 500)
        self.assertLessEqual(len([row for row in selected if row["market_type"] != "parlay"]), 2)
        self.assertLessEqual(sum(int(row["stake"]) for row in selected if row["market_type"] == "parlay"), 30)
        self.assertTrue(all(check["passed"] for check in outputs.audit["risk_checks"]))
        self.assertEqual(200, outputs.audit["risk_caps"]["max_match_exposure"])
        self.assertEqual(5000, outputs.audit["risk_caps"]["monthly_budget_cap"])

    def strategy_context(self, config):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        snapshot = {
            "target_date": TARGET_DATE.isoformat(),
            "capture_phase": "decision",
            "captured_at": CAPTURED_AT,
            "source": "sporttery",
            "matches": [],
        }
        return _Patches(
            patch.object(strategy, "ROOT", root),
            patch.object(strategy, "OUTPUT_DIR", root / "output"),
            patch.object(strategy, "DATA_DIR", root / "data"),
            patch.object(strategy, "read_json", return_value=deepcopy(config)),
            patch.object(strategy, "load_predictions", return_value=[]),
            patch.object(strategy, "load_value_snapshot", return_value=snapshot, create=True),
            patch.object(strategy, "load_official_decision_markets", return_value={}, create=True),
            patch.object(strategy, "load_draw_training_samples", return_value=[]),
        )


class _Patches:
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        for item in reversed(self.patches):
            item.stop()


if __name__ == "__main__":
    unittest.main()
