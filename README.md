# Agent Rewind

Checkpoint, branch, and compare local AI coding-agent runs.

## Status

Agent Rewind is a new project for Hacktoberfest Week 2. Implementation has not started. There is no working demo or benchmark yet. This repository currently contains only this README.

## Planned features

- Checkpoints of project files and recorded task context.
- Isolated alternative continuations from a checkpoint.
- Comparison of code diffs, test results, elapsed time, and reported token usage when available.
- Explicit selection of the result to keep.

These features are planned; they are not implemented yet.

## Limits

File and recorded-context checkpoints cannot restore hidden model state or undo external actions.

## Inspiration

Inspired by [agent-communication-system](https://github.com/anon5376/agent-communication-system). That existing project is not the competition entry. No code has been copied or transferred from it into Agent Rewind.
