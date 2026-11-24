# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [SemVer](https://semver.org/).

## [1.8.2] - 2026-11-24

First public release.

### Features
- **Model routing** for Claude Code: Opus plans, Sonnet does the work, Haiku searches and reads logs (`opusplan` plus helper agents). One command to switch on or off; `off` restores your previous settings exactly.
- **Automatic switch-off** when you pick a model yourself with `/model`.
- **Effort follows the task.** Each message is sized locally (no model call): routine work stays on Sonnet at medium effort, complex work uses the `deep-work` skill (high effort), risky work uses the `critical-work` skill (Opus, high effort).
- **Review gate.** A task that changed files cannot finish until a test, lint, type-check or build command passed after the last edit, an Opus reviewer has approved the change, and the added lines contain no secrets. It never traps a session: after two requests it lets Claude finish and says what is missing.
- **Your standards ride along**: a short, editable rules file is added to every new conversation.
- `/complex` for hard tasks, step by step.
- A single-file installer (`install-claude-model-profile.sh`): one command installs and switches routing on.

### Quality
- Strict-typed, lint-clean, standard library only; end-to-end tests run the real tools in a throwaway home folder; CI on macOS and Linux with Python 3.10 and 3.13.
