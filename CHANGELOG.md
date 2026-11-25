# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [SemVer](https://semver.org/).

## [1.10.0] - 2026-11-25

### Added
- **Every task is checked after it finishes.** The final answer is compared with what actually ran in that turn: tests said to pass although the last check of the turn failed are sent back once (any task); in critical work, facts about data or production with no tool run and no "not verified" are sent back once; "I verified" claims with no tool at all get a free notice. Never a loop, never logged, fails open.
- A seventh default standard: every answer says what was verified and how, and what was not.

### Fixed
- **The review gate could time out in long sessions.** It read the whole transcript (a 1.5 GB transcript took about 37 s and 7.7 GB, against a 30 s hook limit). It now reads only the end, backwards, each byte once, capped at 128 MiB: about 0.02 s, and about 1 s in the worst case.
- Conversation memory is now 2 hours and 8 messages. "fix", "cause", "why", "plan", "risk", "impact" and "roll back" keep the level only in a question; a new instruction about something else drops a level.
- Hardened after an independent review: strict read cap, one transcript parse per stop, odd transcript entries and tampered state files can no longer break or bypass the check.

## [1.9.0] - 2026-11-25

### Added
- **Investigations are recognised.** Databases, queries, indexes or production together with slow, faster, latency, query plan, rows examined or "find the solution" are sized critical (Opus); those words alone are complex. `check`, `verify`, `analyse`, `diagnose`, `inspect` and `examine` count as actions.
- **Conversations are remembered**, so short follow-ups ("code was correct you mean?") keep the stakes of the work they follow; generic follow-ups drop one level; closing words end it.
- **Evidence discipline in both effort skills**: say where each number came from, mark inferences, re-read raw logs and query plans before concluding. `critical-work` now also covers conclusions drawn from production data, even when the message is only a question.

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
