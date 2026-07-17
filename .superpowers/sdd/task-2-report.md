# Phase 2 Task 2 Report

## Files Changed

- `import_sporttery.py`: added explicit per-market single-play normalization and CSV columns for HAD, HHAD, and TTG eligibility.
- `capture_odds_snapshot.py`: added injectable snapshot inputs, fresh odds-file loading, complete decision markets, per-market eligibility, match IDs, and explicit empty-list snapshots.
- `tests/test_official_market_import.py`: added focused import and fallback-eligibility coverage.
- `tests/test_value_strategy.py`: added focused decision-snapshot coverage while retaining existing value-strategy tests.

## RED Evidence

Command:

```text
.superpowers/sdd/runtime/verify-venv/Scripts/python.exe -m unittest tests.test_official_market_import tests.test_value_strategy -v
```

Result: expected failure, exit code 1. Five new tests failed because `fixtures.csv` lacked `is_single_hhad` and `is_single_ttg`, and `capture()` did not accept injected `matches` or `odds_by_match` inputs. The pre-existing five value-strategy tests passed.

## GREEN Result

The same focused command passed: 10 tests, exit code 0.

Relevant compatibility verification also passed:

- `tests.test_capture_odds_snapshot`: 10 tests, exit code 0.
- `tests.test_collect_market_heat tests.test_import_sporttery tests.test_report_status`: 79 tests, exit code 0 with `OPENBLAS_NUM_THREADS=1`.
- `py_compile` for both production modules and both focused test modules: exit code 0.
- `git diff --check`: exit code 0.

## Commit

Implementation commit: `394e305` (`feat: capture eligible sporttery markets at decision time`).

## Self-Review

- Eligibility is true only for literal `True` or normalized `true`, `1`, and `yes` strings; odds presence never implies eligibility.
- The ZGZCW `dg` marker remains HAD-only, leaving HHAD and TTG false unless explicitly supplied.
- Legacy flat snapshot odds are retained for Phase 1 consumers; the new `markets` and `single_eligibility` fields are additive.
- A started-only match list preserves prior no-artifact behavior, while an explicitly empty official list produces a valid zero-match snapshot.

## Concerns

No code concerns found. The combined broader test invocation initially failed before discovery because OpenBLAS could not allocate its default worker threads; rerunning the affected modules with `OPENBLAS_NUM_THREADS=1` passed all 79 tests.
