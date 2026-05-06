# Codex Weekly Usage Fitter

Local account-level sampler for estimating the relationship between Codex
session token deltas and the weekly Codex usage percentage.

The tool does not patch Codex itself. It uses:

- Codex `Stop` hooks for per-turn sample points.
- Local session transcripts for token counts and raw weekly rate limit values.
- `codex app-server` `account/rateLimits/read` as a fallback when transcripts
  do not contain a weekly usage sample.
- SQLite under `~/.codex/usage-monitor/usage.sqlite`.

## Commands

Run from this repo without installing:

```bash
PYTHONPATH=src python3 -m codex_usage status
PYTHONPATH=src python3 -m codex_usage daemon
PYTHONPATH=src python3 -m codex_usage export
PYTHONPATH=src python3 -m codex_usage hook-config
```

Install as a local CLI:

```bash
python3 -m pip install -e .
codex-usage status
```

## Hook Setup

Print the config snippet:

```bash
codex-usage hook-config
```

Add the printed snippet to `~/.codex/config.toml`. The hook command is designed
to return quickly. If the daemon is not online, it writes a small spool file to
`~/.codex/usage-monitor/spool/`.

## Data Model

The SQLite database contains:

- `sessions`: latest seen total tokens per local Codex session.
- `samples`: per-turn hook samples, including token delta and weekly usage.
- `epochs`: weekly reset buckets.
- `fits`: current token-per-weekly-percent estimate per epoch.

The first observed sample for a session is treated as a baseline and records
`token_delta = 0`, so enabling the tool in the middle of a long session does not
over-count previous work.

## Boundaries

Only local Codex sessions on this machine can contribute token deltas. If the
weekly percentage increases with little or no local token delta, the epoch is
marked as external usage observed. That can mean Codex Web, another machine, or
a session that was not covered by the hook consumed usage.
