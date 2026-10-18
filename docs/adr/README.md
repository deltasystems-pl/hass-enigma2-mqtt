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
| [0000](0000-prd.md) | Product requirements (PRD) | accepted, amended by [ADR-0001](0001-m0-decisions.md) and extended by [ADR-0002](0002-scope-after-m0.md) and [ADR-0003](0003-control-feedback-and-household-features.md), §6.3's paragraph on entity ids superseded by [ADR-0007](0007-entity-ids-follow-the-installation-language.md), §6.3's `update` row, §7's "no outbound connection other than the user's broker" and §7's supply-chain bullet superseded in part by [ADR-0008](0008-signed-plugin-index.md), §7's list of connections extended by [ADR-0009](0009-player-picture-and-picons-from-openwebif.md) |
| [0001](0001-m0-decisions.md) | M0 sign-off - the three open questions | accepted |
| [0002](0002-scope-after-m0.md) | Scope added and changed after M0 | accepted, extended by [ADR-0003](0003-control-feedback-and-household-features.md), §6 superseded in part by [ADR-0008](0008-signed-plugin-index.md) |
| [0003](0003-control-feedback-and-household-features.md) | Control feedback and household features - the 0.2.0 and 0.3.0 plan | accepted, extended by [ADR-0004](0004-remote-uninstall.md), §3 superseded in part by [ADR-0005](0005-softcam-restart.md) |
| [0004](0004-remote-uninstall.md) | Remote uninstall behind a box-side permission | accepted, §3 and the first consequence superseded by [ADR-0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md), §4's "does nothing else" superseded in part by [ADR-0008](0008-signed-plugin-index.md) |
| [0005](0005-softcam-restart.md) | Two gates for the softcam button, and a diagnostic that is never taken away | accepted |
| [0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md) | Remote uninstall - the plugin acts, SSH verifies, and the confirmation is a flow step | accepted, §5 superseded in part by [ADR-0008](0008-signed-plugin-index.md) |
| [0007](0007-entity-ids-follow-the-installation-language.md) | Entity ids follow the installation's language, so every translated name is load-bearing | accepted |
| [0008](0008-signed-plugin-index.md) | Plugin versions come from the plugin's signed release index; the update card installs through the receiver when it may and over SSH when it must; older versions only through a confirmed SSH step | accepted 2026-09-26; the install paths of §4-§6 and §8-§9 landed in later changes, and all of it shipped in 0.4.0. Supersedes in part [ADR-0000](0000-prd.md) §6.3's `update` row and §7 (the "No telemetry" and supply-chain bullets), [ADR-0002](0002-scope-after-m0.md) §6, [ADR-0004](0004-remote-uninstall.md) §4, [ADR-0006](0006-remote-uninstall-plugin-acts-ssh-verifies.md) §5, and the "No telemetry" and "Supply chain" paragraphs of [SECURITY.md](../../SECURITY.md). The receiver's half is the plugin's [ADR-0015](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0015-signed-self-update.md) |
| [0009](0009-player-picture-and-picons-from-openwebif.md) | The media player's picture is a choice, and picons are found by asking OpenWebif | accepted 2026-10-12. Extends [ADR-0000](0000-prd.md) §7: HTTP requests to the receiver's own web interface |
| [0010](0010-a-command-carries-an-id-and-a-refusal-is-matched-by-it.md) | A command carries an id where the plugin takes one, and a refusal is matched by it | accepted 2026-10-13. Extends [ADR-0003](0003-control-feedback-and-household-features.md). The receiver's half is the plugin's [ADR-0016](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/blob/main/docs/adr/0016-a-commands-id-comes-in-an-envelope-and-only-a-refusal-says-it-back.md) |
| [0011](0011-stream-facts-addresses-and-a-media-source.md) | Stream facts, addresses and a media source - no camera, the user's own go2rtc, and hiding done here | accepted 2026-10-13. Amended 2026-10-14 (checked against the plugin's stream area; the zap-history selects are `unknown` until a history arrives, like the stream clients). Extends [ADR-0000](0000-prd.md) §7: one request per channel change to a go2rtc address the user typed. The receiver's half is the plugin's ADR-0017, ADR-0018 and ADR-0019 |
| [0012](0012-receiver-menu.md) | The receiver menu - the receiver sends a row id and a value, Home Assistant decides and performs | proposed 2026-10-15, extended by [ADR-0013](0013-menu-creator.md). Amended 2026-10-18 (a toggle carries the state its row showed, section 11). Extends [ADR-0000](0000-prd.md) §7: Home Assistant now publishes the names and states of the entities the user puts into the menu on the broker, and performs services on a message from it. The receiver's half is the plugin's menu ADR |
| [0013](0013-menu-creator.md) | The menu creator - Home Assistant publishes a catalogue, the receiver sends the edited menu back, Home Assistant decides and stores | proposed 2026-10-16. Extends [ADR-0012](0012-receiver-menu.md), and [ADR-0000](0000-prd.md) §7: Home Assistant now publishes the name, area and possible controls of every entity it has while a receiver claims the editor, and stores a menu that arrives over the broker. The receiver's half is the plugin's menu creator ADR |
| [0014](0014-a-list-of-choices.md) | A list of choices - the receiver answers with a row id, once; the action returns it and the bus carries it | proposed 2026-10-18. Extends [ADR-0010](0010-a-command-carries-an-id-and-a-refusal-is-matched-by-it.md): the command's id is also the id an answer is matched by. The receiver's half is the plugin's list ADR |

"Superseded in part by a proposed record" means: the earlier text still describes every released
integration exactly, and the proposed record says where it will stop doing so. It becomes a
supersession when the later record is accepted.
