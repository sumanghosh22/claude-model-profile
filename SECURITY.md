# Security

## Reporting a vulnerability

Please report security problems privately, using GitHub's **Report a vulnerability** button on the repository's *Security* tab. Do not post exploit details in a public issue. You will get a reply as soon as the maintainer can read it; please allow a few days.

## What the tool does to stay safe

- **Isolated Python.** Every hook runs as `python3 -I`, which ignores `PYTHONPATH`, the current directory and user site-packages. A hook fires from whatever project folder you are in, so a planted `json.py` in a repository can never run.
- **Nothing you type or any file content leaves your machine.** The only network use is the daily update check below.
- **Signed automatic updates.** At most once a day, a session-start hook starts a detached background check (the conversation never waits; overall budget 240 s, enforced between every network read). It downloads the latest release record, installer and signature from GitHub over HTTPS (no identifiers, a generic `User-Agent`). The installer runs only if `ssh-keygen -Y verify` accepts its Ed25519 signature from the key shipped in `src/channel/update-channel.json` (namespace `claude-model-profile-release`), **and** the version written in the signed file equals the release tag, is newer than the installed one and keeps the major version. All of this is checked before anything runs, so a compromised GitHub account cannot push unsigned code, and cannot replay an old signed installer to downgrade you or slip in a major version. Non-HTTPS links and redirects, credentials in links, oversized files (1 MiB record, 5 MiB installer, 64 KiB signature, checked while streaming) and a damaged channel file are refused; each refusal names the release and is shown once. `install` and `uninstall` refuse to run while an update is installing, and an update never undoes an uninstall; an installer that overruns its 90 s is stopped with its whole process group. A damaged state file is never overwritten, so `auto-update-off` cannot silently turn back on. Offline or a cut-short download: nothing is shown, it is logged and retried the next day. `CLAUDE_MODEL_PROFILE_UPDATE_API` is accepted only as a local test server (`http://127.0.0.1:<port>`). Off switch: `/model-profile auto-update-off` or `CLAUDE_MODEL_PROFILE_NO_AUTO_UPDATE=1`. The remaining trust is in whoever holds the signing key; switch updates off to review each release yourself. Someone controlling the GitHub account cannot install unsigned code, but could withhold releases or publish an older signed release that is still newer than yours.
- **Your message text is never stored or logged.** Task sizing happens in memory with local keyword matching; only the resulting size is recorded.
- **Settings are changed carefully.** `~/.claude/settings.json` is written atomically, keeps its permissions, and the write is refused if another program changed the file in the meantime. `off` restores what you had and removes only what `on` added. Your own hooks are never touched.
- **Files are tracked by hash.** The installer never overwrites a file you edited, refuses to follow symlinks, and `uninstall` removes only what it installed. Everything it creates is owner-only. It refuses to run through `sudo`.
- **The review gate cannot trap a session.** After two requests it lets Claude finish and tells you what is missing. Any error fails open.
- **Git runs with repository-configured programs disabled** (`core.fsmonitor`, external diff, pager, hooks), so a hostile repository cannot run code through the review gate.
- **The secret scan never prints a secret.** It reports the file, the line and the kind of credential, never the value.
- **Answers are checked in memory only.** After each task the final answer is compared with what actually ran; it is never stored, logged or sent anywhere. Only the end of the transcript is read (the current task, at most 128 MiB), so a huge transcript cannot exhaust memory or time.
- **Conversation memory is minimal.** To size short follow-ups, a per-conversation file keeps the last level, up to three matched keywords, a timestamp and a counter; never message text. It is validated as untrusted input.
- **Helper agents are read-only.** The two untrusted-content readers (`Explore`, `log-reader`) have no shell at all.

## Supported versions

Only the latest release receives fixes.
