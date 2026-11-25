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

- Run the same command again any time to update; it keeps routing on.
- To read the source first with the one-file installer: `bash install-claude-model-profile.sh --extract ./routing-src`. It unpacks the files and installs nothing.
- To install without switching routing on: `./install.sh --friendly`, then `/model-profile on` when you are ready.
- Each release lists a SHA-256 checksum beside the installer. Download `install-claude-model-profile.sh.sha256` next to it, then run `shasum -a 256 -c install-claude-model-profile.sh.sha256` on macOS or `sha256sum -c install-claude-model-profile.sh.sha256` on Linux. `OK` means the file arrived undamaged.

### Linux notes

- **Python 3.10 or newer** is required (`python3 --version`). Ubuntu 22.04+ and Debian 12+ have it. On Ubuntu 20.04 or Debian 11 the installer stops with a clear message and changes nothing; install a newer Python first.
- It also needs only tools every distribution ships: `bash`, `tar`, `awk` and `mktemp`.
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

**Choosing a model yourself** with `/model` turns routing off automatically and keeps your choice. Switch it back on with `/model-profile on`.

## What happens automatically while it is on

**Effort follows the task.** Each message is sized before Claude starts, using a local keyword check (no model call, no extra tokens, your message is never stored).

| Task | What runs |
|---|---|
| Questions, explanations, small edits | Sonnet at medium effort |
| Debugging, refactors, multi-file changes, performance work, features | the `deep-work` skill: high effort for that message |
| Security, auth, billing and payments, migrations, production incidents, concurrency, or "still failing" | the `critical-work` skill: Opus at high effort for that message |

A question that only mentions billing or auth stays cheap. The check only suggests; Claude skips the skill if the task turns out to be simple.

**A change cannot be finished until it is proven and reviewed.** When a task changed files, Claude is asked for three things, in one message:

1. **A passing check.** A test, lint, type-check or build command must have run and passed *after the last edit*. Documentation-only changes are exempt.
2. **An Opus review.** Small changes go to `code-reviewer` (Opus, medium effort); large or risky ones (auth, billing, payments, security, secrets, migrations, webhooks, over 150 lines or 8 files) go to `deep-reasoner` (Opus, high effort). A `CHANGES REQUIRED` verdict means fix and review again.
3. **No secrets in new code.** The lines you added are scanned for real key formats (AWS, Stripe live, GitHub, GitLab, Slack, Google, Anthropic, OpenAI, private keys) and hard-coded passwords. Only file, line and kind are reported, never the value. Test fixtures are not flagged for passwords.

It never traps a session: after two requests it lets Claude finish and tells you what is still missing. Questions and explanations that change nothing are never checked, so they cost nothing extra. Expect the Opus review to add some token cost to every code change; that is the price of the safety net.

**Your standards ride along.** A short file, `~/.claude/model-profile/standards/standards.md`, is added to every new conversation (capped at 4,000 characters). The default rules: enterprise-grade quality, never claim "done" without driving the real flow, production is live so be backward-compatible and additive, testable SOLID design, fix only what was asked, and security by default. **Edit that file to your own rules.**

## What it changes on your machine

- `~/.claude/settings.json`: `model` (set to `opusplan`), `effortLevel`, and hooks on four events (model change, session start, each message, and when Claude finishes). `off` restores what you had and removes exactly what `on` added. Your own settings and hooks are left alone.
- `~/.claude/agents/`: four helper agents while routing is on (`Explore` replaces the built-in one with a Haiku version).
- `~/.claude/skills/`: `deep-work` and `critical-work` while routing is on. It refuses to install if you already have a skill with either name.
- `~/.claude/commands/`: `/model-profile` and `/complex`.
- `~/.claude/bin/` and `~/.claude/model-profile/`: the tool itself, your standards file, a small on/off history log, a size-capped event log (events only, never message text) and per-conversation review state (cleaned up after a week).

Nothing is sent over the network. The hooks make no network calls, run in an isolated Python (`python3 -I`), and fail open: a problem in the tool can never block you from working.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/model-profile` not found | Restart Claude Code after installing |
| Status says ON but the conversation uses another model | A model chosen in that conversation wins. Type `/model opusplan` there, or open a new conversation |
| Routing switched itself off | You picked a model with `/model`. `/model-profile on` switches it back on |
| Claude keeps asking for an Opus review | Intended after a change; let it run. After two requests it stops asking and tells you the change is unreviewed |
| Claude asks to run checks for a tiny change | Intended. If the project has no tests or lint, Claude says so and the request stops after two tries |
| A "possible secret" is just a test value | Put fixtures under `tests/` or name them `*.example`, or tell Claude it is deliberate: you get one warning, then it is only reported |
| `on` says "refusing to overwrite" a skill | You already have a skill named `deep-work` or `critical-work`. Rename yours and run `on` again |
| "settings.json changed while it was being updated" | Another program saved your settings at the same moment. Nothing was overwritten; run the command again |
| "do not run with sudo" | Run it as yourself |

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
