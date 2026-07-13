from dataclasses import dataclass
import math


QUALITY = {"medium": 1, "high": 2}
MIN_ODDS = 1.01
MAX_ODDS = 100.0
MAX_XG_TOTAL = 10.0
MAX_EXPECTED_VALUE = 100.0
MAX_SCORE = 10.0
COLD_RESISTANCE_SIGNALS = {
    "underdog_resistance",
    "underdog_defense",
    "knockout_caution",
    "low_total",
    "favorite_finishing_risk",
}


@dataclass(frozen=True)
class MarketEvidence:
    source: str
    market_type: str
    settlement_minutes: int
    includes_extra_time: bool


@dataclass(frozen=True)
class DrawInputs:
    match_id: str
    team_a: str
    team_b: str
    stage: str
    domestic_odds: tuple[float, float, float]
    model_probabilities: tuple[float, float, float]
    calibrated_draw_probability: float
    xg_total: float
    source_count: int
    market_sources: tuple[MarketEvidence, ...]
    market_scope: str
    favorite_movement: float
    regional_gap: float
    underdog_win_probability: float
    underdog_not_lose_probability: float
    structural_signals: tuple[str, ...]
    data_quality: str


@dataclass(frozen=True)
class DrawCandidate:
    inputs: DrawInputs
    subtype: str
    domestic_draw_probability: float
    draw_edge: float
    expected_value: float
    score: float


def is_finite_between(value: object, lower: float, upper: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and lower <= value <= upper


def valid_odds(odds: tuple[float, float, float]) -> bool:
    return len(odds) == 3 and all(is_finite_between(value, MIN_ODDS, MAX_ODDS) for value in odds)


def fair_probabilities(home_odds: float, draw_odds: float, away_odds: float) -> tuple[float, float, float] | None:
    if not valid_odds((home_odds, draw_odds, away_odds)):
        return None
    implied = (1 / home_odds, 1 / draw_odds, 1 / away_odds)
    total = sum(implied)
    fair = tuple(value / total for value in implied)
    return fair if all(is_finite_between(value, 0.0, 1.0) for value in fair) else None


def _valid_inputs(inputs: DrawInputs) -> bool:
    probabilities = inputs.model_probabilities
    return (
        valid_odds(inputs.domestic_odds)
        and len(probabilities) == 3
        and all(is_finite_between(value, 0.0, 1.0) for value in probabilities)
        and math.isclose(sum(probabilities), 1.0, abs_tol=0.02)
        and is_finite_between(inputs.calibrated_draw_probability, 0.0, 1.0)
        and is_finite_between(inputs.xg_total, 0.0, MAX_XG_TOTAL)
        and is_finite_between(inputs.favorite_movement, -1.0, 1.0)
        and is_finite_between(inputs.regional_gap, -1.0, 1.0)
        and is_finite_between(inputs.underdog_win_probability, 0.0, 1.0)
        and is_finite_between(inputs.underdog_not_lose_probability, 0.0, 1.0)
        and inputs.underdog_not_lose_probability >= inputs.underdog_win_probability
    )


def classify_candidate(inputs: DrawInputs, config: dict) -> DrawCandidate | None:
    if inputs.market_scope != "90m" or inputs.data_quality not in QUALITY or not _valid_inputs(inputs):
        return None
    valid_sources = {
        evidence.source
        for evidence in inputs.market_sources
        if (
            evidence.market_type == "win_draw_loss"
            and evidence.settlement_minutes == 90
            and evidence.includes_extra_time is False
        )
    }
    if len(valid_sources) < 2:
        return None
    fair = fair_probabilities(*inputs.domestic_odds)
    if fair is None:
        return None
    probability = inputs.calibrated_draw_probability
    edge = probability - fair[1]
    expected_value = probability * inputs.domestic_odds[1]
    if not is_finite_between(edge, -1.0, 1.0) or not is_finite_between(expected_value, 0.0, MAX_EXPECTED_VALUE):
        return None
    if probability < config["min_draw_probability"] or edge < config["min_draw_edge"]:
        return None
    if expected_value < config["min_expected_value"] or inputs.xg_total > config["max_xg_total"]:
        return None
    favorite = max(fair[0], fair[2])
    win_gap = abs(fair[0] - fair[2])
    if favorite >= config["cold_favorite_probability"]:
        enough_heat = inputs.favorite_movement <= -0.04 and inputs.regional_gap >= 0.05
        resistance_signals = set(inputs.structural_signals) & COLD_RESISTANCE_SIGNALS
        underdog_resistance = (
            inputs.underdog_not_lose_probability >= 0.35
            and probability > inputs.underdog_win_probability
            and "underdog_resistance" in resistance_signals
        )
        subtype = "cold_draw" if enough_heat and underdog_resistance and len(resistance_signals) >= 2 else ""
    else:
        subtype = "balanced_draw" if win_gap <= config["balanced_max_win_gap"] and inputs.xg_total <= config["balanced_max_xg_total"] and len(inputs.structural_signals) >= 2 else ""
    if not subtype:
        return None
    score = edge * 4 + (expected_value - 1) * 2 + probability + QUALITY[inputs.data_quality] * 0.02
    if not is_finite_between(score, -MAX_SCORE, MAX_SCORE):
        return None
    return DrawCandidate(inputs, subtype, fair[1], edge, expected_value, score)


def rank_candidates(candidates: list[DrawCandidate]) -> list[DrawCandidate]:
    valid = [
        item
        for item in candidates
        if item.inputs.data_quality in QUALITY
        and is_finite_between(item.domestic_draw_probability, 0.0, 1.0)
        and is_finite_between(item.draw_edge, -1.0, 1.0)
        and is_finite_between(item.expected_value, 0.0, MAX_EXPECTED_VALUE)
        and is_finite_between(item.score, -MAX_SCORE, MAX_SCORE)
    ]
    return sorted(valid, key=lambda item: (item.score, QUALITY[item.inputs.data_quality]), reverse=True)
