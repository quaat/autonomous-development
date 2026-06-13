# Review phase

## Independent Codex review

```bash
controller.py codex --phase review
```

Round 1 is a full review; rounds 2+ automatically use a compact delta schema (resolved findings,
new findings, regressions, affected acceptance criteria). The controller merges each round into a
cumulative finding ledger that the completion gate consults.

Read the generated `review-NN.codex.json`. For each finding, decide a disposition:

- `accepted`; `rejected_with_evidence`; `already_resolved`; `out_of_scope_but_recorded`;
  `requires_human_decision`.

Record your triage as a machine-readable ledger so later rounds do not re-raise rejected findings.
Each entry has a semantic `fingerprint` (`file:symbol:issue`), a `status`, and a `resolution` or
`reason`. To release a high/critical finding you are rejecting (rather than fixing) from the
completion gate, also set `finding_id` to its review id (`F-N`) and use a non-blocking `status`
(`rejected`, `rejected_with_evidence`, `already_resolved`, or `out_of_scope_but_recorded`); a
finding left `open` or marked `requires_human_decision` continues to block completion:

```bash
controller.py triage --file <triage.json>
```

Also write a human-readable `triage-NN.md` with repository evidence for every rejection. Fix
accepted findings, add regression tests, rerun affected checks, and request a fresh review. Stop and
mark the run blocked if the same critical/high issue recurs after a genuine fix attempt or the
review budget is exhausted.

## Adversarial review (high-risk gate)

When `risk.requires_adversarial_review` is set:

```bash
controller.py codex --phase adversarial
```

Address valid required actions, verify again, and rerun the adversarial review when needed.

Completion condition: latest review verdict is `pass` with no unresolved critical/high findings;
when required, latest adversarial verdict is `pass`.
