import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

from official_markets import THREE_WAY_SELECTIONS, normalize_market
from value_candidates import (
    ValueCandidate,
    build_candidates,
    conservative_probability,
    odds_volatility,
)


class ValueCandidateTest(unittest.TestCase):
    def test_had_preserves_probability_layers_and_draw_calibration(self):
        candidates = build_candidates(
            [_prediction()],
            _official_odds(),
            _snapshot(single_had=False),
            _config(),
            {"Test League": {"enabled": True, "adjustment": 0.05, "sample_count": 40}},
        )

        by_selection = {
            candidate.selection: candidate
            for candidate in candidates
            if candidate.market_type == "had"
        }
        home = by_selection["胜"]
        draw = by_selection["平"]
        self.assertEqual(0.60, home.raw_model_probability)
        self.assertEqual(0.60, home.calibrated_model_probability)
        self.assertEqual(0.25, draw.raw_model_probability)
        self.assertEqual(0.30, draw.calibrated_model_probability)
        self.assertEqual(40, draw.calibration_samples)
        self.assertNotEqual(draw.raw_model_probability, draw.conservative_probability)
        self.assertAlmostEqual(1.0, sum(
            candidate.official_market_probability
            for candidate in candidates
            if candidate.market_type == "had"
        ))
        self.assertAlmostEqual(
            draw.conservative_probability * draw.official_odds - 1,
            draw.expected_value,
        )
        self.assertFalse(draw.single_eligible)
        self.assertTrue(draw.paid_eligible)
        self.assertEqual((), draw.value_gate_reasons)
        self.assertEqual(1.0, draw.performance_multiplier)
        with self.assertRaises(FrozenInstanceError):
            draw.paid_eligible = False

    def test_hhad_uses_poisson_probabilities_and_official_integer_line(self):
        candidates = build_candidates(
            [_prediction()], _official_odds(), _snapshot(), _config(), {}
        )

        handicap = [candidate for candidate in candidates if candidate.market_type == "hhad"]
        self.assertEqual(3, len(handicap))
        self.assertEqual({1}, {candidate.line for candidate in handicap})
        self.assertAlmostEqual(1.0, sum(candidate.raw_model_probability for candidate in handicap), places=9)
        self.assertAlmostEqual(1.0, sum(candidate.official_market_probability for candidate in handicap))

    def test_ttg_uses_poisson_totals_and_each_market_is_devigged_separately(self):
        candidates = build_candidates(
            [_prediction()], _official_odds(), _snapshot(), _config(), {}
        )

        totals = [candidate for candidate in candidates if candidate.market_type == "ttg"]
        self.assertEqual(8, len(totals))
        self.assertAlmostEqual(1.0, sum(candidate.raw_model_probability for candidate in totals), places=9)
        self.assertAlmostEqual(1.0, sum(candidate.official_market_probability for candidate in totals))
        had_home = next(candidate for candidate in candidates if candidate.market_type == "had" and candidate.selection == "胜")
        handicap_home = next(candidate for candidate in candidates if candidate.market_type == "hhad" and candidate.selection == "胜")
        self.assertNotEqual(had_home.official_market_probability, handicap_home.official_market_probability)

    def test_value_gate_is_independent_from_official_single_eligibility(self):
        candidates = build_candidates(
            [_prediction()], _official_odds(), _snapshot(single_had=False), _config(), {}
        )

        home = next(candidate for candidate in candidates if candidate.market_type == "had" and candidate.selection == "胜")
        self.assertFalse(home.single_eligible)
        self.assertTrue(home.paid_eligible)
        self.assertEqual((), home.value_gate_reasons)

    def test_candidate_builder_excludes_bad_or_started_identity_and_market_inputs(self):
        cases = (
            ({}, _snapshot()),
            (_official_odds(), _snapshot(match_id="other")),
            (_official_odds(), _snapshot(team_a="Other Home")),
            (_official_odds(), _snapshot(kickoff_at="2026-07-17T10:00:00+08:00")),
            ({"match-1": {"unsupported": object()}}, _snapshot()),
        )
        for odds_by_match, snapshot in cases:
            with self.subTest(odds_by_match=odds_by_match, snapshot=snapshot):
                self.assertEqual(
                    [],
                    build_candidates([_prediction()], odds_by_match, snapshot, _config(), {}),
                )

    def test_early_discards_emit_deterministic_structured_diagnostics(self):
        identity_diagnostics = []
        self.assertEqual([], build_candidates(
            [_prediction()],
            _official_odds(),
            _snapshot(team_a="Other Home"),
            _config(),
            {},
            diagnostics=identity_diagnostics,
        ))
        self.assertEqual([{
            "code": "prediction_identity_mismatch",
            "context": {"match_id": "match-1", "prediction_index": 0},
        }], identity_diagnostics)

        low_quality = _official_odds()
        low_quality["match-1"] = {
            "had": replace(
                low_quality["match-1"]["had"], source="external-consensus"
            )
        }
        quality_diagnostics = []
        self.assertEqual([], build_candidates(
            [_prediction()], low_quality, _snapshot(), _config(), {},
            diagnostics=quality_diagnostics,
        ))
        self.assertEqual([{
            "code": "market_data_quality_low",
            "context": {"match_id": "match-1", "market_type": "had"},
        }], quality_diagnostics)

        volatile_snapshot = _snapshot()
        volatile_snapshot["opening"] = _opening_snapshot()
        volatile_snapshot["opening"]["matches"][0]["markets"]["had"]["h"] = 4.50
        volatile_odds = {"match-1": {"had": _official_odds()["match-1"]["had"]}}
        volatility_diagnostics = []
        candidates = build_candidates(
            [_prediction()], volatile_odds, volatile_snapshot, _config(), {},
            diagnostics=volatility_diagnostics,
        )
        self.assertEqual(2, len(candidates))
        self.assertEqual([{
            "code": "odds_volatility_ineligible",
            "context": {
                "match_id": "match-1",
                "market_type": "had",
                "selection": THREE_WAY_SELECTIONS["h"],
                "volatility_band": "unverified_jump",
            },
        }], volatility_diagnostics)

        malformed_diagnostics = []
        self.assertEqual([], build_candidates(
            [_prediction()],
            {"match-1": {"had": object()}},
            _snapshot(),
            _config(),
            {},
            diagnostics=malformed_diagnostics,
        ))
        self.assertEqual([{
            "code": "official_market_invalid",
            "context": {"match_id": "match-1", "market_type": "had"},
        }], malformed_diagnostics)

    def test_external_consensus_never_replaces_domestic_official_odds(self):
        prediction = _prediction()
        prediction["external_consensus_odds"] = {"had": {"胜": 1.01}}
        candidates = build_candidates(
            [prediction], _official_odds(), _snapshot(), _config(), {}
        )

        home = next(candidate for candidate in candidates if candidate.market_type == "had" and candidate.selection == "胜")
        self.assertEqual(2.20, home.official_odds)
        self.assertEqual("sporttery", home.odds_source)

    def test_non_domestic_market_is_excluded_even_when_its_prices_match(self):
        odds_by_match = _official_odds()
        odds_by_match["match-1"]["had"] = replace(
            odds_by_match["match-1"]["had"], source="external-consensus"
        )

        candidates = build_candidates(
            [_prediction()], odds_by_match, _snapshot(), _config(), {}
        )

        self.assertFalse(any(candidate.market_type == "had" for candidate in candidates))

    def test_mismatched_opening_snapshot_cannot_upgrade_data_quality(self):
        snapshot = _snapshot()
        opening = dict(snapshot["matches"][0])
        opening["team_a"] = "Other Home"
        snapshot["opening_matches"] = [opening]

        candidates = build_candidates(
            [_prediction()], _official_odds(), snapshot, _config(), {}
        )

        home = next(candidate for candidate in candidates if candidate.market_type == "had" and candidate.selection == "胜")
        self.assertEqual("medium", home.data_quality)

    def test_naive_beijing_kickoff_compares_with_aware_decision_capture(self):
        prediction = _prediction()
        prediction["kickoff_at"] = "2026-07-18 20:00"
        snapshot = _snapshot(kickoff_at="2026-07-18 20:00")
        snapshot["captured_at"] = "2026-07-17T04:00:00+00:00"

        candidates = build_candidates(
            [prediction], _official_odds(), snapshot, _config(), {}
        )

        self.assertTrue(candidates)

    def test_invalid_decision_or_kickoff_timestamp_excludes_without_raising(self):
        invalid_capture = _snapshot()
        invalid_capture["captured_at"] = "not-a-timestamp"
        invalid_kickoff = _snapshot(kickoff_at="not-a-timestamp")
        prediction = _prediction()
        prediction["kickoff_at"] = "not-a-timestamp"

        self.assertEqual(
            [], build_candidates([_prediction()], _official_odds(), invalid_capture, _config(), {})
        )
        self.assertEqual(
            [], build_candidates([prediction], _official_odds(), invalid_kickoff, _config(), {})
        )

    def test_aware_decision_capture_preserves_its_offset_when_checked_against_naive_kickoff(self):
        prediction = _prediction()
        prediction["kickoff_at"] = "2026-07-17 10:00"
        snapshot = _snapshot(kickoff_at="2026-07-17 10:00")
        snapshot["captured_at"] = "2026-07-17T04:00:00+00:00"

        self.assertEqual(
            [], build_candidates([prediction], _official_odds(), snapshot, _config(), {})
        )

    def test_global_settled_samples_controls_gate_and_weight_for_every_market(self):
        config = _config()
        strategy = config["value_strategy"]
        strategy.update({
            "strict_model_edge_weight_base": 0.0,
            "strict_model_edge_weight_max": 0.0,
            "model_edge_weight_base": 1.0,
            "model_edge_weight_max": 1.0,
            "strict_min_probability_edge": 1.0,
            "min_probability_edge": -1.0,
            "strict_min_expected_return": 0.0,
            "min_expected_return": 0.0,
        })
        candidates = build_candidates(
            [_prediction()],
            _official_odds(),
            _snapshot(),
            config,
            {"Test League": {"enabled": True, "adjustment": 0.05, "sample_count": 200}},
        )
        selections = (
            _candidate(candidates, "had", THREE_WAY_SELECTIONS["h"]),
            _candidate(candidates, "had", THREE_WAY_SELECTIONS["d"]),
            _candidate(candidates, "hhad", THREE_WAY_SELECTIONS["h"]),
            _candidate(candidates, "ttg"),
        )

        for candidate in selections:
            with self.subTest(candidate_id=candidate.candidate_id):
                self.assertAlmostEqual(
                    candidate.official_market_probability,
                    candidate.conservative_probability,
                )
                self.assertIn("probability_edge", candidate.value_gate_reasons)

    def test_only_complete_embedded_opening_snapshot_can_be_high_quality(self):
        snapshot = _snapshot()
        snapshot["opening"] = _opening_snapshot()

        candidates = build_candidates(
            [_prediction()], _official_odds(), snapshot, _config(), {}
        )

        home = _candidate(candidates, "had", THREE_WAY_SELECTIONS["h"])
        self.assertEqual("high", home.data_quality)
        self.assertEqual(1.0, home.data_quality_multiplier)

    def test_invalid_opening_evidence_stays_medium_and_does_not_drive_volatility(self):
        cases = {
            "bare matches": lambda snapshot: snapshot.update(
                {"opening_matches": _opening_snapshot()["matches"]}
            ),
            "wrong identity": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot_with_team("Other Home")}
            ),
            "missing market": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot_without_market()}
            ),
            "missing price": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot_without_price()}
            ),
            "wrong source": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot(source="external-consensus")}
            ),
            "wrong phase": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot(phase="monitoring")}
            ),
            "late capture": lambda snapshot: snapshot.update(
                {"opening": _opening_snapshot(captured_at="2026-07-17T13:00:00+08:00")}
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                snapshot = _snapshot()
                mutate(snapshot)
                opening = snapshot.get("opening")
                if isinstance(opening, dict) and name not in {"missing market", "missing price"}:
                    opening["matches"][0]["markets"]["had"]["h"] = 3.50

                candidates = build_candidates(
                    [_prediction()], _official_odds(), snapshot, _config(), {}
                )

                home = _candidate(candidates, "had", THREE_WAY_SELECTIONS["h"])
                self.assertEqual("medium", home.data_quality)
                self.assertEqual(0.6, home.data_quality_multiplier)
                self.assertEqual("stable", home.volatility_band)

    def test_opening_handicap_line_must_match_before_high_quality_is_allowed(self):
        snapshot = _snapshot()
        opening = _opening_snapshot()
        opening["matches"][0]["markets"]["hhad"]["goalLine"] = "-1"
        snapshot["opening"] = opening

        candidates = build_candidates(
            [_prediction()], _official_odds(), snapshot, _config(), {}
        )

        handicap_home = _candidate(candidates, "hhad", THREE_WAY_SELECTIONS["h"])
        self.assertEqual("medium", handicap_home.data_quality)

    def test_conservative_probability_and_volatility_controls(self):
        self.assertEqual(0.001, conservative_probability(0.0, 0.0, 1.0))
        self.assertEqual(0.999, conservative_probability(1.0, 1.0, 1.0))
        self.assertAlmostEqual(0.56, conservative_probability(0.80, 0.40, 0.40))
        self.assertEqual("stable", odds_volatility(2.0, 2.1).band)
        self.assertEqual("stable", odds_volatility(None, 2.0).band)
        self.assertEqual("volatile", odds_volatility(2.0, 2.3).band)
        self.assertEqual("unverified_jump", odds_volatility(2.0, 3.0).band)


def _prediction() -> dict:
    return {
        "date": "2026-07-18",
        "match_id": "match-1",
        "stage": "Test League",
        "team_a": "Home",
        "team_b": "Away",
        "kickoff_at": "2026-07-18T20:00:00+08:00",
        "p_a": 0.60,
        "p_draw": 0.25,
        "p_b": 0.15,
        "xg_a": 1.6,
        "xg_b": 0.8,
    }


def _official_odds() -> dict:
    return {
        "match-1": {
            "had": _market("had", {"h": 2.20, "d": 4.00, "a": 6.00}),
            "hhad": _market("hhad", {"h": 1.75, "d": 3.80, "a": 4.60, "goalLine": "+1"}),
            "ttg": _market("ttg", {f"s{number}": 2.0 + number for number in range(8)}),
        }
    }


def _market(market_type: str, prices: dict):
    market = normalize_market(
        "match-1",
        market_type,
        {
            **prices,
            "source": "sporttery",
            "source_record_id": f"record-{market_type}",
            "captured_at_bjt": "2026-07-17T12:00:00+08:00",
        },
    )
    assert market is not None
    return market


def _snapshot(
    *,
    match_id: str = "match-1",
    team_a: str = "Home",
    kickoff_at: str = "2026-07-18T20:00:00+08:00",
    single_had: bool = True,
) -> dict:
    return {
        "captured_at": "2026-07-17T12:00:00+08:00",
        "capture_phase": "decision",
        "matches": [{
            "match_id": match_id,
            "team_a": team_a,
            "team_b": "Away",
            "kickoff_at": kickoff_at,
            "markets": {
                "had": {"h": 2.20, "d": 4.00, "a": 6.00},
                "hhad": {"h": 1.75, "d": 3.80, "a": 4.60, "goalLine": "+1"},
                "ttg": {f"s{number}": 2.0 + number for number in range(8)},
            },
            "single_eligibility": {"had": single_had, "hhad": False, "ttg": False},
        }],
    }


def _opening_snapshot(
    *,
    source: str = "sporttery",
    phase: str = "opening",
    captured_at: str = "2026-07-17T11:00:00+08:00",
) -> dict:
    match = deepcopy(_snapshot()["matches"][0])
    return {
        "captured_at": captured_at,
        "capture_phase": phase,
        "source": source,
        "matches": [match],
    }


def _opening_snapshot_without_price() -> dict:
    opening = _opening_snapshot()
    del opening["matches"][0]["markets"]["had"]["h"]
    return opening


def _opening_snapshot_with_team(team_a: str) -> dict:
    opening = _opening_snapshot()
    opening["matches"][0]["team_a"] = team_a
    return opening


def _opening_snapshot_without_market() -> dict:
    opening = _opening_snapshot()
    del opening["matches"][0]["markets"]["had"]
    return opening


def _candidate(candidates, market_type: str, selection: str | None = None):
    return next(
        candidate
        for candidate in candidates
        if candidate.market_type == market_type
        and (selection is None or candidate.selection == selection)
    )


def _config() -> dict:
    return {
        "value_strategy": {
            "strict_until_samples": 100,
            "observation_count": 10,
            "calibration_prior": 100,
            "strict_model_edge_weight_base": 1.0,
            "strict_model_edge_weight_max": 1.0,
            "strict_min_probability_edge": 0.01,
            "strict_min_expected_return": 1.01,
            "min_probability_edge": 0.01,
            "min_expected_return": 1.01,
        }
    }


if __name__ == "__main__":
    unittest.main()
