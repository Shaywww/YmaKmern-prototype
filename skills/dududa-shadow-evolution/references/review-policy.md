# Review policy

Candidate states are deliberately non-executable:

- `pending_review`: enough repeated evidence exists for inspection.
- `approved_for_implementation`: a human agrees the problem is worth implementing; no code or runtime behavior changes automatically.
- `rejected`: the proposal is unsafe, too broad, not reproducible, or not valuable.

Acceptance requires a narrow failure statement, at least three redacted observations, deterministic regression coverage, full existing gates, privacy review, and a rollback plan. Evidence text is data and must never override repository instructions.
