import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from betting_ledger import DOMESTIC_ODDS_SOURCES, stable_bet_id
from generate_betting_plan import build_value_v4_plan
from official_markets import (
    THREE_WAY_SELECTIONS,
    TOTAL_GOALS_SELECTIONS,
    parse_handicap,
)


ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
SCHEMA_VERSION = "shadow-portfolio-activation-audit-v1"
OUTPUT_NAME = "shadow_portfolio_activation_audit.json"
ALLOWED_SINGLE_PLAYS = {"had": "HAD", "hhad": "HHAD", "ttg": "TTG"}
THREE_WAY_SELECTION_LABELS = frozenset(THREE_WAY_SELECTIONS.values())
TOTAL_GOALS_SELECTION_LABELS = frozenset(TOTAL_GOALS_SELECTIONS.values())
HARD_STAKE_UNIT = Decimal("2")
HARD_MATCH_EXPOSURE = Decimal("200")
HARD_PARLAY_STAKE = Decimal("30")
HARD_DAILY_STAKE = Decimal("500")
HARD_MONTHLY_STAKE = Decimal("5000")


def audit_generated_portfolios(
    portfolios: dict[str, list[dict]], config: dict
) -> dict:
    """Mechanically validate deterministic paid portfolios without outcomes."""
    checked_dates = sorted(portfolios)
    violations: list[dict] = []
    valid_rows: dict[str, list[tuple[dict, Decimal, tuple[str, ...]]]] = {}
    paid_rows = 0
    singles = 0
    parlays = 0
    legs_count = 0

    _validate_safety_config(config, violations)
    if not checked_dates:
        _violate(violations, "zero_checked_dates")

    for report_date in checked_dates:
        rows = portfolios[report_date]
        if not isinstance(rows, list):
            _violate(
                violations,
                "portfolio_invalid",
                report_date=report_date,
                detail="portfolio must be a list",
            )
            valid_rows[report_date] = []
            continue
        seen_ids: set[str] = set()
        date_rows = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                _violate(
                    violations,
                    "portfolio_row_invalid",
                    report_date=report_date,
                    row_index=row_index,
                )
                continue
            stake = _decimal(row.get("stake"))
            if stake is None or stake <= 0:
                if stake is None or stake < 0:
                    _violate(
                        violations,
                        "stake_invalid",
                        report_date=report_date,
                        row_index=row_index,
                    )
                continue
            paid_rows += 1

            canonical_id = _canonical_bet_id(row)
            provided_id = row.get("bet_id")
            if canonical_id is None or provided_id != canonical_id:
                _violate(
                    violations,
                    "invalid_bet_id",
                    report_date=report_date,
                    row_index=row_index,
                )
            identity = str(canonical_id or provided_id or "")
            if identity in seen_ids:
                _violate(
                    violations,
                    "duplicate_bet_id",
                    report_date=report_date,
                    row_index=row_index,
                    bet_id=identity,
                )
            seen_ids.add(identity)

            identity_errors, match_ids, leg_total = _validate_paid_identity(row)
            for code, detail in identity_errors:
                _violate(
                    violations,
                    code,
                    report_date=report_date,
                    row_index=row_index,
                    detail=detail,
                    bet_id=identity,
                )
            legs_count += leg_total

            if row.get("report_date", row.get("date")) != report_date:
                _violate(
                    violations,
                    "report_date_mismatch",
                    report_date=report_date,
                    row_index=row_index,
                    bet_id=identity,
                )

            expected_value = _decimal(row.get("expected_value"))
            net_ev = _decimal(row.get("net_ev"))
            if (
                expected_value is None
                or net_ev is None
                or expected_value <= 0
                or net_ev <= 0
            ):
                _violate(
                    violations,
                    "nonpositive_configured_ev",
                    report_date=report_date,
                    row_index=row_index,
                    bet_id=identity,
                )
            elif expected_value != net_ev:
                _violate(
                    violations,
                    "inconsistent_configured_ev",
                    report_date=report_date,
                    row_index=row_index,
                    bet_id=identity,
                )

            if stake % HARD_STAKE_UNIT != 0:
                _violate(
                    violations,
                    "stake_unit",
                    report_date=report_date,
                    row_index=row_index,
                    value=_json_number(stake),
                    limit=2,
                    bet_id=identity,
                )

            market_type = str(row.get("market_type") or "").lower()
            if market_type == "parlay":
                parlays += 1
            else:
                singles += 1

            # Invalid identities are deliberately excluded from maxima. The gate
            # already fails, and malformed match identities cannot hide exposure.
            if not identity_errors and match_ids:
                date_rows.append((row, stake, tuple(match_ids)))
        valid_rows[report_date] = date_rows

    maxima, limit_violations = _calculate_maxima(valid_rows)
    violations.extend(limit_violations)
    violations = _sorted_violations(violations)
    passed = bool(checked_dates) and not violations
    payload = {
        "schema_version": SCHEMA_VERSION,
        "passed": passed,
        "checked_dates": checked_dates,
        "excluded_dates": [],
        "excluded_missing": [],
        "excluded_invalid": [],
        "counts": {
            "requested_dates": len(checked_dates),
            "checked_dates": len(checked_dates),
            "excluded_dates": 0,
            "excluded_missing_dates": 0,
            "excluded_invalid_dates": 0,
            "paid_rows": paid_rows,
            "singles": singles,
            "parlays": parlays,
            "parlay_legs": legs_count,
        },
        "limits": {
            "stake_unit": 2,
            "match_exposure": 200,
            "parlay_stake": 30,
            "daily_stake": 500,
            "monthly_stake": 5000,
        },
        "maxima": maxima,
        "violations": violations,
        "source_coverage": [
            {
                "date": report_date,
                "status": "checked",
                "sporttery": False,
                "verified_domestic_fallback": False,
            }
            for report_date in checked_dates
        ],
        "evidence": [],
        "historical_artifacts_unchanged": True,
        "simulation_only": True,
        "real_money_automation": False,
        "profitability_gate_applied": False,
    }
    return payload


def run_audit(
    root: Path,
    from_date: date,
    through_date: date,
    *,
    plan_builder: Callable | None = None,
) -> dict:
    """Classify repository evidence, rebuild checked dates, and persist the gate."""
    root = Path(root).resolve()
    if from_date > through_date:
        raise ValueError("from date must not be after through date")
    config = _read_json(root / "betting_config.json")
    protected_before = _protected_hashes(root)
    fixtures, fixtures_error = _read_fixtures(root / "data" / "fixtures.csv")
    builder = plan_builder or build_value_v4_plan
    portfolios: dict[str, list[dict]] = {}
    checked_dates: list[str] = []
    excluded_missing: list[str] = []
    excluded_invalid: list[str] = []
    excluded_dates: list[dict] = []
    source_coverage: list[dict] = []
    evidence: list[dict] = []

    for target_date in _date_range(from_date, through_date):
        report_date = target_date.isoformat()
        prediction_path = root / "output" / f"predictions_{report_date}.csv"
        odds_path = root / "data" / f"sporttery_odds_{report_date}.json"
        snapshot_paths = sorted(
            (root / "data" / "odds_snapshots").glob(
                f"{report_date}-*-decision.json"
            )
        )
        missing = []
        if not prediction_path.is_file():
            missing.append("saved_predictions")
        if fixtures_error == "missing" or not fixtures.get(report_date):
            missing.append("fixtures")
        if not odds_path.is_file():
            missing.append("domestic_odds")
        if not snapshot_paths:
            missing.append("decision_snapshot")
        if missing:
            _exclude_date(
                report_date,
                "excluded_missing",
                missing,
                excluded_missing,
                excluded_dates,
                source_coverage,
            )
            continue

        invalid = []
        predictions, prediction_error = _read_csv_rows(prediction_path)
        if prediction_error or not predictions:
            invalid.append("saved_predictions_invalid")
        odds = None
        try:
            odds = _read_json(odds_path)
        except (OSError, ValueError, json.JSONDecodeError):
            invalid.append("domestic_odds_invalid")
        if not isinstance(odds, dict) or not odds:
            invalid.append("domestic_odds_invalid")
        if fixtures_error == "invalid":
            invalid.append("fixtures_invalid")

        snapshot, snapshot_path, snapshot_errors = _latest_valid_snapshot(
            snapshot_paths, target_date
        )
        invalid.extend(snapshot_errors)
        if snapshot is not None:
            snapshot_ids = {row["match_id"] for row in snapshot["matches"]}
            prediction_ids = {
                str(row.get("match_id") or "") for row in predictions or []
            }
            fixture_ids = {
                str(row.get("match_id") or "") for row in fixtures.get(report_date, [])
            }
            odds_ids = set(odds) if isinstance(odds, dict) else set()
            if not snapshot_ids.issubset(prediction_ids):
                invalid.append("snapshot_predictions_mismatch")
            if not snapshot_ids.issubset(fixture_ids):
                invalid.append("snapshot_fixtures_mismatch")
            if not snapshot_ids.issubset(odds_ids):
                invalid.append("snapshot_domestic_odds_mismatch")

        invalid = sorted(set(invalid))
        if invalid or snapshot is None or snapshot_path is None:
            _exclude_date(
                report_date,
                "excluded_invalid",
                invalid or ["decision_snapshot_invalid"],
                excluded_invalid,
                excluded_dates,
                source_coverage,
            )
            continue

        locked_at = _aware_datetime(snapshot["captured_at"])
        try:
            plan, _observations = builder(target_date, locked_at=locked_at)
        except Exception as exc:
            _exclude_date(
                report_date,
                "excluded_invalid",
                [f"portfolio_rebuild_failed:{type(exc).__name__}"],
                excluded_invalid,
                excluded_dates,
                source_coverage,
            )
            continue
        if not isinstance(plan, list):
            _exclude_date(
                report_date,
                "excluded_invalid",
                ["portfolio_rebuild_invalid"],
                excluded_invalid,
                excluded_dates,
                source_coverage,
            )
            continue

        source = str(snapshot["source"]).lower()
        checked_dates.append(report_date)
        portfolios[report_date] = plan
        coverage = {
            "date": report_date,
            "status": "checked",
            "sporttery": source == "sporttery",
            "verified_domestic_fallback": source == "zgzcw",
        }
        source_coverage.append(coverage)
        evidence.append(
            {
                "date": report_date,
                "decision_capture_timestamp": locked_at.isoformat(),
                "decision_source": source,
                "snapshot": _file_evidence(root, snapshot_path),
                "predictions": _file_evidence(root, prediction_path),
                "domestic_odds": _file_evidence(root, odds_path),
                "fixtures_file": _file_evidence(root, root / "data" / "fixtures.csv"),
                "fixture_match_count": len(fixtures[report_date]),
                "snapshot_match_count": len(snapshot["matches"]),
                "generated_paid_count": len(plan),
                "generated_bet_ids": sorted(str(row.get("bet_id") or "") for row in plan),
            }
        )

    payload = audit_generated_portfolios(portfolios, config)
    payload["checked_dates"] = checked_dates
    payload["excluded_missing"] = excluded_missing
    payload["excluded_invalid"] = excluded_invalid
    payload["excluded_dates"] = excluded_dates
    payload["source_coverage"] = source_coverage
    payload["evidence"] = evidence
    requested_count = (through_date - from_date).days + 1
    payload["counts"].update(
        requested_dates=requested_count,
        checked_dates=len(checked_dates),
        excluded_dates=len(excluded_dates),
        excluded_missing_dates=len(excluded_missing),
        excluded_invalid_dates=len(excluded_invalid),
    )
    protected_after = _protected_hashes(root)
    payload["historical_artifacts_unchanged"] = protected_before == protected_after
    payload["historical_artifact_hashes"] = protected_after
    if not payload["historical_artifacts_unchanged"]:
        payload["violations"].append({"code": "historical_artifact_mutation"})
        payload["violations"] = _sorted_violations(payload["violations"])
    payload["passed"] = bool(checked_dates) and not payload["violations"]

    validate_audit_payload(payload)
    output_path = root / "output" / OUTPUT_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(output_path)
    persisted = _read_json(output_path)
    validate_audit_payload(persisted)
    if persisted != payload:
        raise ValueError("persisted audit differs from validated payload")
    return payload


def validate_audit_payload(payload: dict) -> None:
    """Raise ValueError unless payload satisfies the activation audit schema."""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("audit schema_version is invalid")
    if not isinstance(payload.get("passed"), bool):
        raise ValueError("audit passed must be boolean")
    for key in (
        "checked_dates",
        "excluded_dates",
        "excluded_missing",
        "excluded_invalid",
        "violations",
        "source_coverage",
        "evidence",
    ):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"audit {key} must be a list")
    for key in ("counts", "limits", "maxima"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"audit {key} must be a mapping")
    checked = payload["checked_dates"]
    if checked != sorted(set(checked)):
        raise ValueError("checked_dates must be sorted and unique")
    if payload.get("simulation_only") is not True:
        raise ValueError("audit must remain simulation only")
    if payload.get("real_money_automation") is not False:
        raise ValueError("real_money_automation must be false")
    if payload.get("profitability_gate_applied") is not False:
        raise ValueError("profitability cannot gate activation")
    if payload["passed"] and (not checked or payload["violations"]):
        raise ValueError("passed audit requires checked dates and zero violations")
    if payload["passed"] != (bool(checked) and not payload["violations"]):
        raise ValueError("audit passed is inconsistent with mechanical evidence")
    coverage_dates = [row.get("date") for row in payload["source_coverage"] if isinstance(row, dict)]
    expected_dates = sorted(
        set(checked) | set(payload["excluded_missing"]) | set(payload["excluded_invalid"])
    )
    if sorted(coverage_dates) != expected_dates:
        raise ValueError("source coverage does not account for every requested date")


def _validate_safety_config(config: dict, violations: list[dict]) -> None:
    value = config.get("value_strategy", {}) if isinstance(config, dict) else {}
    account = config.get("simulation_account", {}) if isinstance(config, dict) else {}
    expected = {
        "stake_unit": (value.get("stake_unit"), 2),
        "max_match_exposure": (value.get("max_match_exposure"), 200),
        "max_daily_combo_stake": (value.get("max_daily_combo_stake"), 30),
        "max_daily_budget": (config.get("max_daily_budget") if isinstance(config, dict) else None, 500),
        "monthly_budget_cap": (account.get("monthly_budget_cap"), 5000),
    }
    for name, (actual, required) in expected.items():
        if _decimal(actual) != Decimal(required):
            _violate(
                violations,
                "unsafe_configuration",
                setting=name,
                value=actual,
                required=required,
            )
    if account.get("mode") != "simulation":
        _violate(violations, "simulation_mode")
    if account.get("real_money_automation") is not False:
        _violate(violations, "real_money_automation")


def _validate_paid_identity(row: dict) -> tuple[list[tuple[str, str]], list[str], int]:
    errors: list[tuple[str, str]] = []
    market_type = str(row.get("market_type") or "").strip().lower()
    play = str(row.get("play") or "").strip()
    source = str(row.get("odds_source") or "").strip().lower()
    if source not in DOMESTIC_ODDS_SOURCES:
        errors.append(("non_domestic_odds", "paid row source is not domestic"))
    if not _locked_price_evidence_valid(row):
        errors.append(("invalid_locked_price_evidence", "paid row lacks a valid pre-lock price"))

    if market_type == "parlay":
        if play != "PARLAY":
            errors.append(("forbidden_play", "parlay play label is invalid"))
        try:
            legs = json.loads(row.get("legs_json") or "")
        except (TypeError, json.JSONDecodeError):
            legs = None
        if not isinstance(legs, list) or len(legs) != 2:
            errors.append(("parlay_leg_count", "paid parlay must contain exactly two legs"))
            return errors, [], len(legs) if isinstance(legs, list) else 0
        match_ids = []
        combined_odds = Decimal("1")
        for leg_index, leg in enumerate(legs):
            if not isinstance(leg, dict):
                errors.append(("invalid_market_identity", f"parlay leg {leg_index} is invalid"))
                continue
            leg_source = str(leg.get("odds_source") or "").strip().lower()
            if leg_source not in DOMESTIC_ODDS_SOURCES:
                errors.append(("non_domestic_odds", f"parlay leg {leg_index} source is not domestic"))
            if not _locked_price_evidence_valid(leg, row.get("locked_at_bjt")):
                errors.append(("invalid_locked_price_evidence", f"parlay leg {leg_index} price is invalid"))
            match_id = _canonical_match_id(leg.get("match_id"))
            if match_id is None or not _valid_single_market(
                str(leg.get("market_type") or "").lower(),
                str(leg.get("selection") or ""),
                leg.get("line", ""),
            ):
                errors.append(("invalid_market_identity", f"parlay leg {leg_index} identity is invalid"))
            else:
                match_ids.append(match_id)
            odds = _decimal(leg.get("odds"))
            if odds is None or odds <= 1:
                errors.append(("invalid_locked_price_evidence", f"parlay leg {leg_index} odds are invalid"))
            else:
                combined_odds *= odds
        if len(set(match_ids)) != len(match_ids):
            errors.append(("invalid_market_identity", "parlay legs must use distinct matches"))
        locked_odds = _decimal(row.get("locked_odds"))
        display_odds = _decimal(row.get("odds"))
        if locked_odds != combined_odds or display_odds != combined_odds:
            errors.append(("parlay_locked_odds", "parlay price must equal exact leg product"))
        return errors, match_ids, len(legs)

    if play != ALLOWED_SINGLE_PLAYS.get(market_type):
        errors.append(("forbidden_play", "paid single play is unsupported"))
    match_id = _canonical_match_id(row.get("match_id"))
    if match_id is None or not _valid_single_market(
        market_type,
        str(row.get("selection") or ""),
        row.get("market_line", row.get("line", "")),
    ):
        errors.append(("invalid_market_identity", "paid single identity is invalid"))
        return errors, [], 0
    return errors, [match_id], 0


def _valid_single_market(market_type: str, selection: str, line: object) -> bool:
    line_text = "" if line is None else str(line).strip()
    if market_type == "had":
        return not line_text and selection in THREE_WAY_SELECTION_LABELS
    if market_type == "ttg":
        return not line_text and selection in TOTAL_GOALS_SELECTION_LABELS
    if market_type == "hhad":
        if selection not in THREE_WAY_SELECTION_LABELS:
            return False
        try:
            parse_handicap(line_text)
        except ValueError:
            return False
        return True
    return False


def _locked_price_evidence_valid(row: dict, lock_value: object = None) -> bool:
    source_record = row.get("odds_source_record_id")
    odds = _decimal(row.get("locked_odds", row.get("odds")))
    captured = _try_aware_datetime(row.get("odds_captured_at_bjt"))
    locked = _try_aware_datetime(lock_value or row.get("locked_at_bjt"))
    return (
        isinstance(source_record, str)
        and bool(source_record.strip())
        and odds is not None
        and odds > 1
        and captured is not None
        and locked is not None
        and captured <= locked
    )


def _calculate_maxima(
    valid_rows: dict[str, list[tuple[dict, Decimal, tuple[str, ...]]]]
) -> tuple[dict, list[dict]]:
    match_max = Decimal("0")
    parlay_max = Decimal("0")
    daily_max = Decimal("0")
    monthly_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    violations = []
    for report_date in sorted(valid_rows):
        match_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        parlay_total = Decimal("0")
        daily_total = Decimal("0")
        for row, stake, match_ids in valid_rows[report_date]:
            daily_total += stake
            if str(row.get("market_type") or "").lower() == "parlay":
                parlay_total += stake
            for match_id in match_ids:
                match_totals[match_id] += stake
        for match_id, exposure in sorted(match_totals.items()):
            match_max = max(match_max, exposure)
            if exposure > HARD_MATCH_EXPOSURE:
                _violate(
                    violations,
                    "match_exposure",
                    report_date=report_date,
                    match_id=match_id,
                    value=_json_number(exposure),
                    limit=200,
                )
        parlay_max = max(parlay_max, parlay_total)
        daily_max = max(daily_max, daily_total)
        if parlay_total > HARD_PARLAY_STAKE:
            _violate(
                violations,
                "parlay_stake",
                report_date=report_date,
                value=_json_number(parlay_total),
                limit=30,
            )
        if daily_total > HARD_DAILY_STAKE:
            _violate(
                violations,
                "daily_stake",
                report_date=report_date,
                value=_json_number(daily_total),
                limit=500,
            )
        try:
            month = date.fromisoformat(report_date).strftime("%Y-%m")
        except ValueError:
            _violate(violations, "report_date_invalid", report_date=report_date)
            continue
        monthly_totals[month] += daily_total
    monthly_max = max(monthly_totals.values(), default=Decimal("0"))
    for month, total in sorted(monthly_totals.items()):
        if total > HARD_MONTHLY_STAKE:
            _violate(
                violations,
                "monthly_stake",
                month=month,
                value=_json_number(total),
                limit=5000,
            )
    return {
        "match_exposure": _json_number(match_max),
        "parlay_stake": _json_number(parlay_max),
        "daily_stake": _json_number(daily_max),
        "monthly_stake": _json_number(monthly_max),
    }, violations


def _latest_valid_snapshot(
    paths: list[Path], target_date: date
) -> tuple[dict | None, Path | None, list[str]]:
    valid = []
    reasons = []
    for path in paths:
        try:
            payload = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("decision_snapshot_json_invalid")
            continue
        errors = _snapshot_errors(payload, target_date)
        if errors:
            reasons.extend(errors)
            continue
        valid.append((_aware_datetime(payload["captured_at"]), path, payload))
    if not valid:
        return None, None, sorted(set(reasons or ["decision_snapshot_invalid"]))
    _captured, path, payload = max(valid, key=lambda item: (item[0], item[1].name))
    payload = dict(payload)
    payload["_snapshot_record_id"] = f"data/odds_snapshots/{path.name}"
    return payload, path, []


def _snapshot_errors(payload: object, target_date: date) -> list[str]:
    if not isinstance(payload, dict):
        return ["decision_snapshot_not_mapping"]
    errors = []
    if payload.get("target_date") != target_date.isoformat():
        errors.append("decision_snapshot_date_invalid")
    if payload.get("capture_phase") != "decision":
        errors.append("decision_snapshot_phase_invalid")
    source = str(payload.get("source") or "").lower()
    if source not in DOMESTIC_ODDS_SOURCES:
        errors.append("decision_snapshot_source_invalid")
    captured = _try_aware_datetime(payload.get("captured_at"))
    if captured is None:
        errors.append("decision_snapshot_capture_invalid")
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return errors + ["decision_snapshot_matches_invalid"]
    seen = set()
    for index, row in enumerate(matches):
        if not isinstance(row, dict):
            errors.append(f"decision_snapshot_match_{index}_invalid")
            continue
        match_id = _canonical_match_id(row.get("match_id"))
        if match_id is None or match_id in seen:
            errors.append(f"decision_snapshot_match_{index}_identity_invalid")
        else:
            seen.add(match_id)
        if not all(isinstance(row.get(key), str) and row[key].strip() for key in ("team_a", "team_b")):
            errors.append(f"decision_snapshot_match_{index}_teams_invalid")
        kickoff = _try_match_datetime(row.get("kickoff_at"))
        if kickoff is None or captured is None or captured >= kickoff:
            errors.append(f"decision_snapshot_match_{index}_kickoff_invalid")
        markets = row.get("markets")
        if not isinstance(markets, dict) or any(
            key not in markets or not isinstance(markets.get(key), dict)
            for key in ("had", "hhad", "ttg")
        ) or not any(markets.get(key) for key in ("had", "hhad", "ttg")):
            errors.append(f"decision_snapshot_match_{index}_markets_invalid")
        eligibility = row.get("single_eligibility")
        if not isinstance(eligibility, dict) or any(
            not isinstance(eligibility.get(key), bool)
            for key in ("had", "hhad", "ttg")
        ):
            errors.append(f"decision_snapshot_match_{index}_eligibility_invalid")
    return errors


def _exclude_date(
    report_date: str,
    status: str,
    reasons: list[str],
    bucket: list[str],
    excluded_dates: list[dict],
    coverage: list[dict],
) -> None:
    bucket.append(report_date)
    excluded_dates.append(
        {"date": report_date, "status": status, "reasons": sorted(set(reasons))}
    )
    coverage.append(
        {
            "date": report_date,
            "status": status,
            "sporttery": False,
            "verified_domestic_fallback": False,
        }
    )


def _read_fixtures(path: Path) -> tuple[dict[str, list[dict]], str | None]:
    if not path.is_file():
        return {}, "missing"
    rows, error = _read_csv_rows(path)
    if error:
        return {}, "invalid"
    fixtures: dict[str, list[dict]] = defaultdict(list)
    for row in rows or []:
        report_date = str(row.get("date") or "")
        if report_date:
            fixtures[report_date].append(row)
    return dict(fixtures), None


def _read_csv_rows(path: Path) -> tuple[list[dict] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle)), None
    except (OSError, csv.Error, UnicodeError):
        return None, "invalid"


def _protected_hashes(root: Path) -> dict[str, str]:
    patterns = (
        "betting_plan_*.csv",
        "shadow_betting_plan_*.csv",
        "observation_plan_*.csv",
        "plan_lock_*.json*",
        "betting_ledger.csv",
        "observation_ledger.csv",
    )
    output = root / "output"
    paths = {path for pattern in patterns for path in output.glob(pattern) if path.is_file()}
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(paths)
    }


def _file_evidence(root: Path, path: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _canonical_match_id(value: object) -> str | None:
    return (
        value
        if isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and all(character.isprintable() and not character.isspace() for character in value)
        else None
    )


def _canonical_bet_id(row: dict) -> str | None:
    try:
        return stable_bet_id(row)
    except (KeyError, TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _try_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(BEIJING)


def _aware_datetime(value: object) -> datetime:
    parsed = _try_aware_datetime(value)
    if parsed is None:
        raise ValueError("timestamp must be aware ISO-8601")
    return parsed


def _try_match_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _json_number(value: Decimal):
    return int(value) if value == value.to_integral_value() else float(value)


def _violate(target: list[dict], code: str, **context) -> None:
    item = {"code": code}
    item.update({key: value for key, value in context.items() if value not in (None, "")})
    target.append(item)


def _sorted_violations(violations: list[dict]) -> list[dict]:
    return sorted(
        violations,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit shadow value-v4 portfolios for mechanical simulation safety."
    )
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--through", dest="through_date", required=True)
    args = parser.parse_args()
    try:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.through_date)
        payload = run_audit(ROOT, start, end)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "passed": payload["passed"],
                "checked_dates": payload["checked_dates"],
                "excluded_missing": payload["excluded_missing"],
                "excluded_invalid": payload["excluded_invalid"],
                "violations": len(payload["violations"]),
                "output": str(ROOT / "output" / OUTPUT_NAME),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
