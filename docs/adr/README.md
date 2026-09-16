# Architecture decision records

Every decision that constrains later work lives here: what was decided, when, and why the
alternatives lost. A decision that turns out wrong is **superseded**, never deleted — the
record of a wrong turn is worth as much as the record of a right one.

## Numbering

`NNNN-short-title.md`, four digits, allocated in order and never reused. `0000-prd.md` is the
approved product requirements document; the same file exists in the
[plugin repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge), because the
product spans both.

## Statuses

| Status | Meaning |
|---|---|
| `proposed` | written down, open for discussion, not yet binding |
| `accepted` | binding; the code and the docs follow it |
| `superseded by ADR-NNNN` | replaced; kept for the history, with a link forward |
| `rejected` | considered and turned down, with the reason |

An accepted ADR changes only by a new ADR that supersedes it.

## Template

```markdown
# ADR-NNNN: Title

**Status:** proposed | accepted | superseded by ADR-NNNN | rejected
**Date:** YYYY-MM-DD

## Context
What forced a decision: the constraint, the measurement, the conflict.

## Decision
What we do, in the present tense.

## Consequences
What this costs, what it rules out, and what has to change because of it.
```

## Index

| ADR | Title | Status |
|---|---|---|
| [0000](0000-prd.md) | Product requirements (PRD) | accepted |
| [0001](0001-m0-decisions.md) | M0 sign-off — the three open questions | accepted |
