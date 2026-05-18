# Codex Usage Handoff

Last updated: 2026-05-18

This is the short handoff entrypoint for planner and executor sessions.
`AGENTS.md` stays tiny because Codex auto-loads it. Read this file first, then
open the linked docs only when the current task needs them.

## Read Order

1. `AGENTS.md` for always-on repo rules.
2. This `docs.md` for current state and next actions.
3. `docs/decisions.md` when changing product semantics, fit logic, token
   accounting, turn counting, or widget/stat output.
4. `docs/conversation-turn-fix-plan.md` before changing turn/count semantics.
5. `docs/usage-profiler-plan.md` when implementing the usage profiler.
6. `docs/history.md` only when you need completed execution details.

## Project Summary

This repo builds Codex Weekly Usage Fitter, a local Codex usage monitor.

It records token usage from local Codex conversations, observes Codex weekly
usage percentage samples, and estimates tokens or completed conversation turns
per 1% of weekly usage.

The project is local-first:

- Python stdlib-only CLI and daemon.
- SQLite state under `~/.codex/usage-monitor/usage.sqlite`.
- Local transcript parsing plus Codex app-server fallback.
- Optional native macOS floating widget.
- No transcript upload.

Public repo: `https://github.com/giga-drill/codex-weekly-usage-fitter`

## Current Repo State

- Current c-orch worktree is on detached HEAD at `f4abacf` (same commit as `main`).
- `main` tracks `origin/main` and is currently ahead by 4 commits.
- Working tree is clean.
- The repo path contains a space: `/Users/mac/projs/codex usage`.

## Current Status

Conversation-turn migration blockers from planner review are now fixed:

- `weekly_percent_delta` for `conversation_turns` is now computed against
  global weekly usage progression (by reset window), not per-session;
- top-level epoch/fit rebuild prefers `conversation_turns` and falls back to
  raw `samples` only when aggregate turns are unavailable;
- init/backfill order now ensures `conversation_turns` before fit/movement
  rebuild checks, so reopening an old DB backfills and rebuilds dependents;
- transcript conversation-turn parsing now exposes internal-turn token delta
  breakdown (`internal_token_deltas`) for reconciliation.
- opening an existing DB now detects stale `conversation_turns` rows (for
  example missing `internal_token_deltas_json`, token-delta mismatch, or
  legacy weekly percent deltas) and triggers a full rebuild of
  `conversation_turns` plus dependent epochs/fits/movement events.
- weekly movement now uses reset-window high-water accounting consistently for
  both stored `conversation_turns.weekly_percent_delta` and
  `usage_movement_events`.
- `conversation_turns.token_delta` now follows the sum of internal turn deltas
  (including the first completed conversation turn), instead of forcing the
  first turn to zero.

CLI status raw row is now clearly labeled as internal:
`Latest raw sample (internal) ...`.

Latest validation state:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Current result: 39 passing tests (revalidated on 2026-05-18).

## Active Notes

Widget runtime note:

- Rebuilding `build/Codex Usage.app` does not update an already-running widget
  process.
- If the UI still shows old token semantics, quit and reopen the widget.
- The observed mismatch where the UI still showed a `last_total_tokens`-like
  value was likely caused by an old running app process, not the current source.

Hook runtime note:

- On 2026-05-08, Codex Desktop 0.129 stopped invoking the global lifecycle
  hooks even after restart, while transcripts still contained `token_count`.
- The daemon now scans recent `~/.codex/sessions` transcripts every 10 seconds,
  so forked/planner sessions are recorded even when hooks do not fire.
- Hook config remains installed as a fast path; `scripts/codex-usage-hook.sh`
  logs successful invocations when Codex starts honoring hooks again.

Product semantics to preserve:

- User-visible token consumption uses `token_delta`, not `last_total_tokens`.
- User-visible turn/count language should become `conversation turn`.
- Mixed model/effort usage movement is reported as a combination, not split by
  token share.
- `samples` remains the raw observation layer; `conversation_turns` is the
  user-facing aggregate layer in progress.

See `docs/decisions.md` for the full stable rules.

## Common Commands

Use these from `/Users/mac/projs/codex usage`:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m codex_usage status
PYTHONPATH=src python3 -m codex_usage billing-stats --billing-day 12 --period current --debug
PYTHONPATH=src python3 -m codex_usage billing-stats --billing-day 12 --period previous --json
scripts/package-widget-app.sh
open "build/Codex Usage.app"
```

## Next Executor Checklist

1. Verify live widget behavior after restart:
   compact/expanded view plus stats panel values should match
   `conversation_turns`-based CLI output.
2. Continue compatibility cleanup in CLI/JSON naming:
   keep aliases but prefer explicit `conversation_turn_*` keys.
3. Review mixed-movement zero-token events from real data and decide whether
   they should be surfaced, filtered, or marked as external-only.
4. If touching widget behavior, rebuild and restart the running app before
   judging the UI.
5. Run the Python test suite after backend changes.

## Maintenance Rules

- Keep this file under about 120 lines.
- Keep only active/pending context here.
- Move completed execution notes to `docs/history.md`.
- Move stable product or architecture rules to `docs/decisions.md`.
- Delete details from this file once they are captured in code, tests, README,
  or the split docs.
