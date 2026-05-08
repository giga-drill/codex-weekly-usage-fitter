# Codex Usage Execution History

Last updated: 2026-05-08

This file archives completed execution updates. It is for traceability, not for
routine executor startup.

## 2026-05-08 - Turn Count Estimation Policy

Executor audited turn semantics and locked them with regression tests.

Completed:

- Confirmed turn logic counts only `token_delta > 0` in:
  - movement events (`usage_movement_events.turn_count`);
  - billing period/day/week rollups;
  - model/effort turns-per-1% fits.
- Added tests:
  - `test_clean_fit_turn_count_uses_positive_token_delta_intervals`
  - `test_billing_stats_turn_count_ignores_zero_delta_samples`
- Updated README (EN + ZH) to define a user-visible turn as a positive
  `token_delta` interval and clarify baseline/zero-delta behavior.

Validation:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Result: 27 tests passed.

## 2026-05-08 - User-Visible Token Delta Policy

Executor implemented the user-visible token delta policy.

Completed:

- `src/codex_usage/store.py`
  - `status()` returns a sanitized public `latest_sample` that omits `last_*`
    fields.
  - `today_usage()` switched from `last_turn_token_total` to
    `latest_turn_token_delta`.
- `macos/CodexUsageWidget.swift`
  - Main widget `Last turn ... tokens` uses latest sample `token_delta`.
  - Stats day/sample rows use `token_delta`.
- `README.md`
  - Added EN + ZH wording that "last turn tokens" uses `samples.token_delta`
    rather than transcript sub-step counters.
- `tests/test_store.py`
  - Added assertions that public status sample omits `last_total_tokens`.
  - Updated today-usage assertions to `latest_turn_token_delta`.

Validation:

- Python test suite: 25 tests passed.
- Widget build/package smoke succeeded.

Note:

- Raw exports (`export jsonl/csv`) still include internal `last_*` columns for
  low-level debugging.

## 2026-05-08 - macOS Stats Panel Alignment

Executor aligned the macOS stats panel with backend movement-event semantics.

Completed:

- `macos/CodexUsageWidget.swift` reads mixed movement observations from
  `usage_movement_events` for the selected billing period.
- Added mixed movement aggregation by combination in Swift.
- Stats panel summary shows:
  - clean 1% estimate for the latest model/reasoning effort;
  - mixed observation headline for the selected period.
- Stats panel outline appends `Mixed movement observations`:
  - combination aggregate rows;
  - per-event breakdown rows.

Validation:

- `scripts/build-widget.sh` succeeded.
- Python test suite: 25 tests passed.

## 2026-05-08 - Backend Movement Event Attribution

Executor implemented backend movement-event attribution and switched
model/effort fits to clean events only.

Completed:

- Added `usage_movement_events` table in `src/codex_usage/store.py`.
- Rebuilt fit pipeline to:
  - create one movement event on each positive `weekly_used_percent` jump;
  - keep mixed events as bucket combinations;
  - avoid token-proportional splits;
  - derive `model_effort_fits` and `model_effort_global_fits` from clean events
    only.
- Added status payload fields:
  - `latest_model_effort_key`
  - `latest_clean_model_effort_fit`
  - `latest_mixed_movement_events`
- Added billing-period payload fields:
  - `mixed_movement_events`
  - `mixed_movement_combinations`
- Updated CLI output:
  - `status` highlights latest clean estimate for current model/reasoning
    effort.
  - `billing-stats` prints mixed movement combinations and per-event details in
    `--debug`.
- Replaced/added store tests for attribution logic.

Validation:

- Python test suite: 25 tests passed.
- `billing-stats --json` and `--debug` smoke-tested.

Follow-up completed later:

- macOS widget stats panel now consumes/reflects clean estimates and mixed
  observations.

## 2026-05-08 - Planner Initial Handoff

Planner created the first handoff documents after reading the repo and related
session context.

Initial handoff established:

- `AGENTS.md` as the short auto-loaded rule file.
- `docs.md` as the manual handoff entrypoint.
- Current project summary, dirty files, WIP billing stats direction, commands,
  and executor checklist.

That single-file handoff later grew too large and was split into:

- `docs.md`
- `docs/decisions.md`
- `docs/history.md`
