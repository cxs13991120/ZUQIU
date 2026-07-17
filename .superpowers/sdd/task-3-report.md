# Phase 2 Task 3 Report

## Files Changed

- `value_candidates.py`: added the immutable candidate and odds-risk models plus candidate construction for official HAD, HHAD, and TTG markets.
- `tests/test_value_candidates.py`: added focused coverage for probability layers, eligibility separation, identity and quality rejection, volatility controls, and domestic odds preservation.

## RED Evidence

Initial command:

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_value_candidates -v
```

Result: exit code 1 with the expected `ModuleNotFoundError: No module named 'value_candidates'`.

Two later regression cycles also observed expected RED failures before their minimal fixes:

- An externally constructed non-domestic `OfficialMarket` was accepted despite matching prices.
- A mismatched opening snapshot incorrectly upgraded direct official odds to `high` quality.

## GREEN Result

```text
$env:OPENBLAS_NUM_THREADS='1'; .\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m unittest tests.test_official_markets tests.test_value_candidates -v
Ran 21 tests in 0.004s
OK
```

Also passed:

```text
.\.superpowers\sdd\runtime\verify-venv\Scripts\python.exe -m py_compile value_candidates.py tests\test_value_candidates.py
git diff --check
```

## Commit

Implementation commit: `1db2fe3` (`feat: build unified positive-value candidate pool`).

## Self-Review

- `ValueCandidate` is frozen and contains the resolved `paid_eligible`, `value_gate_reasons`, `calibration_samples`, and `performance_multiplier=1.0` fields.
- `paid_eligible` only records probability-edge and EV gates; it stays independent from Task 2's official `single_eligible` flag and no stake or budget data is calculated.
- Official prices come only from trusted domestic `OfficialMarket` objects and match the decision snapshot's exact match ID, identity, market, line, and prices. External consensus data is ignored.
- HAD retains raw, calibrated, market, and conservative probabilities separately; league calibration applies only to draws. HHAD and TTG use the Task 1 Poisson helpers.
- Started, unsupported, malformed, identity-conflicting, missing-decision, non-domestic, and unverified-jump markets are excluded. Missing or mismatched opening evidence is medium quality rather than high.

## Concerns

No code concerns found. Callers that want `high` quality must supply a same-identity opening record through `snapshot["opening_matches"]` (or `snapshot["opening"]["matches"]`); the Task 2 decision-only payload remains valid and intentionally produces `medium` quality.
