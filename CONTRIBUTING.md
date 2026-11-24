# Contributing

## Layout

```
src/bin/        model-profile: the on/off switch, hooks, review gate and checks
src/commands/   the /model-profile and /complex slash commands
src/agents/     the helper agents installed while routing is on
src/skills/     the deep-work and critical-work effort skills
src/standards/  the default rules added to every new conversation
scripts/        install.py (the installer), build_bundle.py (single-file installer)
tests/          end-to-end tests; they run the real tools in a throwaway HOME
```

## Workflow

```bash
make tools      # installs the pinned ruff and mypy
make check      # lint, format check, strict types, all tests
```

CI runs the same on macOS and Linux, on the oldest (3.10) and newest supported Python. A change is not done until `make check` is green.

## Principles

- Tests run the real tools in a temporary `HOME`; never against real user data or the real `~/.claude`. Anything that resolves `Path.home()` lazily must be pointed at the sandbox first.
- Fail with a one-line message, never a traceback. Never overwrite or delete a file the tool did not create.
- No dependencies beyond the Python standard library.
- Never log or store message text.
- Prefer deleting code to adding options.

## Adding an agent

Add `src/agents/<name>.md` (frontmatter: `name` equal to the file name, `description`, `tools`, `model`), update the expected agent sets in the tests, run `make check`, bump `VERSION` and `pyproject.toml`, and add a `CHANGELOG.md` entry.

## Releasing

Bump `VERSION` and `pyproject.toml`, add a `CHANGELOG.md` entry, run `make check` and `make bundle`, then attach `dist/install-claude-model-profile.sh` and its `.sha256` to a GitHub release tagged `v<version>`.
