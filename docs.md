# Codex Usage Handoff

Last updated: 2026-05-08

This is the short handoff entrypoint for planner and executor sessions.
`AGENTS.md` stays tiny because Codex auto-loads it. Read this file first, then
open the linked docs only when the current task needs them.

## Read Order

1. `AGENTS.md` for always-on repo rules.
2. This `docs.md` for current state and next actions.
3. `docs/decisions.md` when changing product semantics, fit logic, token
   accounting, turn counting, or widget/stat output.
4. `docs/usage-profiler-plan.md` when implementing the usage profiler.
5. `docs/history.md` only when you need completed execution details.

## Project Summary

This repo builds Codex Weekly Usage Fitter, a local Codex usage monitor.

It records token usage from local Codex conversations, observes Codex weekly
usage percentage samples, and estimates how many tokens or user-visible turns
correspond to 1% of weekly usage.

The project is local-first:

- Python stdlib-only CLI and daemon.
- SQLite state under `~/.codex/usage-monitor/usage.sqlite`.
- Local transcript parsing plus Codex app-server fallback.
- Optional native macOS floating widget.
- No transcript upload.

Public repo: `https://github.com/giga-drill/codex-weekly-usage-fitter`

## Current Repo State

- Branch: `main`, tracking `origin/main`, currently ahead by 1 commit.
- Working tree is dirty. Do not revert current changes unless the user asks.
- Current dirty files include README, widget, CLI/store/tests, and docs.
- The repo path contains a space: `/Users/mac/projs/codex usage`.

## Current Status

Executor has implemented the major semantic work:

- backend movement-event attribution;
- clean-only model/effort fits;
- mixed movement observations;
- macOS stats panel mixed-observation display;
- user-visible token delta policy;
- turn count estimation policy.

Latest known validation from executor:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Executor reported 27 passing tests after the turn-count audit.

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
- A user-visible turn is a positive sample-to-sample `token_delta` interval.
- Mixed model/effort usage movement is reported as a combination, not split by
  token share.

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

1. For profiler work, read `docs/usage-profiler-plan.md`.
2. Before semantic edits, read `docs/decisions.md`.
3. Preserve the token delta and turn-count semantics in all user-visible output.
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
