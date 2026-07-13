"""Chronological champion/challenger learning for draw probabilities."""

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent
FEATURES = [
    "base_draw_probability",
    "market_draw_probability",
    "favorite_probability",
    "win_probability_gap",
    "xg_total",
    "favorite_movement",
    "regional_gap",
    "source_count",
    "is_knockout",
    "is_balanced",
]
SMALL_SAMPLE_FEATURES = FEATURES[:2]
SAMPLE_FIELDS = [
    "date",
    "match_id",
    "team_a",
    "team_b",
    "stage",
    "captured_at",
    "kickoff_at",
    "snapshot_path",
    "domestic_draw_odds",
    "closing_market_draw_probability",
    "outcome",
    *FEATURES,
]
ARTIFACT_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1
MIN_FULL_FEATURE_SAMPLES = 200
MIN_SHADOW_SAMPLES = 200
MIN_SHADOW_BETS = 100
TRAINING_INTERVAL_DAYS = 7
WEIGHT_HALF_LIFE_DAYS = 180


def chronological_splits(dates: list[date], n_splits: int):
    indices = list(range(len(dates)))
    for train, validation in TimeSeriesSplit(n_splits=n_splits).split(indices):
        yield list(train), list(validation)


def promotion_decision(challenger: dict, champion: dict) -> bool:
    return all((
        challenger.get("shadow_days", 0) >= 28,
        challenger.get("sample_count", 0) >= MIN_SHADOW_SAMPLES,
        challenger.get("bet_count", 0) >= MIN_SHADOW_BETS,
        challenger.get("brier_improvement", 0) >= 0.02,
        challenger.get("log_loss_improvement", 0) >= 0.02,
        challenger.get("brier_skill", 0) > 0,
        challenger.get("clv") is not None,
        (challenger.get("clv") or 0) > 0,
        challenger.get("roi", 0) > 0,
        challenger.get("max_drawdown", float("inf")) <= champion.get("max_drawdown", float("inf")),
    ))


def rollback_decision(current: dict, previous: dict) -> bool:
    for field in ("brier", "log_loss"):
        current_value = _number(current.get(field))
        previous_value = _number(previous.get(field))
        if current_value is None or previous_value is None:
            continue
        if previous_value == 0:
            if current_value > 0:
                return True
        elif current_value / previous_value >= 1.02 - 1e-12:
            return True
    return False


def league_pause_states(rows: list[dict]) -> dict:
    grouped = {}
    for row in rows:
        stage = str(row.get("stage") or "unknown")
        outcome = _binary_outcome(row.get("outcome"))
        probability = _number(row.get("model_draw_probability"))
        if outcome is None or probability is None or not 0 <= probability <= 1:
            continue
        grouped.setdefault(stage, []).append((row, outcome, probability))

    states = {}
    for stage, settled in grouped.items():
        settled.sort(key=lambda item: _league_sort_key(item[0]))
        stake = sum(_number(row.get("hypothetical_stake")) or 0.0 for row, _, _ in settled)
        profit = sum(_number(row.get("hypothetical_profit")) or 0.0 for row, _, _ in settled)
        roi = profit / stake if stake else 0.0
        previous_ten = settled[-20:-10]
        recent_ten = settled[-10:]
        previous_brier = _brier_from_tuples(previous_ten)
        recent_brier = _brier_from_tuples(recent_ten)
        paused = (
            len(settled) >= 30
            and roi < 0
            and previous_brier is not None
            and recent_brier is not None
            and recent_brier > previous_brier
        )
        states[stage] = {
            "paused": paused,
            "sample_count": len(settled),
            "roi": roi,
            "previous_ten_brier": previous_brier,
            "recent_ten_brier": recent_brier,
        }
    return states


def predict_draw_probability(features: dict, *, root: Path = ROOT) -> float:
    fallback = float(features["base_draw_probability"])
    root = Path(root)
    try:
        registry = _read_registry(root / "output" / "draw_model_registry.json")
        if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise ValueError("unsupported draw model registry schema")
        champion = registry.get("champion")
        if not isinstance(champion, dict):
            return fallback
        artifact = _load_registry_artifact(root, champion)
        values = [_required_feature_vector(features, artifact["feature_order"])]
        probability = float(artifact["model"].predict_proba(values)[0][1])
        if not math.isfinite(probability):
            return fallback
        return min(0.70, max(0.03, probability))
    except Exception:
        return fallback


def build_training_samples(root: Path = ROOT, as_of: date | None = None) -> list[dict]:
    root = Path(root)
    cutoff = as_of or date.today()
    results = _read_csv(root / "data" / "bet_results.csv")
    result_by_match = {
        (str(row.get("date", "")), str(row.get("team_a", "")), str(row.get("team_b", ""))): row
        for row in results
    }
    snapshots = {}
    snapshot_dir = root / "data" / "draw_feature_snapshots"
    for path in sorted(snapshot_dir.glob("*.json")) if snapshot_dir.exists() else []:
        snapshot = _valid_snapshot(path, root, cutoff)
        if snapshot is None:
            continue
        key = (
            snapshot["date"],
            snapshot["team_a"],
            snapshot["team_b"],
            snapshot["match_id"],
        )
        snapshots.setdefault(key, []).append(snapshot)

    samples = []
    for key, captures in snapshots.items():
        result = result_by_match.get(key[:3])
        if result is None:
            continue
        home_goals = _goal(result.get("home_goals"))
        away_goals = _goal(result.get("away_goals"))
        if home_goals is None or away_goals is None:
            continue
        captures.sort(key=lambda item: (item["captured_time"], item["snapshot_path"]))
        entry = captures[0]
        closing = captures[-1]
        row = {
            "date": entry["date"],
            "match_id": entry["match_id"],
            "team_a": entry["team_a"],
            "team_b": entry["team_b"],
            "stage": entry["stage"],
            "captured_at": entry["captured_at"],
            "kickoff_at": entry["kickoff_at"],
            "snapshot_path": entry["snapshot_path"],
            "domestic_draw_odds": entry["domestic_draw_odds"],
            "closing_market_draw_probability": closing["features"]["market_draw_probability"],
            "outcome": int(home_goals == away_goals),
        }
        row.update(entry["features"])
        samples.append(row)
    samples.sort(
        key=lambda row: (
            row["date"],
            row["captured_at"],
            row["match_id"],
            row["team_a"],
            row["team_b"],
        )
    )
    return samples


def update_draw_model(
    root: Path = ROOT,
    as_of: date | None = None,
    force_train: bool = False,
) -> Path:
    root = Path(root)
    current_date = as_of or date.today()
    model_dir = root / "data" / "models"
    output_dir = root / "output"
    model_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "draw_model_registry.json"
    original = _load_or_initialize_registry(registry_path)
    registry = copy.deepcopy(original)

    try:
        samples = build_training_samples(root, as_of=current_date)
        _atomic_write_csv(root / "data" / "draw_training_samples.csv", SAMPLE_FIELDS, samples)
        registry["per_league"] = league_pause_states(
            _read_csv(output_dir / "draw_alert_ledger.csv")
        )
        _rollback_if_needed(root, registry, samples, current_date)
        resolved_challenger = _advance_challenger(root, registry, samples, current_date)

        if (
            registry.get("challenger") is None
            and not resolved_challenger
            and _training_is_due(registry, current_date, force_train)
        ):
            artifact = _train_artifact(samples, as_of=current_date)
            challenger_path = model_dir / f"{artifact['metadata']['version']}.joblib"
            _persist_immutable_artifact(artifact, challenger_path)
            registry["challenger"] = _challenger_entry(
                artifact, root, challenger_path, current_date
            )
            registry["last_training_date"] = current_date.isoformat()
            registry["last_training_error"] = None
    except Exception as error:
        registry = copy.deepcopy(original)
        registry["last_training_error"] = f"{type(error).__name__}: {error}"

    registry["schema_version"] = REGISTRY_SCHEMA_VERSION
    registry["updated_at"] = current_date.isoformat()
    _atomic_write_json(registry_path, registry)
    return registry_path


def _train_artifact(rows: list[dict], as_of: date) -> dict:
    if len(rows) < 2:
        raise ValueError("at least two settled samples are required")
    feature_order = list(
        SMALL_SAMPLE_FEATURES if len(rows) < MIN_FULL_FEATURE_SAMPLES else FEATURES
    )
    model_kind = (
        "sigmoid_calibrator"
        if len(rows) < MIN_FULL_FEATURE_SAMPLES
        else "full_feature_logistic"
    )
    x_values = np.asarray(
        [[float(row[name]) for name in feature_order] for row in rows], dtype=float
    )
    outcomes = np.asarray([int(row["outcome"]) for row in rows], dtype=int)
    if len(set(outcomes.tolist())) < 2:
        raise ValueError("training samples must contain both draw and non-draw outcomes")
    weights = _sample_weights(rows, as_of)
    fold_metrics = _cross_validation_metrics(rows, x_values, outcomes, weights, model_kind)
    model = _new_model(model_kind)
    _fit_model(model, x_values, outcomes, weights, model_kind)
    digest_input = [
        [
            row.get("date"),
            row.get("match_id"),
            row.get("outcome"),
            *[row.get(name) for name in feature_order],
        ]
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    version = f"draw-{as_of.strftime('%Y%m%d')}-{digest}"
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "feature_order": feature_order,
        "metadata": {
            "version": version,
            "model_kind": model_kind,
            "trained_as_of": as_of.isoformat(),
            "sample_count": len(rows),
            "weight_half_life_days": WEIGHT_HALF_LIFE_DAYS,
            "fold_metrics": fold_metrics,
        },
        "model": model,
    }


def _cross_validation_metrics(rows, x_values, outcomes, weights, model_kind):
    if len(rows) < 3:
        return []
    metrics = []
    n_splits = min(5, len(rows) - 1)
    dates = [_parse_date(row["date"]) for row in rows]
    for fold, (train, validation) in enumerate(
        chronological_splits(dates, n_splits=n_splits), start=1
    ):
        if len(set(outcomes[train].tolist())) < 2:
            continue
        model = _new_model(model_kind)
        _fit_model(model, x_values[train], outcomes[train], weights[train], model_kind)
        probabilities = model.predict_proba(x_values[validation])[:, 1]
        market = np.asarray(
            [float(rows[index]["market_draw_probability"]) for index in validation]
        )
        actual = outcomes[validation]
        metrics.append({
            "fold": fold,
            "training_end": max(dates[index] for index in train).isoformat(),
            "validation_start": min(dates[index] for index in validation).isoformat(),
            "validation_count": len(validation),
            "brier": float(brier_score_loss(actual, probabilities)),
            "log_loss": float(log_loss(actual, probabilities, labels=[0, 1])),
            "market_brier": float(brier_score_loss(actual, market)),
            "market_log_loss": float(log_loss(actual, market, labels=[0, 1])),
        })
    return metrics


def _new_model(model_kind):
    logistic = LogisticRegression(C=0.5, max_iter=1000, random_state=42)
    if model_kind == "full_feature_logistic":
        return Pipeline(
            [("standardscaler", StandardScaler()), ("logisticregression", logistic)]
        )
    return logistic


def _fit_model(model, x_values, outcomes, weights, model_kind):
    if model_kind == "full_feature_logistic":
        model.fit(x_values, outcomes, logisticregression__sample_weight=weights)
    else:
        model.fit(x_values, outcomes, sample_weight=weights)


def _sample_weights(rows, as_of):
    decay = math.log(2) / WEIGHT_HALF_LIFE_DAYS
    return np.asarray([
        math.exp(-decay * max(0, (as_of - _parse_date(row["date"])).days))
        for row in rows
    ])


def _advance_challenger(root, registry, samples, current_date):
    challenger = registry.get("challenger")
    if not isinstance(challenger, dict):
        return False
    created_on = _parse_date(challenger.get("created_on"))
    if created_on is None:
        registry["last_training_error"] = "Active challenger has no valid creation date"
        return False
    challenger["shadow_days"] = max(0, (current_date - created_on).days)
    artifact = _load_registry_artifact(root, challenger)
    shadow_start = _challenger_start(challenger, created_on)
    shadow_samples = [
        row
        for row in samples
        if (_timestamp(row.get("captured_at")) or _date_start(_parse_date(row.get("date"))))
        > shadow_start
    ]
    reference_artifact = None
    champion = registry.get("champion")
    if isinstance(champion, dict):
        reference_artifact = _load_registry_artifact(root, champion)
    challenger.update(
        _shadow_metrics(
            artifact,
            shadow_samples,
            root,
            since=created_on,
            reference_artifact=reference_artifact,
        )
    )
    if challenger["shadow_days"] < 28:
        return False
    if (
        challenger.get("sample_count", 0) < MIN_SHADOW_SAMPLES
        or challenger.get("bet_count", 0) < MIN_SHADOW_BETS
    ):
        return False
    if promotion_decision(challenger, champion or {}):
        _promote_challenger(root, registry, artifact, challenger, current_date)
        return True
    registry["last_model_event"] = {
        "type": "rejection",
        "version": challenger.get("version"),
        "date": current_date.isoformat(),
    }
    registry["challenger"] = None
    return True


def _promote_challenger(root, registry, artifact, challenger, current_date):
    _validate_artifact(artifact, challenger)
    old_champion = copy.deepcopy(registry.get("champion"))
    registry["previous_champion"] = old_champion
    registry["champion"] = {
        **copy.deepcopy(challenger),
        "promoted_on": current_date.isoformat(),
    }
    registry["challenger"] = None
    registry["last_model_event"] = {
        "type": "promotion",
        "version": challenger.get("version"),
        "date": current_date.isoformat(),
    }


def _rollback_if_needed(root, registry, samples, current_date):
    champion = registry.get("champion")
    previous = registry.get("previous_champion")
    if not isinstance(champion, dict) or not isinstance(previous, dict) or len(samples) < 50:
        return
    current_artifact = _load_registry_artifact(root, champion)
    previous_artifact = _load_registry_artifact(root, previous)
    latest = samples[-50:]
    current_metrics = _artifact_metrics(current_artifact, latest)
    previous_metrics = _artifact_metrics(previous_artifact, latest)
    if not rollback_decision(current_metrics, previous_metrics):
        champion["recent_50"] = current_metrics
        previous["recent_50"] = previous_metrics
        return
    registry["champion"] = {
        **copy.deepcopy(previous),
        "recent_50": previous_metrics,
    }
    registry["previous_champion"] = {
        **copy.deepcopy(champion),
        "recent_50": current_metrics,
    }
    registry["last_model_event"] = {
        "type": "rollback",
        "from_version": champion.get("version"),
        "to_version": previous.get("version"),
        "date": current_date.isoformat(),
        "current_recent_50": current_metrics,
        "previous_recent_50": previous_metrics,
    }


def _challenger_entry(artifact, root, path, current_date):
    metadata = artifact["metadata"]
    return {
        "version": metadata["version"],
        "artifact": _relative_path(root, path),
        "feature_order": list(artifact["feature_order"]),
        "model_kind": metadata["model_kind"],
        "created_on": current_date.isoformat(),
        "created_at": f"{current_date.isoformat()}T23:59:59.999999+00:00",
        "shadow_days": 0,
        "sample_count": 0,
        "bet_count": 0,
        "fold_metrics": metadata["fold_metrics"],
        "brier_improvement": 0.0,
        "log_loss_improvement": 0.0,
        "brier_skill": 0.0,
        "clv": None,
        "roi": 0.0,
        "max_drawdown": 0.0,
    }


def _shadow_metrics(
    artifact,
    samples,
    root,
    since,
    reference_artifact=None,
):
    if reference_artifact is None:
        try:
            registry = _read_registry(Path(root) / "output" / "draw_model_registry.json")
            champion = registry.get("champion")
            if isinstance(champion, dict):
                reference_artifact = _load_registry_artifact(Path(root), champion)
        except Exception:
            reference_artifact = None
    probabilities = _artifact_probabilities(artifact, samples)
    outcomes = [int(row["outcome"]) for row in samples]
    reference_probabilities = (
        _artifact_probabilities(reference_artifact, samples)
        if reference_artifact is not None
        else [float(row["base_draw_probability"]) for row in samples]
    )
    market_probabilities = [float(row["market_draw_probability"]) for row in samples]
    model_metrics = _probability_metrics(outcomes, probabilities)
    reference_metrics = _probability_metrics(outcomes, reference_probabilities)
    market_metrics = _probability_metrics(outcomes, market_probabilities)

    config = _read_json(Path(root) / "betting_config.json", {}).get("draw_alert", {})
    minimum_probability = float(config.get("min_draw_probability", 0.27))
    minimum_edge = float(config.get("min_draw_edge", 0.04))
    minimum_ev = float(config.get("min_expected_value", 1.05))
    maximum_xg = float(config.get("max_xg_total", 2.5))
    stake = float(config.get("hypothetical_stake", 10))
    profits = []
    clv_values = []
    for row, probability in zip(samples, probabilities):
        market = _number(row.get("market_draw_probability"))
        odds = _number(row.get("domestic_draw_odds"))
        xg_total = _number(row.get("xg_total"))
        if market is None or odds is None or odds <= 1 or xg_total is None:
            continue
        if not (
            probability >= minimum_probability
            and probability - market >= minimum_edge
            and probability * odds >= minimum_ev
            and xg_total <= maximum_xg
        ):
            continue
        profits.append(stake * (odds - 1) if int(row["outcome"]) else -stake)
        closing = _number(row.get("closing_market_draw_probability"))
        if closing is not None:
            clv_values.append(closing - market)

    model_brier = model_metrics.get("brier")
    model_log_loss = model_metrics.get("log_loss")
    reference_brier = reference_metrics.get("brier")
    reference_log_loss = reference_metrics.get("log_loss")
    market_brier = market_metrics.get("brier")
    total_stake = stake * len(profits)
    return {
        **model_metrics,
        "sample_count": len(samples),
        "bet_count": len(profits),
        "reference_brier": reference_brier,
        "reference_log_loss": reference_log_loss,
        "market_brier": market_brier,
        "brier_improvement": _relative_improvement(reference_brier, model_brier),
        "log_loss_improvement": _relative_improvement(reference_log_loss, model_log_loss),
        "brier_skill": _relative_improvement(market_brier, model_brier),
        "clv": sum(clv_values) / len(clv_values) if clv_values else None,
        "roi": sum(profits) / total_stake if total_stake else 0.0,
        "max_drawdown": _max_drawdown(profits),
    }


def _artifact_metrics(artifact, rows):
    probabilities = _artifact_probabilities(artifact, rows)
    outcomes = [int(row["outcome"]) for row in rows]
    return _probability_metrics(outcomes, probabilities)


def _artifact_probabilities(artifact, rows):
    if not rows:
        return []
    values = [
        _required_feature_vector(row, artifact["feature_order"])
        for row in rows
    ]
    probabilities = artifact["model"].predict_proba(values)
    return [min(0.70, max(0.03, float(row[1]))) for row in probabilities]


def _probability_metrics(outcomes, probabilities):
    if not outcomes:
        return {}
    return {
        "brier": float(brier_score_loss(outcomes, probabilities)),
        "log_loss": float(log_loss(outcomes, probabilities, labels=[0, 1])),
    }


def _relative_improvement(reference, candidate):
    if reference is None or candidate is None or reference <= 0:
        return 0.0
    return (reference - candidate) / reference


def _training_is_due(registry, current_date, force_train):
    if force_train:
        return True
    last_training = _parse_date(registry.get("last_training_date"))
    return last_training is None or (current_date - last_training).days >= TRAINING_INTERVAL_DAYS


def _load_or_initialize_registry(path):
    if not path.exists():
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "champion": None,
            "challenger": None,
            "previous_champion": None,
            "per_league": {},
            "last_training_date": None,
            "last_training_error": None,
        }
    registry = _read_registry(path)
    if not isinstance(registry, dict):
        raise ValueError("draw model registry must be a JSON object")
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported draw model registry schema")
    registry.setdefault("champion", None)
    registry.setdefault("challenger", None)
    registry.setdefault("previous_champion", None)
    registry.setdefault("per_league", {})
    registry.setdefault("last_training_date", None)
    registry.setdefault("last_training_error", None)
    return registry


def _persist_immutable_artifact(artifact, path):
    path = Path(path)
    entry = {
        "version": artifact["metadata"]["version"],
        "artifact": path.name,
        "feature_order": artifact["feature_order"],
        "model_kind": artifact["metadata"]["model_kind"],
    }
    if path.exists():
        _validate_artifact(_load_artifact(path), entry)
        return
    temporary = _temporary_path(path)
    try:
        joblib.dump(artifact, temporary)
        _validate_artifact(_load_artifact(temporary), entry)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    _validate_artifact(_load_artifact(path), entry)


def _atomic_dump_artifact(artifact, path):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        joblib.dump(artifact, temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path, payload):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_csv(path, fieldnames, rows):
    path = Path(path)
    temporary = _temporary_path(path)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_path(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    return Path(name)


def _load_registry_artifact(root, entry):
    path = _registry_artifact_path(root, entry)
    artifact = _load_artifact(path)
    _validate_artifact(artifact, entry)
    return artifact


def _load_artifact(path):
    return joblib.load(path)


def _validate_artifact(artifact, entry=None):
    if not isinstance(artifact, dict):
        raise ValueError("model artifact must be a dictionary")
    if artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported model artifact schema")
    feature_order = artifact.get("feature_order")
    if feature_order == SMALL_SAMPLE_FEATURES:
        expected_kind = "sigmoid_calibrator"
    elif feature_order == FEATURES:
        expected_kind = "full_feature_logistic"
    else:
        raise ValueError("model artifact feature order is not allowed")
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("version"):
        raise ValueError("model artifact has no version metadata")
    if metadata.get("model_kind") != expected_kind:
        raise ValueError("model artifact kind does not match its feature order")
    model = artifact.get("model")
    if not callable(getattr(model, "predict_proba", None)):
        raise ValueError("model artifact cannot predict probabilities")
    feature_count = getattr(model, "n_features_in_", None)
    if feature_count is None or int(feature_count) != len(feature_order):
        raise ValueError("model artifact feature count does not match feature order")
    if entry is not None:
        if entry.get("version") != metadata["version"]:
            raise ValueError("registry and artifact versions differ")
        if entry.get("feature_order") != feature_order:
            raise ValueError("registry and artifact feature orders differ")
        if entry.get("model_kind") != expected_kind:
            raise ValueError("registry and artifact model kinds differ")
    return artifact


def _registry_artifact_path(root, entry):
    raw = entry.get("artifact")
    if not isinstance(raw, str) or not raw:
        raise ValueError("registry artifact path is missing")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("registry artifact path must be relative and non-escaping")
    model_root = (Path(root) / "data" / "models").resolve()
    candidate = (Path(root) / relative).resolve(strict=True)
    try:
        candidate.relative_to(model_root)
    except ValueError as error:
        raise ValueError("registry artifact path escapes data/models") from error
    if candidate == model_root or candidate.suffix != ".joblib":
        raise ValueError("registry artifact path must name a joblib file")
    return candidate


def _required_feature_vector(features, feature_order):
    values = []
    for name in feature_order:
        if name not in features:
            raise ValueError(f"required model feature is missing: {name}")
        value = _number(features[name])
        if value is None:
            raise ValueError(f"required model feature is invalid: {name}")
        values.append(value)
    return values


def _valid_snapshot(path, root, cutoff):
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return None
        if payload.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
            return None
        target_date = _parse_date(payload.get("date"))
        captured_at = _timestamp(payload.get("captured_at"))
        kickoff_at = _timestamp(payload.get("kickoff_at"))
        if (
            target_date is None
            or target_date > cutoff
            or captured_at is None
            or kickoff_at is None
            or captured_at > kickoff_at
        ):
            return None
        identity = [str(payload.get(name) or "") for name in ("match_id", "team_a", "team_b")]
        if any(not value for value in identity):
            return None
        features = payload.get("features")
        if not isinstance(features, dict):
            return None
        normalized_features = {
            name: _number(features.get(name)) for name in FEATURES
        }
        if any(value is None for value in normalized_features.values()):
            return None
        odds = _number(payload.get("domestic_draw_odds"))
        if odds is None or odds <= 1:
            return None
        return {
            "date": target_date.isoformat(),
            "match_id": identity[0],
            "team_a": identity[1],
            "team_b": identity[2],
            "stage": str(payload.get("stage") or ""),
            "captured_at": payload["captured_at"],
            "kickoff_at": payload["kickoff_at"],
            "captured_time": captured_at,
            "domestic_draw_odds": odds,
            "features": normalized_features,
            "snapshot_path": path.relative_to(root).as_posix(),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _challenger_start(challenger, created_on):
    return _timestamp(challenger.get("created_at")) or _date_start(
        created_on + timedelta(days=1)
    )


def _date_start(value):
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _league_sort_key(row):
    target_date = _parse_date(row.get("date"))
    captured = _timestamp(row.get("captured_at"))
    return (
        _date_start(target_date),
        captured or _date_start(target_date),
        str(row.get("match_id") or ""),
        str(row.get("team_a") or ""),
        str(row.get("team_b") or ""),
    )


def _relative_path(root, path):
    return Path(path).relative_to(root).as_posix()


def _parse_date(value):
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _goal(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and str(value).strip() == str(number) else None


def _binary_outcome(value):
    number = _number(value)
    return int(number) if number in (0.0, 1.0) else None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _brier_from_tuples(rows):
    if not rows:
        return None
    return sum((probability - outcome) ** 2 for _, outcome, probability in rows) / len(rows)


def _max_drawdown(profits):
    cumulative = peak = drawdown = 0.0
    for profit in profits:
        cumulative += profit
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_registry(path):
    registry = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(registry, dict):
        raise ValueError("draw model registry must be a JSON object")
    return registry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", action="store_true", help="update the draw model registry")
    parser.add_argument("--date", help="training cutoff in YYYY-MM-DD format")
    parser.add_argument("--force", action="store_true", help="ignore the weekly training interval")
    arguments = parser.parse_args(argv)
    if not arguments.train:
        parser.error("--train is required")
    try:
        target_date = date.fromisoformat(arguments.date) if arguments.date else None
        path = update_draw_model(as_of=target_date, force_train=arguments.force)
        print(path)
        return 0
    except Exception as error:
        print(f"draw model orchestration failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
