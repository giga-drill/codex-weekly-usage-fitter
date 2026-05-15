# Codex Usage Decisions

Last updated: 2026-05-08

This file holds stable product and architecture decisions. It is not
auto-loaded by Codex; read it when changing semantics or user-visible behavior.

## Token Delta Policy

Every user-visible token-consumption number should use the `token_delta`
concept.

Raw observation definition:

```text
sample token delta = samples.token_delta
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

- Raw sample rows should use `token_delta` when displaying sample-level token
  values.
- Completed conversation-turn rows should aggregate raw `token_delta` samples
  according to `docs/conversation-turn-fix-plan.md`.
- Do not show `last_total_tokens` or `last_*_tokens` in normal UI, CLI status,
  stats panels, or public status JSON.
- `last_total_tokens` may remain as an internal raw stored field for debugging
  or future parser work.
- If raw export keeps internal columns, label it clearly as raw/internal data.

Operational caveats:

- `token_delta` is a raw sample-to-sample interval. It may be an intermediate
  step inside a conversation turn.
- If samples are missed, `token_delta` can cover more than one internal event.
- The first observed sample for a session is a baseline with `token_delta = 0`;
  it is not a completed conversation turn.

## Conversation Turn Policy

User-visible accounting should use `conversation turn`, not bare `turn`.

Definition:

```text
conversation turn = the interval that starts when the user sends one message
and ends when the assistant finishes the final response for that message.
```

Rules:

- Codex transcript `turn_id` is an internal id and is not a conversation turn.
- A single conversation turn can contain multiple internal Codex steps,
  token-count events, and sometimes multiple internal `turn_id` values.
- Raw `samples` are token-count observations, not user-visible turns.
- Persist completed conversation turns in a separate `conversation_turns`
  aggregate table; normal UI and CLI statistics should read that table.
- User-visible UI, CLI, stats, billing details, and estimates should use
  completed conversation turns.
- Baseline observations and intermediate in-progress token-count events are not
  completed conversation turns.
- Raw/internal exports may keep `turn_id`, but label it as internal Codex data.

Estimator:

```text
conversation_turns_per_weekly_percent =
  sum(clean_event.conversation_turn_count) / sum(clean_event.percent_delta)
```

Each clean event should count completed conversation turns inside that movement
event, not positive raw sample intervals.

Audit surfaces:

- `usage_movement_events.turn_count` or replacement conversation-turn count
- `model_effort_fits.turns_per_weekly_percent` or renamed replacement
- `model_effort_global_fits.turns_per_weekly_percent` or renamed replacement
- billing period/day/week conversation-turn count
- widget `1% ~= X conversations / Y tok`
- stats panel `Clean 1%`
- CLI `status` and `billing-stats`
- README/user docs

See `docs/conversation-turn-fix-plan.md` for the executor-facing fix plan.

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
