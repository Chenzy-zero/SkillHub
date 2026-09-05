# Static Quality Review

This is the maintained quality review policy used by the canonical batch-review
entry point. It is informed by upstream review themes but is not an upstream copy.

This score measures the quality visible in the reviewed package. It does not prove that the Skill succeeds at real tasks. Functional quality requires separate test cases and sandboxed with-Skill/without-Skill evaluation.

## Scoring method

Score five dimensions. Choose one anchor per dimension and explain it in `quality_review.dimensions[].reason`.

| Dimension | Weight | STRONG | ACCEPTABLE | WEAK | POOR | ABSENT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `PURPOSE_AND_TRIGGER` | 20 | 20 | 15 | 10 | 5 | 0 |
| `INSTRUCTION_CLARITY` | 25 | 25 | 19 | 13 | 6 | 0 |
| `SCOPE_AND_PERMISSION_FIT` | 15 | 15 | 11 | 8 | 4 | 0 |
| `ROBUSTNESS_AND_BOUNDARIES` | 20 | 20 | 15 | 10 | 5 | 0 |
| `MAINTAINABILITY_AND_VERIFIABILITY` | 20 | 20 | 15 | 10 | 5 | 0 |

Anchor meanings:

- `STRONG`: complete, specific, internally consistent, and supported by the package.
- `ACCEPTABLE`: usable with minor gaps that do not materially change normal behavior.
- `WEAK`: material ambiguity or omissions can cause inconsistent results.
- `POOR`: major gaps make misuse or failure likely.
- `ABSENT`: required information is missing, contradictory, or cannot be assessed.

The quality score is the exact sum of the five dimension scores. Do not add bonuses, hidden deductions, or scanner risk scores.

## Dimension guidance

### PURPOSE_AND_TRIGGER

Evaluate whether the name, description, trigger conditions, inputs, outputs, and non-goals make the intended use clear and avoid triggering on unrelated work.

### INSTRUCTION_CLARITY

Evaluate whether the workflow is complete, ordered where order matters, internally consistent, economical in context, and clear about expected results. Compare documented behavior with scripts and references.

### SCOPE_AND_PERMISSION_FIT

Evaluate whether requested capabilities are the minimum needed, target boundaries are clear, and risky actions require appropriate control. This is a quality score, not a security override; a security finding still controls the security verdict.

### ROBUSTNESS_AND_BOUNDARIES

Evaluate treatment of missing input, malformed files, partial failure, unsupported cases, timeouts, destructive operations, and stopping conditions. Do not reward instructions that hide errors or silently claim success.

### MAINTAINABILITY_AND_VERIFIABILITY

Evaluate organization, progressive disclosure, unnecessary duplication, versioned dependencies or assumptions, testable outputs, examples where they materially clarify behavior, and whether another maintainer can verify the result.

## Quality verdict

- `PASS`: score from 70 through 100.
- `FAIL`: score from 0 through 69.
- `INCOMPLETE`: full static quality review is impossible; score must be `null`.

Record material weaknesses as quality findings with severity `HIGH`, `MEDIUM`, `LOW`, or `INFO`; quality findings do not use `CRITICAL`. Do not duplicate a security finding unless it independently changes a quality dimension. When it affects both, reference the same evidence and explain the quality impact.
