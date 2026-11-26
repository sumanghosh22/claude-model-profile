# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [SemVer](https://semver.org/).

## [1.12.0] - 2026-11-26

### Changed
- **Every finished change is reviewed by Opus at high effort.** `code-reviewer` moved from medium to high effort (and up to 30 turns); `deep-reasoner` already ran at high.
- **Everyday work runs at high effort.** The profile's default effort moved from medium to high, so code is written with more thought, not only reviewed with it. Installs upgraded while routing is on move the old `medium` to `high` (a value you chose yourself is kept), and `off` still restores what you had before routing. Costs more tokens and replies take longer.
- **Every scenario must have a test.** Both reviewers now list every scenario the change can meet (the normal path, boundaries, invalid or hostile input, every failure path, repeated or concurrent calls, security and authorisation, backward compatibility, a regression test per fixed bug) with the test that covers it. Any scenario without a test is a major finding, so the review gate sends the change back until it has one. The gate's request says so, and an eighth default standard asks for it in every task.

### Fixed
- **Upgrades now reach the helper agents and skills.** Their copies in `~/.claude/agents` and `~/.claude/skills` were made only by `on`, so an update (automatic or by hand) never changed them until routing was switched off and on. The installer now refreshes them while routing is on: unedited copies get the new content, new files are added where nothing of yours has the name, files no longer shipped are removed; a copy you edited, linked, made unreadable or deleted is never touched (and a deleted one never comes back). A refresh cut off midway repairs itself on the next install; if the refresh cannot run at all, the hooks are still registered and the failure is reported. An independent Opus review found 2 major and 4 minor problems in the first version; all are fixed. 19 tests cover each case, and each of the 12 guards was removed in turn to confirm a test fails.

## [1.11.0] - 2026-11-25

### Added
- **Automatic, signed updates.** Once a day, in the background, an install checks the latest release and installs it, so you get each new version without doing anything; the next conversation shows one line, "updated to X", with a link to what changed. A conversation never waits for the network, and updates keep working while routing is off.
- **Every release is signed** (Ed25519, `ssh-keygen -Y sign`), and the signed installer states its own version. Before anything runs, the signature must verify against the key shipped in `src/channel/update-channel.json`, and the signed version must equal the release tag, be newer than the installed one and keep the major version. Refused, never run: unsigned or re-signed releases, an old signed installer replayed under a newer tag (no downgrades), a major version slipped in under a minor tag, oversized files, non-HTTPS links or redirects, a damaged channel file. Each refusal is shown once; offline is silent.
- New major versions are announced, never installed automatically.
- `/model-profile update`, `update-check`, `auto-update-on`, `auto-update-off`; `status` shows the update state. `CLAUDE_MODEL_PROFILE_NO_AUTO_UPDATE=1` also switches it off.
- **Upgrades register new hooks.** Installing over an older version now adds any hook the new version needs, without `off` and `on`; it writes nothing when nothing is missing.
- `build_bundle.py --sign KEY` signs a release build.

### Upgrading
- From 1.10.0 or earlier, run the one-file installer once more; from then on updates are automatic.

### Verified
- Two independent security reviews found 19 problems before release (among them: the version was checked only after the installer ran; upgrades by hand never registered the hook; switching routing off stopped updates; a stalled server could hold up a conversation start; an uninstall during an update could be undone; a busy settings lock could fail an update). All are fixed, each with a test.
- The updater tests drive real signed releases from a local server: tampered, re-signed, unsigned, replayed, oversized, redirected, cut short, stalled, offline and concurrent. 38 safety checks were each removed in turn, and every removal made a test fail.
- A real upgrade, from an installed 1.11.0 to a release signed with the real key, was driven end to end.

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
