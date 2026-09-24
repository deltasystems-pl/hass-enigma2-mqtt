# Architecture decision records

Every decision that constrains later work lives here: what was decided, when, and why the
alternatives lost. A decision that turns out wrong is **superseded**, never deleted - the
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
| `extended by ADR-NNNN` | still binding, and a later record adds scope it never mentioned |
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
| [0000](0000-prd.md) | Product requirements (PRD) | accepted, amended by [ADR-0001](0001-m0-decisions.md) and extended by [ADR-0002](0002-scope-after-m0.md) and [ADR-0003](0003-control-feedback-and-household-features.md) |
| [0001](0001-m0-decisions.md) | M0 sign-off - the three open questions | accepted |
| [0002](0002-scope-after-m0.md) | Scope added and changed after M0 | accepted, extended by [ADR-0003](0003-control-feedback-and-household-features.md) |
| [0003](0003-control-feedback-and-household-features.md) | Control feedback and household features - the 0.2.0 and 0.3.0 plan | accepted, extended by [ADR-0004](0004-remote-uninstall.md), §3 superseded in part by [ADR-0005](0005-softcam-restart.md) |
| [0004](0004-remote-uninstall.md) | Remote uninstall behind a box-side permission | accepted, §3 and the first consequence superseded by [ADR-0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md) |
| [0005](0005-softcam-restart.md) | Two gates for the softcam button, and a diagnostic that is never taken away | accepted |
| [0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md) | Remote uninstall - the plugin acts, SSH verifies, and the confirmation is a flow step | accepted |
