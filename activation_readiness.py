"""Fail-closed coupling between active generation and its persisted safety audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


AUDIT_SCHEMA_VERSION = "shadow-portfolio-activation-audit-v1"
AUDIT_OUTPUT_NAME = "shadow_portfolio_activation_audit.json"


def activation_config_digest(config: dict) -> str:
    """Hash all audited configuration except the shadow/active routing switch."""
    if not isinstance(config, dict):
        raise ValueError("activation configuration must be a mapping")
    try:
        normalized = json.loads(json.dumps(config, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("activation configuration is not JSON serializable") from exc
    value = normalized.get("value_strategy")
    if isinstance(value, dict):
        value.pop("activation_mode", None)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_activation_payload(payload: dict) -> None:
    """Validate the persisted mechanical gate without importing generation."""
    if not isinstance(payload, dict) or payload.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise ValueError("activation audit schema is invalid")
    if not isinstance(payload.get("passed"), bool):
        raise ValueError("activation audit passed flag is invalid")
    list_keys = (
        "checked_dates",
        "excluded_dates",
        "excluded_missing",
        "excluded_invalid",
        "violations",
        "source_coverage",
        "evidence",
    )
    if any(not isinstance(payload.get(key), list) for key in list_keys):
        raise ValueError("activation audit list fields are invalid")
    if any(
        not isinstance(payload.get(key), dict)
        for key in ("counts", "limits", "maxima")
    ):
        raise ValueError("activation audit mapping fields are invalid")
    checked = payload["checked_dates"]
    if checked != sorted(set(checked)):
        raise ValueError("activation audit checked dates are invalid")
    if payload.get("simulation_only") is not True:
        raise ValueError("activation audit is not simulation only")
    if payload.get("real_money_automation") is not False:
        raise ValueError("activation audit permits real-money automation")
    if payload.get("profitability_gate_applied") is not False:
        raise ValueError("activation audit applies a forbidden profitability gate")
    if payload.get("historical_artifacts_unchanged") is not True:
        raise ValueError("activation audit did not preserve historical artifacts")
    if payload["passed"] != (bool(checked) and not payload["violations"]):
        raise ValueError("activation audit passed flag is inconsistent")
    digest = payload.get("rebuild_config_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("activation audit configuration digest is invalid")
    coverage_dates = [
        item.get("date")
        for item in payload["source_coverage"]
        if isinstance(item, dict)
    ]
    expected_dates = sorted(
        set(checked)
        | set(payload["excluded_missing"])
        | set(payload["excluded_invalid"])
    )
    if sorted(coverage_dates) != expected_dates:
        raise ValueError("activation audit source coverage is invalid")
    evidence_dates = [
        item.get("date") for item in payload["evidence"] if isinstance(item, dict)
    ]
    if evidence_dates != checked:
        raise ValueError("activation audit evidence dates are invalid")


def assert_activation_ready(
    root: Path,
    *,
    audit_path: Path | None = None,
    config: dict | None = None,
) -> dict:
    """Return a verified audit or raise before production active routing."""
    root = Path(root).resolve()
    audit_path = (
        root / "output" / AUDIT_OUTPUT_NAME
        if audit_path is None
        else Path(audit_path).resolve()
    )
    _require_within_root(root, audit_path)
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("activation audit is missing or invalid") from exc
    validate_activation_payload(payload)
    if config is None:
        try:
            config = json.loads(
                (root / "betting_config.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("activation configuration is missing or invalid") from exc

    checked = payload["checked_dates"]
    if payload.get("passed") is not True:
        raise ValueError("activation audit has not passed")
    if not isinstance(checked, list) or not checked or checked != sorted(set(checked)):
        raise ValueError("activation audit has no checked dates")
    if payload["violations"] != []:
        raise ValueError("activation audit contains violations")
    if payload.get("simulation_only") is not True:
        raise ValueError("activation audit is not simulation only")
    if payload.get("real_money_automation") is not False:
        raise ValueError("activation audit permits real-money automation")
    if payload.get("profitability_gate_applied") is not False:
        raise ValueError("activation audit applies a forbidden profitability gate")
    if payload.get("historical_artifacts_unchanged") is not True:
        raise ValueError("activation audit did not preserve historical artifacts")

    account = config.get("simulation_account", {}) if isinstance(config, dict) else {}
    if account.get("mode") != "simulation":
        raise ValueError("active routing requires simulation mode")
    if account.get("real_money_automation") is not False:
        raise ValueError("active routing forbids real-money automation")
    if payload.get("rebuild_config_sha256") != activation_config_digest(config):
        raise ValueError("activation configuration differs from audited reconstruction")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("activation audit evidence is invalid")
    if [item.get("date") for item in evidence if isinstance(item, dict)] != checked:
        raise ValueError("activation audit evidence dates are stale")
    required = {"snapshot", "predictions", "domestic_odds", "fixtures_file"}
    for item in evidence:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("activation audit evidence is incomplete")
        if int(item.get("candidate_count") or 0) <= 0:
            raise ValueError("activation audit candidate reconstruction is unproven")
        if int(item.get("observation_count") or 0) <= 0:
            raise ValueError("activation audit observation reconstruction is unproven")

    records = list(_file_records(evidence))
    if not records:
        raise ValueError("activation audit has no hashed evidence")
    for record in records:
        _verify_file_record(root, record)
    return payload


def _file_records(value):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            yield value
        for child in value.values():
            yield from _file_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _file_records(child)


def _verify_file_record(root: Path, record: dict) -> None:
    relative = Path(record["path"])
    if relative.is_absolute():
        raise ValueError("activation audit evidence path is absolute")
    path = (root / relative).resolve()
    _require_within_root(root, path)
    if record.get("exists") is False:
        if path.exists():
            raise ValueError(f"activation audit evidence appeared: {record['path']}")
        return
    expected_hash = record.get("sha256")
    expected_bytes = record.get("bytes")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or not path.is_file()
    ):
        raise ValueError(f"activation audit evidence is invalid: {record['path']}")
    if path.stat().st_size != expected_bytes or _sha256(path) != expected_hash:
        raise ValueError(f"activation audit evidence hash mismatch: {record['path']}")


def _require_within_root(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("activation audit evidence escapes repository root") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()
