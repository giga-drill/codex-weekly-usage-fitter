# Usage Profiler Execution Plan

Last updated: 2026-05-08

This is the executor-facing plan for turning the project from a usage counter
into a Codex usage profiler with a feedback loop.

## Goal

Explain why Codex usage grows, then use small reversible policy experiments to
reduce usage per completed task without hurting quality.

Primary output:

```text
What consumed usage?
Why did it consume usage?
Which mitigation was tried?
Did usage improve without more rework?
```

## Non-Goals

- Do not change token delta or turn-count semantics.
- Do not guess OpenAI's internal usage formula.
- Do not optimize by simply hiding validation or skipping necessary evidence.
- Do not store raw transcript text in new profiler tables unless the user
  explicitly approves it.

## Stable Definitions

- Observed turn: one positive sample-to-sample `token_delta` interval.
- Result cost: `token_delta`, `weekly_used_percent` delta, model, and
  reasoning effort for an observed turn.
- Process cost: tool calls, tool output size, files read, commands run, tests
  run, and repeated exploration inside the observed turn.
- Task quality: tests pass, user does not need corrective rework, and the next
  turn can continue without rediscovering the same facts.
- Usage efficiency: usage consumed per completed useful task, not usage per
  chat message.

## Root-Cause Variables

The profiler should estimate four variables:

1. Context density
   - Is the agent carrying mostly useful state or old evidence/noise?
   - Useful proxies: files modified divided by files read; decisions/changes
     produced divided by tool output bytes; docs/history rereads per turn.

2. Exploration hit rate
   - Did searches and file reads point at files that affected the final work?
   - Useful proxy:

```text
hit_rate = files_modified_or_tested_or_cited / files_read_or_matched
```

3. Frontier retention
   - After resume, compaction, or planner/executor handoff, can the next agent
     continue without rediscovering the same execution frontier?
   - Useful proxies: repeated reads of already-identified files; repeated search
     patterns; rereading `docs/history.md`; repeated explanation of already
     captured decisions.

4. Repeat verification rate
   - How often did the agent rerun the same command or reread the same file
     when neither the file nor relevant inputs changed?
   - Re-running tests after edits is valid verification, not waste.

## Phase 1 - Passive Observation

Add profiling without changing existing product behavior.

Implementation shape:

- Add transcript/tool-event extraction in or near `src/codex_usage/transcript.py`.
- Add persistence helpers in `src/codex_usage/store.py`.
- Add a CLI report command in `src/codex_usage/cli.py`.
- Add tests using small local fixtures.

Suggested tables:

```text
turn_profiles
  id
  start_sample_id
  end_sample_id
  session_id
  turn_id
  observed_at
  token_delta
  weekly_percent_delta
  model
  reasoning_effort
  duration_seconds
  task_type nullable
  scope nullable
  handoff_quality nullable
  quality_flags_json nullable

turn_tool_events
  id
  turn_profile_id
  tool_name
  command_kind nullable
  command_hash nullable
  output_bytes
  output_lines
  exit_code nullable
  paths_json
  started_at nullable

turn_file_events
  id
  turn_profile_id
  path
  action read|modified|tested|cited
  source tool|git|diff|report
```

Privacy rule:

- Store command hashes and normalized command kind by default.
- Store file paths and output sizes.
- Do not store raw command output or full prompt/assistant text in profiler
  tables.

Minimum report:

```bash
PYTHONPATH=src python3 -m codex_usage profile --period today
PYTHONPATH=src python3 -m codex_usage profile --period today --json
```

Report should show:

- highest-cost turns by `token_delta`;
- tool output bytes by tool kind;
- files read vs files modified/tested;
- repeated file reads;
- repeated command hashes;
- docs/history rereads;
- candidate root-cause labels:
  - `large_tool_output`
  - `broad_exploration`
  - `low_hit_rate`
  - `repeated_verification`
  - `docs_reread`
  - `handoff_reexploration`

Validation:

- Existing tests still pass:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

- New parser tests cover:
  - shell/tool events are counted;
  - output bytes/lines are counted;
  - paths are extracted without storing raw output;
  - repeated reads of the same path are detected;
  - edits followed by tests are not classified as wasteful repeated
    verification.
- `profile --json` returns stable JSON with no raw transcript text.
- Running the profiler twice is idempotent: it does not duplicate turn profiles.

## Phase 2 - Derived Diagnosis

Add simple scoring over observed turns.

Suggested derived fields:

```text
context_density_score
exploration_hit_rate
frontier_retention_score
repeat_verification_score
primary_cost_driver
```

Scoring should be transparent and conservative:

- Prefer coarse labels over false precision.
- Use `unknown` when evidence is insufficient.
- Never claim causality from token count alone.

CLI report should include a section like:

```text
Likely cost drivers today:
- large_tool_output: 43% of profiled token_delta
- broad_exploration: 31%
- repeated_verification: 12%
- unknown: 14%
```

Validation:

- Fixture tests prove each label can be produced.
- Mixed/unknown cases remain `unknown` instead of forced into a label.
- Reports reconcile with existing sample totals for the selected period.

## Phase 3 - Policy Experiment Records

Add records for small reversible experiments. This phase still does not need to
automatically change Codex behavior.

Suggested tables:

```text
policy_experiments
  id
  name
  status planned|active|completed|rolled_back
  task_type
  hypothesis
  intervention_json
  expected_effect_json
  guardrails_json
  started_at
  ended_at nullable

policy_decisions
  id
  experiment_id
  decision keep|rollback|adjust
  evidence_json
  reason
  created_at
```

Example experiment:

```text
Hypothesis:
  Executor rereads too much history on implementation tasks.

Intervention:
  Read docs.md and decisions only; read history only when debugging or when
  docs.md points to a required historical detail.

Expected effect:
  usage_per_completed_implementation_task down 10%.

Guardrails:
  user corrections do not increase;
  repeated exploration does not increase;
  tests still pass.
```

Validation:

- CLI can create/list/complete an experiment.
- Completed experiments show before/after metrics over the selected windows.
- If quality guardrails worsen, the recommended decision is `rollback` or
  `adjust`, not `keep`.

## Phase 4 - Feedback Loop Report

Add a periodic report that turns observations into conservative next actions.

Report shape:

```text
Effective policies:
- targeted tests first for implementation: usage/task -18%, no test regression.

Failed policies:
- output cap 100 lines: repeated debugging turns increased; rollback.

Candidate next experiment:
- reduce history reads for non-debugging executor tasks.
```

The system should only recommend one small next experiment at a time.

Validation:

- A report with no enough data says "not enough data".
- A report with worsened guardrails does not recommend tightening constraints.
- A report can identify a successful, failed, and inconclusive experiment from
  fixtures.

## Executor Acceptance Criteria

The first implementation should be accepted when Phase 1 is complete:

- Existing CLI/status/widget behavior is unchanged.
- Existing token delta and turn-count tests still pass.
- A new `profile` command exists with text and JSON output.
- The new profiler stores metadata only, not raw transcript text.
- The profiler can identify high-output tool turns and repeated file reads on a
  fixture.
- The report is useful even before semantic labels are perfect.

Later phases should be separate tasks unless the user asks to continue in one
large implementation pass.

