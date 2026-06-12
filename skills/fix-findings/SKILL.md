---
name: fix-findings
description: Triage the latest Codex review findings, fix evidence-backed issues, add regression tests, rerun verification, and prepare the implementation for a fresh independent review.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Edit Write LSP Bash(git *) Bash(python3 *)
disallowed-tools: AskUserQuestion
---

# Triage and fix review findings

1. Read the latest `.ai/autonomous-development/review-NN.codex.json` and prior triage files.
2. Classify every finding as one of:
   - `accepted`;
   - `rejected_with_evidence`;
   - `already_resolved`;
   - `out_of_scope_but_recorded`;
   - `requires_human_decision`.
3. Record the classification and repository evidence in `triage-NN.md` under the run directory.
4. Fix accepted findings with the smallest coherent change.
5. Add a regression test for every correctness, security, compatibility, or reliability defect when practical.
6. Rerun all affected verification checks using the controller.
7. Do not automatically implement low-confidence stylistic suggestions.
8. Do not exceed the configured review-round budget; mark the run blocked when a safe resolution requires external product or operational authority.
