---
name: critical-work
description: Switch this turn to Opus at high effort. Use only when a mistake is costly - security, authentication, billing or payments, data migrations, production incidents, concurrency, architecture decisions - or after two failed attempts at the same problem. Do not use for questions or routine edits.
model: opus
effort: high
---

This turn runs on Opus at high effort because the work is critical. Work this way:
1. State the risks and how the change could be rolled back before changing anything.
2. Read the affected code and all its callers end to end; keep changes backward-compatible and additive.
3. Implement in small verified steps, then drive the real flow end to end. Use the deep-reasoner agent for the final review of the diff.
