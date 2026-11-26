# Claude Model Profile

> Unofficial. Not affiliated with or endorsed by Anthropic.

**Use the right Claude model for each job, automatically, without thinking about it.**

Running everything on the most capable model is expensive. Running everything on a cheaper one costs you quality on the hard parts. This profile for [Claude Code](https://claude.com/claude-code) uses all three models where each one fits, raises the thinking effort only for hard or risky work, and will not let Claude call a code change "done" until it has been tested and reviewed.

| Model | Used for | How it is chosen |
|---|---|---|
| **Opus** | Planning, hard reasoning, and reviewing every finished change | Plan mode switches to Opus; the `deep-reasoner` and `code-reviewer` helper agents |
| **Sonnet** | Writing code, edits, everyday work | The default once plan mode ends (`opusplan`) |
| **Haiku** | Searching the codebase, reading logs and test output | The `Explore` and `log-reader` helper agents |

## Install

Requirements: [Claude Code](https://claude.com/claude-code), Python 3.10 or newer, macOS or Linux, access to Opus, Sonnet and Haiku.

**One file** (simplest). Download `install-claude-model-profile.sh` from the [latest release](https://github.com/sumanghosh22/claude-model-profile/releases/latest), or from a terminal (macOS and Linux):

```bash
curl -fsSLO https://github.com/sumanghosh22/claude-model-profile/releases/latest/download/install-claude-model-profile.sh
bash install-claude-model-profile.sh
```

**From source:**

```bash
git clone https://github.com/sumanghosh22/claude-model-profile.git
cd claude-model-profile
./install.sh
```

Either way it installs and switches routing on. Then **restart Claude Code** and open a **new conversation** (conversations that were already open keep the model they had; in one of those, type `/model opusplan`). Type `/model-profile` to check it is on.

- **Updates install themselves.** Once a day, in the background (a conversation never waits), it checks the latest release and installs it if the installer is signed by the maintainer's key; the next conversation shows one line, "updated to X". This keeps working while routing is off. New major versions are only announced. Run the same command again any time to update by hand; it keeps routing on.
- Installed 1.10.0 or earlier? Run the one-file command once more to get 1.11.0; from then on updates are automatic.
- To read the source first with the one-file installer: `bash install-claude-model-profile.sh --extract ./routing-src`. It unpacks the files and installs nothing.
- To install without switching routing on: `./install.sh --friendly`, then `/model-profile on` when you are ready.
- Each release lists a SHA-256 checksum beside the installer. Download `install-claude-model-profile.sh.sha256` next to it, then run `shasum -a 256 -c install-claude-model-profile.sh.sha256` on macOS or `sha256sum -c install-claude-model-profile.sh.sha256` on Linux. `OK` means the file arrived undamaged. From 1.11.0 each release also carries `install-claude-model-profile.sh.sig`, an Ed25519 signature that installed copies verify before every automatic update.

### Linux notes

- **Python 3.10 or newer** is required (`python3 --version`). Ubuntu 22.04+ and Debian 12+ have it. On Ubuntu 20.04 or Debian 11 the installer stops with a clear message and changes nothing; install a newer Python first.
- It also needs only tools every distribution ships: `bash`, `tar`, `awk` and `mktemp`. Automatic updates also use `ssh-keygen` (package `openssh-client`, present on almost every system) to check signatures; without it, updates are refused and you are told once.
- Do **not** run it with `sudo`. It installs into your own `~/.claude` and refuses to run as root through `sudo`, which would make that folder root-owned.
- The test suite runs on Ubuntu with Python 3.10 and 3.13 in CI. If something behaves differently on your distribution, please [open an issue](https://github.com/sumanghosh22/claude-model-profile/issues).

## Daily use

| You want | Type |
|---|---|
| See whether it is on | `/model-profile` |
| Switch on / off | `/model-profile on` · `/model-profile off` |
| Routing says ON but the model is wrong | `/model-profile repair`, then restart |
| A hard task, planned and checked step by step | `/complex <task>` |
| Maximum thinking for one message | add `ultrathink` |
| Force a hard problem to Opus | say "use deep-reasoner on this" |
| Update now / stop automatic updates | `/model-profile update` · `/model-profile auto-update-off` |

**Choosing a model yourself** with `/model` turns routing off automatically and keeps your choice. Switch it back on with `/model-profile on`.

## What happens automatically while it is on

**Effort follows the task.** Each message is sized before Claude starts, using a local keyword check (no model call, no extra tokens, your message is never stored).

| Task | What runs |
|---|---|
| Questions, explanations, small edits | Sonnet at high effort |
| Debugging, refactors, multi-file changes, performance work, features | the `deep-work` skill: high effort for that message |
| Security, auth, billing and payments, migrations, production incidents, concurrency, or "still failing" | the `critical-work` skill: Opus at high effort for that message |

A question that only mentions billing or auth stays cheap. The check only suggests; Claude skips the skill if the task turns out to be simple.

**Investigations get Opus, and the conversation remembers.** Reading evidence about a database or a production system ("why is this query slow", "find the solution", "rows examined", "check the prod db") is sized critical, because a misread number is the costliest mistake. And a conversation about hard work stays hard: short follow-ups carry no keywords of their own, so for the next 2 hours (up to 8 messages) a follow-up about the same subject keeps the level, while a generic one ("then what should I do?") drops one level. Closing words ("ok", "thanks") and long silences end it. Only the level, a few keywords and a counter are remembered, never your messages. The two effort skills also tell Claude to say where every number came from, mark what it inferred, and re-read raw logs and query plans before concluding.

**A change cannot be finished until it is proven and reviewed.** When a task changed files, Claude is asked for three things, in one message:

1. **A passing check.** A test, lint, type-check or build command must have run and passed *after the last edit*. Documentation-only changes are exempt.
2. **An Opus review.** Small changes go to `code-reviewer` (Opus, high effort); large or risky ones (auth, billing, payments, security, secrets, migrations, webhooks, over 150 lines or 8 files) go to `deep-reasoner` (Opus, high effort). A `CHANGES REQUIRED` verdict means fix and review again. Both reviewers also list every scenario the change can meet (normal path, boundaries, invalid input, failures, concurrency, security, backward compatibility, each bug fixed) with the test that covers it; any scenario without a test is a major finding, so the change is sent back until it has one.
3. **No secrets in new code.** The lines you added are scanned for real key formats (AWS, Stripe live, GitHub, GitLab, Slack, Google, Anthropic, OpenAI, private keys) and hard-coded passwords. Only file, line and kind are reported, never the value. Test fixtures are not flagged for passwords.

**Every task is checked after it finishes, not only code changes.** When Claude stops, its final answer is compared with what actually ran in that turn:

- It says tests or checks pass, but the last check of the turn **failed** → sent back once to fix it or say plainly that it fails. (Any task.)
- In critical work it states facts about data or production (numbers over 999, rows, keys, tables, queries) while **no tool ran** and without saying "not verified" or "inferred" → sent back once to verify with a read-only tool or label it honestly.
- It says "I verified / checked / ran…" or "tests pass" while no tool ran in that turn → a free **notice** to you ("treat it as unverified"), not a block, because answers often summarise work from earlier messages.

Each message is sent back at most once, never in a loop, and the answer is never stored or logged. On two weeks of the author's real use: 4 send-backs and 14 notices across 670 tasks. Two default standards back this up: every answer says what was verified and how, and what was not; and every scenario is tested, none skipped.

It never traps a session: after two requests it lets Claude finish and tells you what is still missing. Questions and explanations that change nothing are never checked, so they cost nothing extra. Expect the Opus review to add some token cost to every code change; that is the price of the safety net.

**Your standards ride along.** A short file, `~/.claude/model-profile/standards/standards.md`, is added to every new conversation (capped at 4,000 characters). The default rules: enterprise-grade quality, never claim "done" without driving the real flow, production is live so be backward-compatible and additive, testable SOLID design, fix only what was asked, and security by default. **Edit that file to your own rules.**

## What it changes on your machine

- `~/.claude/settings.json`: `model` (set to `opusplan`), `effortLevel`, and hooks on four events (model change, session start, each message, and when Claude finishes). `off` restores what you had and removes exactly what `on` added. Your own settings and hooks are left alone. One more session-start hook, for automatic updates, is added at install, kept while routing is off, and removed by `uninstall`.
- `~/.claude/agents/`: four helper agents while routing is on (`Explore` replaces the built-in one with a Haiku version).
- `~/.claude/skills/`: `deep-work` and `critical-work` while routing is on. It refuses to install if you already have a skill with either name.
- `~/.claude/commands/`: `/model-profile` and `/complex`.
- `~/.claude/bin/` and `~/.claude/model-profile/`: the tool itself, your standards file, a small on/off history log, a size-capped event log (events only, never message text) and per-conversation review state (cleaned up after a week).

Nothing about you or your work is sent anywhere. The hooks run in an isolated Python (`python3 -I`) and fail open: a problem in the tool can never block you from working.

**The one network use: updates.** At most once a day, in the background, it downloads the latest release from GitHub (plain HTTPS `GET`s, no identifiers). It installs it only if the installer is signed by the maintainer's key and the signed file is the version the release claims; an unsigned, re-signed, replayed, oversized or non-HTTPS release is refused before anything runs, and you are told once. Offline, nothing happens. Switch it off with `/model-profile auto-update-off` (or `CLAUDE_MODEL_PROFILE_NO_AUTO_UPDATE=1`). Details: [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/model-profile` not found | Restart Claude Code after installing |
| Status says ON but the conversation uses another model | A model chosen in that conversation wins. Type `/model opusplan` there, or open a new conversation |
| Routing switched itself off | You picked a model with `/model`. `/model-profile on` switches it back on |
| Claude says "evidence check" and answers again | Intended: the answer claimed tests pass after a failed check, or stated facts about data in critical work without running anything. It asks once per message. |
| A notice says "no tool ran in this turn" | Claude claimed a check it did not run in that message. If it matters, ask it to run the check now. |
| Claude keeps asking for an Opus review | Intended after a change; let it run. After two requests it stops asking and tells you the change is unreviewed |
| Claude asks to run checks for a tiny change | Intended. If the project has no tests or lint, Claude says so and the request stops after two tries |
| A "possible secret" is just a test value | Put fixtures under `tests/` or name them `*.example`, or tell Claude it is deliberate: you get one warning, then it is only reported |
| `on` says "refusing to overwrite" a skill | You already have a skill named `deep-work` or `critical-work`. Rename yours and run `on` again |
| "settings.json changed while it was being updated" | Another program saved your settings at the same moment. Nothing was overwritten; run the command again |
| "do not run with sudo" | Run it as yourself |
| "an update was not installed: …" | A release failed a safety check (unsigned, wrong signature, version mismatch, too large, not HTTPS, or a file you edited). Nothing was changed; your version keeps working. If it is a file you edited, run the installer with `--force` to keep your copy as a backup |
| "… is a major update; it is not installed automatically" | Read the release notes, then `/model-profile update` |
| `status` says `auto-update: inactive` | The update hook is missing from your settings. Run `/model-profile auto-update-on`, then restart |
| "… was installed, but its hooks could not be registered" | Another program kept `settings.json` busy during the update. Run the one-file installer again |
| The installer says "an automatic update is being installed right now" | Wait a minute and run it again |

## Honest limitations

- Claude Code cannot switch the main model automatically for each message. Routing combines plan mode (`opusplan`), helper agents (the model decides when to call them) and the effort skills.
- The size check is keywords, not understanding. If a task is sized wrong, please [open an issue](https://github.com/sumanghosh22/claude-model-profile/issues) with the keywords (never paste private message text).
- The Opus review and the extra effort cost tokens. The savings come from Sonnet and Haiku doing most of the work.

## Uninstall

```bash
bash install-claude-model-profile.sh uninstall    # or: ./install.sh uninstall
# switches routing off first and restores your settings
```

It removes only the files it installed. If you edited one of them it stops and tells you, unless you add `--force`.

## Development

```bash
make tools    # installs the pinned ruff and mypy
make check    # lint, format check, strict types, all tests
make bundle   # builds dist/install-claude-model-profile.sh and its checksum
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
