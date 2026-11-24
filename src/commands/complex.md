---
description: Do a complex task right the first time at the lowest token cost (triage, plan, verify, review)
argument-hint: <describe the task>
model: sonnet
effort: high
disable-model-invocation: true
---

Task: $ARGUMENTS

Scale the work to the task. Skip any step that does not apply; every extra step costs tokens.

1. Triage in one line: SIMPLE (one place, obvious fix), MODERATE (a few files, clear approach) or COMPLEX (many files, unclear approach, or risky logic such as money, security, concurrency, data migration).
2. SIMPLE: do it, verify, finish. No plan, no review.
3. MODERATE: plan in at most 8 lines, implement, verify.
4. COMPLEX:
   a. Use the Explore agent for every search; read only what you need.
   b. Give the deep-reasoner agent a tight brief (goal, constraints, the files and facts it needs; it cannot see this conversation) and ask for the approach, risks and edge cases. Do not re-read what it already read.
   c. Implement in small steps. After each step run the narrowest check that proves it (test, lint, type check, build).
   d. When finished, run the full checks, then give deep-reasoner the diff and the goal for a correctness review. Fix every finding you can confirm.
5. If the same check fails twice, stop. Report what you tried and the exact failure, then recommend the next step (raise effort one level, then switch to Opus). Do not loop.
6. Done means the checks pass AND, where the work has a user-facing or API flow, you drove that real flow end to end; show the output of both. Final reply: what changed, how it was verified, what is left or risky. No restating the task, no long summary.

Follow the project's standing rules (CLAUDE.md) and the routing standards. Never claim done without evidence. Change nothing outside the task.
