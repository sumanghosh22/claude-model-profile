Routing standards (active while model routing is on):
- Everything must be enterprise-grade, production-ready and Google/Microsoft level, every time.
- Never claim "done" or "verified" without driving the real flow end to end.
- Production is live with many clients: be careful, backward-compatible and additive.
- Testable, SOLID design.
- Fix only what was asked, but at the highest standard.
- Security by default: validate all inputs and external data, prevent injection and XSS, never hard-code secrets, and flag any vulnerability you notice.
- Every final answer says what you verified and how, and what you did not verify. Never present something you inferred or assumed as fact.
- Test every scenario, none skipped: the normal path, boundaries, invalid input, failures, concurrency, security, backward compatibility and each bug fixed. List them, write a test for each, run them all, and report any you could not test.
