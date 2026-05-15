# Conversation Turn Fix Plan

Last updated: 2026-05-09

This plan fixes the current "turn" ambiguity before implementing the usage
profiler.

## User-Facing Concept

Use `conversation turn` for user-visible accounting.

Definition:

```text
conversation turn = the interval that starts when the user sends one message
and ends when the assistant finishes the final response for that message.
```

This is not the same as Codex transcript `turn_id`.

Codex internals can emit several token-count events inside one user-visible
conversation turn, especially during tool calls. One conversation turn can also
contain several internal Codex steps. User-facing UI must not treat those
intermediate events as separate turns.

## Current Root Cause

Current code has two conflated concepts:

- `samples.turn_id` stores Codex transcript/internal `turn_id`.
- User-visible reports call positive `token_delta` sample intervals "turns".

This breaks when a single conversation turn emits multiple `token_count` events.
Observed local evidence on 2026-05-09:

```text
same session_id = 019e081a-31d3-7dc3-b533-6eadf8ae9f33
same internal turn_id = 019e0ac4-c093-7af0-af8d-885adf5026bd
multiple samples:
  token_delta 0
  token_delta 236626
  token_delta 265884
```

Those rows are not three conversation turns. They are intermediate samples from
one in-progress conversation turn.

Likely implementation cause:

- `scan_recent_transcripts()` calls `_transcript_turns()`.
- `_transcript_turns()` calls `finish_turn()` at EOF.
- If the latest transcript turn is still active, EOF still records it.
- `parse_transcript(path, turn_id=...)` then selects the latest token-count
  event currently available inside that internal turn.
- The daemon scans repeatedly, so one active internal turn can be sampled
  multiple times before the assistant final response is finished.

## Required Product Semantics

All normal user-visible surfaces should use conversation-turn terminology and
values:

- Main complete widget view:
  - replace `Last turn ... tokens` with `Last conversation ... tokens` or
    `Last conversation turn ... tokens`.
- Stats panel period summary:
  - replace `Turns` with `Conversation turns`.
  - replace `Avg / turn` with `Avg / conversation`.
- Stats detail rows:
  - sample rows should represent completed conversation turns, not internal
    token-count steps.
- CLI:
  - `status` should describe latest completed conversation-turn token usage.
  - `billing-stats` should use conversation-turn counts and labels.
- README/docs:
  - stop defining a user-visible turn as a positive sample interval.
  - define `conversation turn` separately from internal Codex `turn_id`.

Raw/internal exports may still expose `turn_id`, but label it as internal Codex
turn id.

## Data Model

Use a two-layer model:

```text
samples = raw token-count observation layer
conversation_turns = user-facing aggregate layer
```

`samples` should keep its raw meaning. Each row is a token-count observation
from a hook or transcript scan. It may represent an intermediate step inside a
conversation turn.

`conversation_turns` is the aggregate table produced from `samples` plus the
transcript's `user_message` boundaries. Frontend, CLI, billing stats, and fits
should read this aggregate layer for user-visible turn/count/token-per-turn
semantics.

Data flow:

```text
Codex transcript
  -> samples(raw token-count observations)
  -> conversation_turns(completed user-visible turns)
  -> movement events / fits / billing stats / widget / CLI
```

## Required Backend Change

Executor must introduce the completed conversation-turn aggregate layer instead
of using raw `samples` directly for user-visible turn counts.

Suggested table:

```text
conversation_turns
  id INTEGER PRIMARY KEY
  session_id TEXT
  conversation_turn_key TEXT UNIQUE
  start_observed_at TEXT
  end_observed_at TEXT
  transcript_path TEXT
  first_internal_turn_id TEXT
  last_internal_turn_id TEXT
  internal_turn_ids_json TEXT
  sample_ids_json TEXT
  model TEXT
  reasoning_effort TEXT
  token_delta INTEGER NOT NULL
  token_total_start INTEGER
  token_total_end INTEGER
  weekly_used_percent_start REAL
  weekly_used_percent_end REAL
  weekly_percent_delta REAL
  sample_count INTEGER NOT NULL
  completed INTEGER NOT NULL DEFAULT 1
```

Implementation rule:

- Raw `samples` continue to store token-count observations.
- User-facing status, billing stats, movement-event turn counts, widget, and
  README must use completed `conversation_turns`.
- `samples.turn_id` remains an internal field and should not be displayed as a
  user-facing turn id.
- The macOS widget reads SQLite directly, so Python-only in-memory
  `ConversationTurn` objects are insufficient. The aggregate must be persisted.
- Initial implementation may rebuild `conversation_turns` for the affected
  session after each inserted sample, using `INSERT OR REPLACE` or equivalent
  idempotent writes. It does not need a complex incremental updater.

## Boundary Detection

Preferred robust boundary:

1. Parse transcript by `event_msg` `user_message` records.
2. A conversation turn starts at a `user_message`.
3. It ends immediately before the next `user_message`, or at EOF only if the
   assistant final response has completed.
4. The token delta for the conversation turn is both:

```text
last token_total inside this conversation turn
- previous completed conversation turn's last token_total in the same session
```

and:

```text
sum(internal Codex turn token deltas inside this conversation turn)
```

For reconciliation, an internal Codex turn token delta means:

```text
last token_total observed for that internal turn
- token_total immediately before that internal turn's contribution
```

Do not use transcript `last_token_usage` as the conversation-turn total. It can
be only one model step inside the conversation turn.

Important:

- Do not mark the current active conversation turn complete just because EOF is
  reached during an in-progress assistant response.
- Stop hook events are a strong signal that the assistant turn has completed.
- Repeated background transcript scans should not create multiple completed
  conversation turns for the same user message.

Practical first implementation:

- Stop using scan-created samples from the active EOF turn for user-visible
  accounting.
- For transcript scans, only emit completed conversation turns whose next
  `user_message` exists, or whose completion was observed through the Stop hook.
- Use an idempotent `conversation_turn_key`, for example:

```text
sha256(session_id + transcript_path + user_message_timestamp_or_line_number)
```

If the transcript lacks a stable user-message id, line number is acceptable for
local storage.

## Migration / Backfill

Backfill `conversation_turns` from existing transcripts:

- Parse all recent/known transcript paths from `samples`.
- Group token-count events by conversation-turn boundaries.
- Insert one row per completed conversation turn.
- Rebuild movement events and fits from `conversation_turns`, not raw
  `samples` intervals.

Keep `samples` as raw observation history. Do not delete it.

## Surface Read Rules

After this change:

- `samples` is for raw/debug views and profiler internals.
- `conversation_turns` is for normal user-facing output.
- `usage_movement_events` should be built from `conversation_turns`.
- `model_effort_fits` and `model_effort_global_fits` should use completed
  conversation-turn counts.
- `billing-stats --debug` should list conversation-turn rows. If raw sample
  debug rows are still exposed, they must be explicitly labeled raw/internal.
- Swift widget SQL should read latest token usage and day/week/month counts
  from `conversation_turns`, not `samples`.

Compatibility:

- Existing field names such as `turn_count` may remain temporarily if changing
  them would be too invasive, but their values must come from completed
  conversation turns and labels must say conversation/conversation turns.
- Prefer adding clearer JSON keys such as `conversation_turn_count` and
  `latest_conversation_turn_token_delta` while keeping old keys only as
  documented compatibility aliases.

## Executor Review Findings After First Pass

The first implementation created `conversation_turns` and moved several
surfaces in the right direction, but these blockers remain:

1. `weekly_percent_delta` is currently computed per session while weekly usage
   is global/account state. This double-counts percentage movement when
   multiple sessions share the same weekly window. Local evidence on
   2026-05-09: today's `weekly_used_percent_end` moved from 25 to 27, but
   `SUM(weekly_percent_delta)` was 4; current billing-period stats reported
   `Usage: +141%`.
2. Top-level `epochs` and `fits` are still rebuilt from raw `samples`, so the
   normal `status` fit line can remain sample-interval based. User-visible fit
   output must be backed by completed `conversation_turns`.
3. Initial migration/backfill order is wrong for existing databases:
   `_ensure_model_effort_fits()` runs before `_ensure_conversation_turns()`.
   If samples exist but conversation turns do not, fit/movement rebuild is
   skipped, then conversation turns are backfilled without rebuilding dependent
   tables.
4. Transcript reconciliation is still incomplete. The parser stores final
   cumulative totals and `internal_turn_ids`, but does not expose/store the
   per-internal-turn token delta breakdown needed to prove:

```text
conversation_turns.token_delta
= final token_total - previous completed conversation final token_total
= sum(each internal Codex turn delta inside this conversation turn)
```

5. Normal CLI status still exposes `Latest sample ... turn=... delta=...`,
   which is raw/internal terminology. Keep that only in explicit debug/raw
   output, or label it clearly as internal.
6. README and profiler-plan docs still contain the old definition where a
   user-visible turn is a positive sample interval.

New tests were added to pin the missing behavior. They are expected to fail
until Executor fixes the implementation:

- multi-session weekly percentage movement must be counted globally, not once
  per session;
- generic `latest_fit` must be rebuilt from conversation-turn rows, not raw
  sample rows;
- reopening an existing DB with only samples must backfill conversation turns
  and rebuild dependent fit/movement tables in the same initialization path;
- transcript parsing must expose internal-turn token delta breakdown for
  reconciliation.

## Acceptance Criteria

Token reconciliation:

- For every completed `conversation_turns` row, the displayed token count
  equals `conversation_turns.token_delta`.
- `conversation_turns.token_delta` must equal the sum of all internal Codex
  turn token deltas inside that conversation turn.
- The same value must reconcile against the transcript:

```text
conversation_turns.token_delta
= final token_total in the conversation turn
  - previous completed conversation turn final token_total
= sum(each internal Codex turn delta included in internal_turn_ids_json)
```

- A test fixture with one user message, one internal `turn_id`, and multiple
  token-count events must produce one conversation turn whose token count equals
  the final cumulative token delta, not the last intermediate step.
- A test fixture with one user message spanning multiple internal `turn_id`
  values must produce one conversation turn whose token count equals the sum of
  those internal turn deltas.
- Executor should provide a debug path, test helper, or report assertion that
  prints/reconstructs the transcript-side calculation for a selected
  conversation turn. The numbers must match the database row.

Completion and idempotency:

- Re-running the scanner is idempotent.
- An active EOF conversation turn is not counted until completion is known.

UI/CLI correctness:

- Main widget no longer says `Last turn`.
- Stats panel no longer says bare `Turns` or `Avg / turn`.
- `billing-stats --debug` rows represent conversation turns, not internal
  token-count samples.
- The token value shown in widget complete view, status, and billing details is
  the reconciled `conversation_turns.token_delta`.
- CLI JSON names should either use new keys such as
  `conversation_turn_count` or keep old keys only with compatibility aliases
  clearly documented.

Estimator correctness:

- `turns_per_weekly_percent` should be renamed or backed by
  `conversation_turns_per_weekly_percent`.
- Mixed movement events count completed conversation turns.
- Positive raw sample intervals are not counted as user-visible turns.

Validation commands:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m codex_usage status
PYTHONPATH=src python3 -m codex_usage billing-stats --billing-day 12 --period current --debug
scripts/package-widget-app.sh
```

Widget validation:

1. Build/package the widget.
2. Quit any running `Codex Usage` process.
3. Open `build/Codex Usage.app`.
4. Confirm complete view and stats panel use conversation-turn labels and
   values.
