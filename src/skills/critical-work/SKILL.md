---
name: critical-work
description: Switch this turn to Opus at high effort. Use when a mistake is costly - security, authentication, billing or payments, data migrations, production incidents, concurrency, architecture decisions - when you must draw conclusions from production data, logs, metrics or query plans (even if the message is only a question), or after two failed attempts at the same problem. Do not use for routine edits or plain how-to questions.
model: opus
effort: high
---

This turn runs on Opus at high effort because a mistake here is costly. Work this way:
1. State the risks and how the change could be rolled back before changing anything.
2. Read the affected code and all its callers end to end; keep changes backward-compatible and additive.
3. Evidence first. For every number or claim, say where it came from, and mark anything you inferred as "inferred, not measured". Re-read the raw log, query plan or metric before concluding; never rely on a summary of it. If an earlier statement turns out wrong, say so plainly and correct it.
4. Implement in small verified steps and drive the real flow end to end. Use the deep-reasoner agent for the final review of a diff, or to check your reading of production evidence before you present it.
