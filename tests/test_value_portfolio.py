import math
import unittest
from dataclasses import replace

from value_candidates import ValueCandidate
from value_portfolio import (
    PortfolioLimits,
    allocate_portfolio,
    build_two_leg_candidates,
    full_kelly,
    stake_for,
)


class KellyTest(unittest.TestCase):
    def test_quarter_kelly_is_applied_before_quality_multipliers(self):
        candidate = _candidate(
            conservative_probability=0.60,
            official_odds=2.00,
            data_quality_multiplier=0.60,
            volatility_multiplier=0.75,
        )
        self.assertEqual(112, stake_for(candidate, 5000, 0.25))

    def test_nonpositive_edge_has_zero_stake(self):
        self.assertEqual(0, stake_for(_candidate(conservative_probability=0.40), 5000, 0.25))

    def test_stake_rounds_down_to_two_yuan_and_small_result_is_zero(self):
        self.assertEqual(174, stake_for(_candidate(conservative_probability=0.57), 5000, 0.25))
        self.assertEqual(0, stake_for(_candidate(conservative_probability=0.51), 10, 0.25))

    def test_invalid_kelly_inputs_fail_closed(self):
        candidate = _candidate()
        for probability, odds in ((0, 2), (1, 2), (0.5, 1), (math.nan, 2), (0.5, math.inf)):
            with self.subTest(probability=probability, odds=odds):
                self.assertEqual(0.0, full_kelly(probability, odds))
        for invalid in (0, -1, math.nan, math.inf):
            with self.subTest(invalid=invalid):
                self.assertEqual(0, stake_for(candidate, invalid, 0.25))
                self.assertEqual(0, stake_for(candidate, 5000, invalid))
        self.assertEqual(0, stake_for(replace(candidate, data_quality_multiplier=math.nan), 5000, 0.25))


class PortfolioAllocationTest(unittest.TestCase):
    def test_strict_and_normal_both_use_quarter_kelly_but_strict_caps_exposure(self):
        candidate = _candidate(conservative_probability=0.70, official_odds=2.00)
        normal = _limits(max_single_stake=200, single_budget_cap=200)
        strict = _limits(max_single_stake=50, single_budget_cap=100)

        normal_portfolio = allocate_portfolio([candidate], normal, _account())
        strict_portfolio = allocate_portfolio([candidate], strict, _account())

        self.assertEqual(0.25, normal.kelly_fraction)
        self.assertEqual(0.25, strict.kelly_fraction)
        self.assertEqual(200, normal_portfolio.total_stake)
        self.assertEqual(50, strict_portfolio.total_stake)

    def test_single_limits_are_applied_together_in_deterministic_rank_order(self):
        candidates = [
            _candidate(candidate_id="z", match_id="m1", conservative_probability=0.70),
            _candidate(candidate_id="a", match_id="m1", conservative_probability=0.65),
            _candidate(candidate_id="b", match_id="m2", conservative_probability=0.66),
            _candidate(candidate_id="c", match_id="m3", conservative_probability=0.64),
        ]
        portfolio = allocate_portfolio(
            candidates, _limits(max_single_stake=100, single_budget_cap=300), _account()
        )

        self.assertEqual(("z", "b"), tuple(item.candidate.candidate_id for item in portfolio.singles))
        self.assertEqual(2, len(portfolio.singles))
        self.assertLessEqual(sum(item.stake for item in portfolio.singles), 200)
        self.assertLessEqual(sum(item.stake for item in portfolio.singles if item.candidate.match_id == "m1"), 200)

    def test_monthly_budget_truncates_to_unit_and_realized_loss_stops(self):
        candidate = _candidate(conservative_probability=0.70)
        portfolio = allocate_portfolio([candidate], _limits(), _account(monthly_stake=4995))
        self.assertEqual(4, portfolio.total_stake)
        self.assertEqual(0, allocate_portfolio([candidate], _limits(), _account(monthly_realized_profit=-5000)).total_stake)

    def test_callers_cannot_raise_global_hard_limits(self):
        candidates = [
            _candidate(candidate_id=f"single-{index}", match_id=f"match-{index}", conservative_probability=0.80)
            for index in range(4)
        ]
        oversized = _limits(
            max_match_exposure=1000,
            max_single_stake=1000,
            single_budget_cap=1000,
            max_single_count=10,
            max_parlay_stake=300,
            max_daily_stake=1000,
            monthly_budget_cap=10000,
            monthly_stop_loss=10000,
        )

        portfolio = allocate_portfolio(candidates, oversized, _account())

        self.assertLessEqual(len(portfolio.singles), 2)
        self.assertTrue(all(item.stake <= 200 for item in portfolio.singles))
        self.assertLessEqual(portfolio.total_stake, 500)
        monthly_limited = allocate_portfolio(
            [candidates[0]], oversized, _account(monthly_stake=4995)
        )
        self.assertEqual(4, monthly_limited.total_stake)
        stopped = allocate_portfolio(
            [candidates[0]], oversized, _account(monthly_realized_profit=-5000)
        )
        self.assertEqual(0, stopped.total_stake)

    def test_market_type_and_play_must_describe_the_same_supported_market(self):
        disguised_score = _candidate(play="SCORE")
        portfolio = allocate_portfolio([disguised_score], _limits(), _account())
        self.assertEqual(0, portfolio.total_stake)
        self.assertIn("candidate:unsupported_play", portfolio.rejections)

    def test_pending_monthly_stake_consumes_budget_but_profit_alias_is_supported(self):
        candidate = _candidate(conservative_probability=0.70)
        pending = allocate_portfolio([candidate], _limits(), _account(monthly_stake=4998, monthly_realized_profit=0))
        alias = allocate_portfolio([candidate], _limits(), {"monthly_stake": 4998, "monthly_profit": 0})
        self.assertEqual(2, pending.total_stake)
        self.assertEqual(pending.total_stake, alias.total_stake)

    def test_invalid_accounts_and_nonpositive_value_do_not_force_bets(self):
        candidate = _candidate(expected_value=0.0)
        self.assertEqual(0, allocate_portfolio([candidate], _limits(), _account()).total_stake)
        for account in ({}, {"monthly_stake": -1, "monthly_realized_profit": 0}, {"monthly_stake": 0, "monthly_realized_profit": math.nan}):
            with self.subTest(account=account):
                self.assertEqual(0, allocate_portfolio([_candidate()], _limits(), account).total_stake)

    def test_malformed_candidate_values_fail_closed_without_raising(self):
        malformed = replace(_candidate(), expected_value=math.nan)
        portfolio = allocate_portfolio([malformed], _limits(), _account())
        self.assertEqual(0, portfolio.total_stake)
        self.assertIn("candidate:invalid_candidate_values", portfolio.rejections)

    def test_input_order_and_duplicate_ids_cannot_change_or_duplicate_selection(self):
        candidates = [
            _candidate(candidate_id="b", match_id="m2", conservative_probability=0.62),
            _candidate(candidate_id="a", match_id="m1", conservative_probability=0.62),
            _candidate(candidate_id="a", match_id="m3", conservative_probability=0.80),
        ]
        first = allocate_portfolio(candidates, _limits(), _account())
        second = allocate_portfolio(list(reversed(candidates)), _limits(), _account())
        first_rows = tuple((item.candidate.candidate_id, item.candidate.match_id, item.stake) for item in first.singles)
        second_rows = tuple((item.candidate.candidate_id, item.candidate.match_id, item.stake) for item in second.singles)
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(len({item.candidate.candidate_id for item in first.singles}), len(first.singles))


class ParlayTest(unittest.TestCase):
    def test_missing_combo_gate_configuration_fails_closed(self):
        self.assertEqual([], build_two_leg_candidates([_candidate()], {}))

    def test_single_ineligible_candidate_can_be_legal_two_leg_parlay(self):
        left = _candidate(candidate_id="left", match_id="left", single_eligible=False, conservative_probability=0.70)
        right = _candidate(candidate_id="right", match_id="right", single_eligible=False, conservative_probability=0.68)
        parlays = build_two_leg_candidates([left, right], _combo_config())
        self.assertEqual(1, len(parlays))
        self.assertEqual(("left", "right"), parlays[0].match_ids)
        self.assertGreater(parlays[0].expected_value, 0)

    def test_parlay_pairs_require_distinct_uncorrelated_eligible_legs(self):
        left = _candidate(candidate_id="left", match_id="same", correlation_tags=("league:x", "team:left"))
        same_match = _candidate(candidate_id="same", match_id="same", correlation_tags=("league:x", "team:left"))
        correlated = _candidate(candidate_id="correlated", match_id="other", correlation_tags=("league:y", "team:left"))
        bad_quality = _candidate(candidate_id="bad", match_id="third", data_quality="low")
        self.assertEqual([], build_two_leg_candidates([left, same_match, correlated, bad_quality], _combo_config()))

    def test_allocator_selects_only_one_two_leg_parlay_within_all_caps(self):
        candidates = [
            _candidate(candidate_id="a", match_id="a", single_eligible=False, conservative_probability=0.70),
            _candidate(candidate_id="b", match_id="b", single_eligible=False, conservative_probability=0.68),
            _candidate(candidate_id="c", match_id="c", single_eligible=False, conservative_probability=0.67),
        ]
        portfolio = allocate_portfolio(candidates, _limits(), _account(), config=_combo_config())
        self.assertIsNotNone(portfolio.parlay)
        self.assertEqual(2, len(portfolio.parlay.parlay.legs))
        self.assertLessEqual(portfolio.parlay.stake, 30)
        self.assertLessEqual(portfolio.total_stake, 500)

    def test_parlay_uses_the_minimum_of_each_multiplier_type_across_legs(self):
        left = _candidate(
            candidate_id="left",
            match_id="left",
            single_eligible=False,
            conservative_probability=0.70,
            data_quality_multiplier=0.60,
            volatility_multiplier=1.0,
            performance_multiplier=1.0,
        )
        right = _candidate(
            candidate_id="right",
            match_id="right",
            single_eligible=False,
            conservative_probability=0.68,
            data_quality_multiplier=1.0,
            volatility_multiplier=0.75,
            performance_multiplier=0.90,
        )
        limits = _limits(bankroll=900, max_parlay_stake=300)
        portfolio = allocate_portfolio([left, right], limits, _account(), config=_combo_config())
        self.assertEqual(26, portfolio.parlay.stake)

    def test_callers_cannot_raise_the_thirty_yuan_parlay_cap(self):
        left = _candidate(
            candidate_id="left", match_id="left", single_eligible=False,
            conservative_probability=0.70,
        )
        right = _candidate(
            candidate_id="right", match_id="right", single_eligible=False,
            conservative_probability=0.68,
        )
        portfolio = allocate_portfolio(
            [left, right], _limits(max_parlay_stake=300), _account(), config=_combo_config()
        )
        self.assertEqual(30, portfolio.parlay.stake)

    def test_selected_single_match_cannot_reappear_in_parlay(self):
        single = _candidate(candidate_id="single", match_id="a", conservative_probability=0.72)
        other = _candidate(candidate_id="other", match_id="b", single_eligible=False, conservative_probability=0.70)
        portfolio = allocate_portfolio([single, other], _limits(), _account(), config=_combo_config())
        self.assertEqual(("single",), tuple(item.candidate.candidate_id for item in portfolio.singles))
        self.assertIsNone(portfolio.parlay)


def _candidate(**overrides) -> ValueCandidate:
    values = {
        "candidate_id": "candidate",
        "date": "2026-07-18",
        "match_id": "match",
        "stage": "League",
        "team_a": "Home",
        "team_b": "Away",
        "kickoff_at": "2026-07-18T20:00:00+08:00",
        "market_type": "had",
        "play": "HAD",
        "selection": "home",
        "line": None,
        "official_odds": 2.0,
        "official_market_probability": 0.5,
        "raw_model_probability": 0.60,
        "calibrated_model_probability": 0.60,
        "conservative_probability": 0.60,
        "probability_edge": 0.10,
        "expected_value": 0.20,
        "single_eligible": True,
        "data_quality": "high",
        "data_quality_multiplier": 1.0,
        "volatility_band": "stable",
        "volatility_multiplier": 1.0,
        "odds_source": "sporttery",
        "source_record_id": "record",
        "captured_at_bjt": "2026-07-17T12:00:00+08:00",
        "correlation_tags": (),
        "paid_eligible": True,
        "value_gate_reasons": (),
        "calibration_samples": 100,
        "performance_multiplier": 1.0,
    }
    values.update(overrides)
    return ValueCandidate(**values)


def _limits(**overrides) -> PortfolioLimits:
    values = {
        "bankroll": 5000,
        "kelly_fraction": 0.25,
        "stake_unit": 2,
        "max_match_exposure": 200,
        "max_single_stake": 200,
        "single_budget_cap": 200,
        "max_single_count": 2,
        "min_single_stake": 2,
        "max_parlay_stake": 30,
        "min_parlay_stake": 2,
        "max_daily_stake": 500,
        "monthly_budget_cap": 5000,
        "monthly_stop_loss": 5000,
    }
    values.update(overrides)
    return PortfolioLimits(**values)


def _account(**overrides) -> dict:
    values = {"monthly_stake": 0, "monthly_realized_profit": 0}
    values.update(overrides)
    return values


def _combo_config() -> dict:
    return {
        "value_strategy": {
            "settled_samples": 100,
            "strict_until_samples": 100,
            "min_combo_leg_probability": 0.45,
            "strict_min_combo_leg_edge": 0.02,
            "min_combo_leg_edge": 0.02,
            "strict_min_combo_leg_expected_return": 1.01,
            "min_combo_leg_expected_return": 1.01,
            "strict_min_combo_expected_return": 1.03,
            "min_combo_expected_return": 1.03,
        }
    }


if __name__ == "__main__":
    unittest.main()
