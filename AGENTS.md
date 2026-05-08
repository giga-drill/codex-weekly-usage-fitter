# AGENTS.md

## Project Role

This repo is a local Codex usage monitor. Keep this file short because Codex
loads it automatically.

## Planner / Executor Workflow

- GPT-5.5 planner sessions should read `docs.md` only when resuming, planning,
  or reviewing work.
- gpt-5.3-codex executor sessions should read this file and `docs.md` before
  implementation work.
- After meaningful work, update `docs.md` with the current state, validation,
  and next task.

## Repo Rules

- Use `python3`, not `python`.
- Quote paths because this repo path contains a space:
  `/Users/mac/projs/codex usage`.
- Do not revert unrelated dirty changes; they may come from another session.
- After Python changes, run:
  `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Keep public docs free of machine-specific paths unless documenting local
  setup intentionally.
