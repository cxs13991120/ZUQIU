"""Deterministic, simulation-only allocation for verified value candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Iterable

from official_markets import TRUSTED_SOURCES
from value_candidates import ValueCandidate


SUPPORTED_MARKETS = frozenset({"had", "hhad", "ttg"})
PLAY_BY_MARKET = {"had": "HAD", "hhad": "HHAD", "ttg": "TTG"}
QUALITY_RANK = {"high": 0, "medium": 1}
VOLATILITY_RANK = {"stable": 0, "volatile": 1}
QUARTER_KELLY = 0.25
HARD_MAX_MATCH_EXPOSURE = 200
HARD_MAX_SINGLE_STAKE = 200
HARD_SINGLE_BUDGET_CAP = 200
HARD_MAX_SINGLE_COUNT = 2
HARD_MAX_PARLAY_STAKE = 30
HARD_MAX_DAILY_STAKE = 500
HARD_MONTHLY_BUDGET_CAP = 5000
HARD_MONTHLY_STOP_LOSS = 5000


@dataclass(frozen=True)
class PortfolioLimits:
    bankroll: float = 5000
    kelly_fraction: float = QUARTER_KELLY
    stake_unit: int = 2
    max_match_exposure: int = 200
    max_single_stake: int = 200
    single_budget_cap: int = 200
    max_single_count: int = 2
    min_single_stake: int = 2
    max_parlay_stake: int = 30
    min_parlay_stake: int = 2
    max_daily_stake: int = 500
    monthly_budget_cap: int = 5000
    monthly_stop_loss: int = 5000


@dataclass(frozen=True)
class ParlayCandidate:
    legs: tuple[ValueCandidate, ValueCandidate]
    pair_id: str
    combined_probability: float
    combined_odds: float
    expected_value: float
    expected_log_growth: float

    @property
    def match_ids(self) -> tuple[str, str]:
        return tuple(leg.match_id for leg in self.legs)


@dataclass(frozen=True)
class AllocatedSingle:
    candidate: ValueCandidate
    stake: int


@dataclass(frozen=True)
class AllocatedParlay:
    parlay: ParlayCandidate
    stake: int


@dataclass(frozen=True)
class Portfolio:
    singles: tuple[AllocatedSingle, ...]
    parlay: AllocatedParlay | None
    rejections: tuple[str, ...] = ()

    @property
    def total_stake(self) -> int:
        return sum(item.stake for item in self.singles) + (self.parlay.stake if self.parlay else 0)

    @property
    def parlays(self) -> tuple[AllocatedParlay, ...]:
        return (self.parlay,) if self.parlay else ()

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.rejections


def full_kelly(probability: float, odds: float) -> float:
    """Return the positive full-Kelly fraction for decimal odds, or zero."""
    probability = _finite_number(probability)
    odds = _finite_number(odds)
    if probability is None or odds is None or not 0.0 < probability < 1.0 or odds <= 1.0:
        return 0.0
    b = odds - 1.0
    return max(0.0, (b * probability - (1.0 - probability)) / b)


def stake_for(candidate: ValueCandidate, bankroll: float, kelly_fraction: float) -> int:
    """Size one candidate with Kelly only; portfolio limits are applied elsewhere."""
    bankroll = _finite_number(bankroll)
    kelly_fraction = _finite_number(kelly_fraction)
    multipliers = _candidate_multipliers(candidate)
    if bankroll is None or kelly_fraction is None or bankroll <= 0 or kelly_fraction <= 0 or multipliers is None:
        return 0
    probability = _finite_number(candidate.conservative_probability)
    odds = _finite_number(candidate.official_odds)
    if probability is None or odds is None:
        return 0
    raw = bankroll * full_kelly(probability, odds) * kelly_fraction
    for multiplier in multipliers:
        raw *= multiplier
    return _round_down(raw, 2)


def build_two_leg_candidates(candidates: list[ValueCandidate], config: dict) -> list[ParlayCandidate]:
    """Build deterministic, legal two-leg parlay candidates from independent legs."""
    strategy = _strategy(config)
    gates = _combo_gates(strategy)
    if gates is None:
        return []
    legs = [candidate for candidate in _unique_candidates(candidates) if _parlay_leg_reason(candidate, gates) is None]
    parlays = []
    for index, left in enumerate(legs):
        for right in legs[index + 1 :]:
            if left.match_id == right.match_id or _correlated(left, right):
                continue
            probability = left.conservative_probability * right.conservative_probability
            odds = left.official_odds * right.official_odds
            expected_value = probability * odds - 1.0
            if not _finite_positive(probability) or not _finite_number(odds) or odds <= 1.0 or expected_value <= 0:
                continue
            if 1.0 + expected_value < gates["combined_expected_return"]:
                continue
            multiplier = _parlay_multiplier((left, right))
            if multiplier is None:
                continue
            growth = _expected_log_growth(probability, odds, QUARTER_KELLY * full_kelly(probability, odds) * multiplier)
            if growth is None or growth <= 0:
                continue
            ordered = tuple(sorted((left, right), key=lambda candidate: candidate.candidate_id))
            parlays.append(
                ParlayCandidate(
                    legs=ordered,
                    pair_id="|".join(candidate.candidate_id for candidate in ordered),
                    combined_probability=probability,
                    combined_odds=odds,
                    expected_value=expected_value,
                    expected_log_growth=growth,
                )
            )
    return sorted(parlays, key=lambda parlay: (-parlay.expected_log_growth, -parlay.expected_value, parlay.pair_id))


def allocate_portfolio(
    candidates: list[ValueCandidate], limits: PortfolioLimits, account: dict, config: dict | None = None
) -> Portfolio:
    """Allocate a bounded paid portfolio without mutating candidates or doing I/O."""
    limit_values = _validated_limits(limits)
    if limit_values is None:
        return Portfolio((), None, ("invalid_limits",))
    monthly_stake, realized_profit = _account_values(account)
    if monthly_stake is None or realized_profit is None:
        return Portfolio((), None, ("invalid_account",))
    if realized_profit <= -limit_values.monthly_stop_loss:
        return Portfolio((), None, ("monthly_stop_loss",))

    rejections: list[str] = []
    available_monthly = max(0.0, limit_values.monthly_budget_cap - monthly_stake)
    if available_monthly < limit_values.stake_unit:
        rejections.append("monthly_budget_cap")

    unique_candidates, duplicate_reasons = _unique_candidates_with_reasons(candidates)
    rejections.extend(duplicate_reasons)
    ranked: list[tuple[ValueCandidate, float]] = []
    for candidate in unique_candidates:
        reason, growth = _single_reason_and_growth(candidate, limit_values)
        if reason is not None:
            rejections.append(f"{candidate.candidate_id}:{reason}")
            continue
        ranked.append((candidate, growth))
    ranked.sort(key=lambda item: _single_sort_key(item[0], item[1]))

    singles: list[AllocatedSingle] = []
    selected_matches: set[str] = set()
    match_exposure: dict[str, int] = {}
    single_stake = 0
    daily_stake = 0
    for candidate, _ in ranked:
        if candidate.match_id in selected_matches:
            rejections.append(f"{candidate.candidate_id}:single_match_already_selected")
            continue
        if len(singles) >= limit_values.max_single_count:
            rejections.append(f"{candidate.candidate_id}:max_single_count")
            continue
        raw_stake = stake_for(candidate, limit_values.bankroll, limit_values.kelly_fraction)
        cap = min(
            raw_stake,
            limit_values.max_single_stake,
            limit_values.max_match_exposure - match_exposure.get(candidate.match_id, 0),
            limit_values.single_budget_cap - single_stake,
            limit_values.max_daily_stake - daily_stake,
            available_monthly - daily_stake,
        )
        stake = _round_down(cap, limit_values.stake_unit)
        if stake < limit_values.min_single_stake:
            rejections.append(f"{candidate.candidate_id}:single_limit")
            continue
        singles.append(AllocatedSingle(candidate, stake))
        selected_matches.add(candidate.match_id)
        match_exposure[candidate.match_id] = match_exposure.get(candidate.match_id, 0) + stake
        single_stake += stake
        daily_stake += stake

    parlay = None
    parlay_config = config if isinstance(config, dict) else _default_combo_config()
    for candidate in build_two_leg_candidates(unique_candidates, parlay_config):
        if selected_matches.intersection(candidate.match_ids):
            rejections.append(f"{candidate.pair_id}:parlay_reuses_single_match")
            continue
        raw_stake = _parlay_stake(candidate, limit_values)
        cap = min(
            raw_stake,
            limit_values.max_parlay_stake,
            *(limit_values.max_match_exposure - match_exposure.get(match_id, 0) for match_id in candidate.match_ids),
            limit_values.max_daily_stake - daily_stake,
            available_monthly - daily_stake,
        )
        stake = _round_down(cap, limit_values.stake_unit)
        if stake < limit_values.min_parlay_stake:
            rejections.append(f"{candidate.pair_id}:parlay_limit")
            continue
        parlay = AllocatedParlay(candidate, stake)
        break

    return Portfolio(tuple(singles), parlay, tuple(sorted(set(rejections))))


def _single_reason_and_growth(candidate: ValueCandidate, limits: PortfolioLimits) -> tuple[str | None, float | None]:
    if candidate.paid_eligible is not True:
        return "not_paid_eligible", None
    if candidate.single_eligible is not True:
        return "not_single_eligible", None
    reason = _common_candidate_reason(candidate)
    if reason is not None:
        return reason, None
    if candidate.expected_value <= 0:
        return "nonpositive_expected_value", None
    growth = _candidate_growth(candidate, limits.kelly_fraction)
    if growth is None or growth <= 0 or stake_for(candidate, limits.bankroll, limits.kelly_fraction) <= 0:
        return "nonpositive_kelly", None
    return None, growth


def _parlay_leg_reason(candidate: ValueCandidate, gates: dict[str, float]) -> str | None:
    reason = _common_candidate_reason(candidate)
    if reason is not None:
        return reason
    if candidate.conservative_probability < gates["leg_probability"]:
        return "leg_probability"
    if candidate.probability_edge < gates["leg_edge"]:
        return "leg_edge"
    if candidate.expected_value <= 0 or 1.0 + candidate.expected_value < gates["leg_expected_return"]:
        return "leg_expected_return"
    return None


def _common_candidate_reason(candidate: ValueCandidate) -> str | None:
    if candidate.market_type not in SUPPORTED_MARKETS:
        return "unsupported_market"
    if candidate.play != PLAY_BY_MARKET[candidate.market_type]:
        return "unsupported_play"
    if candidate.data_quality not in QUALITY_RANK:
        return "data_quality"
    if candidate.odds_source not in TRUSTED_SOURCES or not _nonempty_text(candidate.source_record_id) or not _nonempty_text(candidate.captured_at_bjt):
        return "unlocked_domestic_odds"
    probability = _finite_number(candidate.conservative_probability)
    odds = _finite_number(candidate.official_odds)
    expected_value = _finite_number(candidate.expected_value)
    multipliers = _candidate_multipliers(candidate)
    if (
        probability is None
        or not 0 < probability < 1
        or odds is None
        or odds <= 1
        or expected_value is None
        or multipliers is None
    ):
        return "invalid_candidate_values"
    return None


def _candidate_growth(candidate: ValueCandidate, kelly_fraction: float) -> float | None:
    multipliers = _candidate_multipliers(candidate)
    if multipliers is None:
        return None
    fraction = kelly_fraction * full_kelly(candidate.conservative_probability, candidate.official_odds)
    for multiplier in multipliers:
        fraction *= multiplier
    return _expected_log_growth(candidate.conservative_probability, candidate.official_odds, fraction)


def _expected_log_growth(probability: float, odds: float, fraction: float) -> float | None:
    probability = _finite_number(probability)
    odds = _finite_number(odds)
    fraction = _finite_number(fraction)
    if probability is None or odds is None or fraction is None or not 0 < probability < 1 or odds <= 1 or not 0 < fraction < 1:
        return None
    win = 1.0 + fraction * (odds - 1.0)
    loss = 1.0 - fraction
    if win <= 0 or loss <= 0:
        return None
    growth = probability * math.log(win) + (1.0 - probability) * math.log(loss)
    return growth if math.isfinite(growth) else None


def _single_sort_key(candidate: ValueCandidate, growth: float) -> tuple:
    return (
        -growth,
        -candidate.expected_value,
        -_nonnegative_integer(candidate.calibration_samples),
        QUALITY_RANK[candidate.data_quality],
        VOLATILITY_RANK.get(candidate.volatility_band, 2),
        candidate.candidate_id,
    )


def _parlay_stake(parlay: ParlayCandidate, limits: PortfolioLimits) -> int:
    multiplier = _parlay_multiplier(parlay.legs)
    if multiplier is None:
        return 0
    raw = limits.bankroll * full_kelly(parlay.combined_probability, parlay.combined_odds) * limits.kelly_fraction * multiplier
    return _round_down(raw, limits.stake_unit)


def _correlated(left: ValueCandidate, right: ValueCandidate) -> bool:
    left_tags = {tag for tag in left.correlation_tags if isinstance(tag, str) and not tag.startswith("league:")}
    right_tags = {tag for tag in right.correlation_tags if isinstance(tag, str) and not tag.startswith("league:")}
    return bool(left_tags.intersection(right_tags))


def _combo_gates(strategy: dict) -> dict[str, float] | None:
    settled = _nonnegative_integer(strategy.get("settled_samples"))
    strict_until = _nonnegative_integer(strategy.get("strict_until_samples"))
    if settled is None or strict_until is None:
        return None
    strict = settled < strict_until
    prefix = "strict_" if strict else ""
    values = {
        "leg_probability": strategy.get("min_combo_leg_probability"),
        "leg_edge": strategy.get(f"{prefix}min_combo_leg_edge"),
        "leg_expected_return": strategy.get(f"{prefix}min_combo_leg_expected_return"),
        "combined_expected_return": strategy.get(f"{prefix}min_combo_expected_return"),
    }
    parsed = {name: _finite_number(value) for name, value in values.items()}
    if any(value is None for value in parsed.values()):
        return None
    if parsed["leg_probability"] < 0 or parsed["leg_probability"] >= 1 or parsed["leg_edge"] < 0:
        return None
    if parsed["leg_expected_return"] <= 0 or parsed["combined_expected_return"] <= 0:
        return None
    return parsed


def _strategy(config: dict) -> dict:
    value = config.get("value_strategy") if isinstance(config, dict) else None
    return value if isinstance(value, dict) else {}


def _default_combo_config() -> dict:
    return {"value_strategy": {
        "settled_samples": 100,
        "strict_until_samples": 100,
        "min_combo_leg_probability": 0.45,
        "strict_min_combo_leg_edge": 0.02,
        "min_combo_leg_edge": 0.02,
        "strict_min_combo_leg_expected_return": 1.01,
        "min_combo_leg_expected_return": 1.01,
        "strict_min_combo_expected_return": 1.03,
        "min_combo_expected_return": 1.03,
    }}


def _validated_limits(limits: PortfolioLimits) -> PortfolioLimits | None:
    if not isinstance(limits, PortfolioLimits):
        return None
    if _finite_number(limits.bankroll) is None or limits.bankroll <= 0:
        return None
    if _finite_number(limits.kelly_fraction) != QUARTER_KELLY or limits.stake_unit != 2:
        return None
    values = (
        limits.max_match_exposure,
        limits.max_single_stake,
        limits.single_budget_cap,
        limits.max_single_count,
        limits.min_single_stake,
        limits.max_parlay_stake,
        limits.min_parlay_stake,
        limits.max_daily_stake,
        limits.monthly_budget_cap,
        limits.monthly_stop_loss,
    )
    if any(_nonnegative_integer(value) is None for value in values):
        return None
    if limits.min_single_stake < 2 or limits.min_parlay_stake < 2:
        return None
    return replace(
        limits,
        max_match_exposure=min(limits.max_match_exposure, HARD_MAX_MATCH_EXPOSURE),
        max_single_stake=min(limits.max_single_stake, HARD_MAX_SINGLE_STAKE),
        single_budget_cap=min(limits.single_budget_cap, HARD_SINGLE_BUDGET_CAP),
        max_single_count=min(limits.max_single_count, HARD_MAX_SINGLE_COUNT),
        max_parlay_stake=min(limits.max_parlay_stake, HARD_MAX_PARLAY_STAKE),
        max_daily_stake=min(limits.max_daily_stake, HARD_MAX_DAILY_STAKE),
        monthly_budget_cap=min(limits.monthly_budget_cap, HARD_MONTHLY_BUDGET_CAP),
        monthly_stop_loss=min(limits.monthly_stop_loss, HARD_MONTHLY_STOP_LOSS),
    )


def _account_values(account: dict) -> tuple[float | None, float | None]:
    if not isinstance(account, dict) or "monthly_stake" not in account:
        return None, None
    stake = _finite_number(account.get("monthly_stake"))
    profit_value = account.get("monthly_realized_profit", account.get("monthly_profit"))
    profit = _finite_number(profit_value)
    if stake is None or profit is None or stake < 0:
        return None, None
    return stake, profit


def _unique_candidates(candidates: Iterable[ValueCandidate]) -> list[ValueCandidate]:
    return _unique_candidates_with_reasons(candidates)[0]


def _unique_candidates_with_reasons(candidates: Iterable[ValueCandidate]) -> tuple[list[ValueCandidate], list[str]]:
    grouped: dict[str, list[ValueCandidate]] = {}
    for candidate in candidates if isinstance(candidates, list) else ():
        if isinstance(candidate, ValueCandidate) and _nonempty_text(candidate.candidate_id):
            grouped.setdefault(candidate.candidate_id, []).append(candidate)
    selected = []
    reasons = []
    for candidate_id in sorted(grouped):
        group = grouped[candidate_id]
        representative = min(group, key=_candidate_identity_key)
        selected.append(representative)
        if len(group) > 1:
            reasons.append(f"{candidate_id}:duplicate_candidate_id")
    return selected, reasons


def _candidate_identity_key(candidate: ValueCandidate) -> tuple:
    growth = _candidate_growth(candidate, QUARTER_KELLY)
    return (
        -(growth if growth is not None else -math.inf),
        -(_finite_number(candidate.expected_value) or -math.inf),
        str(candidate.match_id),
        str(candidate.market_type),
        str(candidate.selection),
        str(candidate.source_record_id),
    )


def _candidate_multipliers(candidate: ValueCandidate) -> tuple[float, float, float] | None:
    values = tuple(_finite_number(getattr(candidate, name, None)) for name in (
        "data_quality_multiplier", "volatility_multiplier", "performance_multiplier",
    ))
    if any(value is None or value < 0 for value in values):
        return None
    return values


def _parlay_multiplier(legs: tuple[ValueCandidate, ValueCandidate]) -> float | None:
    multipliers = tuple(_candidate_multipliers(leg) for leg in legs)
    if any(values is None for values in multipliers):
        return None
    return (
        min(values[0] for values in multipliers)
        * min(values[1] for values in multipliers)
        * min(values[2] for values in multipliers)
    )


def _round_down(value: float, unit: int) -> int:
    value = _finite_number(value)
    if value is None or value <= 0 or not isinstance(unit, int) or unit <= 0:
        return 0
    return int(math.floor(value / unit)) * unit


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _finite_positive(value: object) -> bool:
    number = _finite_number(value)
    return number is not None and number > 0


def _nonnegative_integer(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
