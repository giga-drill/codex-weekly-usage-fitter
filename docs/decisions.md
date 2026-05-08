# Codex Usage Decisions

Last updated: 2026-05-08

This file holds stable product and architecture decisions. It is not
auto-loaded by Codex; read it when changing semantics or user-visible behavior.

## Token Delta Policy

Every user-visible token-consumption number should use the `token_delta`
concept.

Definition:

```text
Last turn tokens = samples.token_delta
```

Meaning:

- `token_delta` is computed by this project as the delta between the current
  sample's cumulative session total and the previous sample's cumulative
  session total.
- `last_total_tokens` comes from the transcript's last `last_token_usage`
  payload. It can describe a final model step or sub-step inside a turn, not the
  whole sampled turn interval.
- The user does not care about per-step/sub-step token usage for now.

Rules:

- Use `token_delta` for every visible "last turn", sample-row token value,
  billing row token value, and token-per-turn calculation.
- Do not show `last_total_tokens` or `last_*_tokens` in normal UI, CLI status,
  stats panels, or public status JSON.
- `last_total_tokens` may remain as an internal raw stored field for debugging
  or future parser work.
- If raw export keeps internal columns, label it clearly as raw/internal data.

Operational caveats:

- If the Stop hook fires after every Codex turn, `token_delta` represents one
  complete turn.
- If samples are missed, `token_delta` can cover more than one turn. It is still
  the best user-facing number because it reflects the observed sample-to-sample
  consumption interval.
- The first observed sample for a session is a baseline with `token_delta = 0`;
  it is not a completed turn.

## Turn Count Policy

A user-visible turn is a positive sample-to-sample token delta interval.

Definition:

```text
One user-visible turn = one positive sample-to-sample token_delta interval.
```

Rules:

- `turn_count` counts only intervals where `token_delta > 0`.
- Baseline samples are not turns.
- Zero-delta samples are not turns.
- `sample_count` means number of samples or number of clean movement events; it
  must not be displayed or used as turn count unless explicitly converted to
  positive delta intervals.

Estimator:

```text
turns_per_weekly_percent =
  sum(clean_event.turn_count) / sum(clean_event.percent_delta)
```

Each `clean_event.turn_count` is the number of positive `token_delta` intervals
inside that clean movement event.

Audit surfaces:

- `usage_movement_events.turn_count`
- `model_effort_fits.turns_per_weekly_percent`
- `model_effort_global_fits.turns_per_weekly_percent`
- billing period/day/week `turn_count`
- widget `1% ~= X turns / Y tok`
- stats panel `Clean 1%`
- CLI `status` and `billing-stats`
- README/user docs

## Movement Event Attribution

Do not split a `weekly_used_percent` movement across model/effort buckets by
token share. OpenAI may charge different models and reasoning efforts at very
different effective rates, so token-proportional allocation creates false
precision.

Instead:

- Each time `weekly_used_percent` increases, create one movement event.
- The event covers local samples since the previous movement.
- Group samples by `(model, reasoning_effort)`.
- If the event has exactly one bucket, it is a clean sample for that bucket.
- If the event has multiple buckets, keep it as a mixed combination sample and
  do not attribute the percent movement to individual buckets.

Clean estimates:

```text
tokens_per_weekly_percent = token_delta_total / percent_delta
turns_per_weekly_percent = turn_count / percent_delta
sample_count = clean event count
percent_delta = sum(clean event percent_delta)
```

Mixed observations should be displayed as combinations, for example:

```text
gpt-5.5/medium + gpt-5.3-codex/high: +1%, 7 turns, 1.08M tokens
```

## Widget Runtime Rule

Rebuilding or packaging `build/Codex Usage.app` does not hot-update an already
running macOS widget process.

When validating widget behavior:

1. Build/package the app.
2. Quit the existing `Codex Usage` process.
3. Reopen `build/Codex Usage.app`.
4. Then compare UI output with SQLite/source behavior.

If UI still shows old semantics after a rebuild, first suspect a stale running
process.

## Documentation Hygiene

- `docs.md` is the active handoff entrypoint and should stay under about 120
  lines.
- `docs/decisions.md` holds stable rules.
- `docs/history.md` holds completed execution updates.
- Completed detail should leave `docs.md` once it is captured in code, tests,
  README, decisions, or history.
