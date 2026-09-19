# Quality bar

Home Assistant's [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is a badge only core integrations can carry. A custom integration cannot claim it — but the
rules are still the best available checklist, so this repository holds itself to the
**silver** tier and tracks it here.

Every box is unticked until the behaviour is real and covered by a test or by a documented
by-effect check. The milestone column says when it is due.

## Bronze

| Rule | Done | Milestone |
|---|---|---|
| `config-flow` — set up from the UI, no YAML | [x] | M1 |
| `config-flow-test-coverage` — the flow is fully tested | [x] | M1 |
| `unique-config-entry` — a box cannot be added twice (`node_id`) | [x] | M1 |
| `test-before-configure` — the flow fails early when the broker or the box is wrong | [x] | M1 |
| `test-before-setup` — setup raises `ConfigEntryNotReady` instead of half-working | [x] | M1 |
| `runtime-data` — entry state in `entry.runtime_data`, typed | [x] | M1 |
| `entity-unique-id` — every entity has one (`<node_id>_<key>`) | [x] | M3 |
| `has-entity-name` — entities use `_attr_has_entity_name` | [x] | M3 |
| `entity-event-setup` — subscriptions set up and torn down with the entity | [x] | M3 |
| `common-modules` — `coordinator.py` / `entity.py` where they belong | [x] | M3 |
| `appropriate-polling` — not applicable, the integration is push-only | [x] | M0 |
| `action-setup` — actions registered once, validated by schema | [x] | M3 |
| `docs-actions` — every action documented | [x] | M3 |
| `docs-high-level-description` — what the integration is, in the README | [x] | M1 |
| `docs-installation-instructions` — HACS and manual | [x] | M1 |
| `docs-removal-instructions` — how to remove it cleanly, including `cmd/reset` | [x] | M1 |
| `brands` — icon and logo shipped in `brand/` | [x] | M1 |
| `dependency-transparency` — dependencies pinned and auditable | [x] | M4 bundles the GPL IPK with its exact corresponding source archive; metadata pins commit, sizes and SHA-256 values, and CI reproduces both instead of trusting an opaque download. |

## Silver

| Rule | Done | Milestone |
|---|---|---|
| `config-entry-unloading` — unload and reload without a restart | [x] | M1 |
| `entity-unavailable` — entities follow the box's availability topic | [x] | M3 |
| `log-when-unavailable` — logged once when it goes away, once when it returns | [x] | M3 |
| `action-exceptions` — actions raise `HomeAssistantError` / `ServiceValidationError` | [x] | M3 |
| `parallel-updates` — `PARALLEL_UPDATES = 0`, the integration is push-only | [x] | M3 |
| `reauthentication-flow` — re-enter the SSH password when it stops working | [x] | M4, covered by config-flow and update-action tests; live installer acceptance remains pending. |
| `docs-configuration-parameters` — every option explained | [x] | M3 |
| `docs-installation-parameters` — every setup field explained | [x] | M3 |
| `integration-owner` — a codeowner in the manifest | [x] | M1 |
| `test-coverage` — at least 95 % of the integration's lines | [ ] | M4 |

## Beyond silver

Not targeted for v1, but recorded so the decision is visible. Four gold rules are met anyway,
because they were cheap: `diagnostics` and `devices` landed in M1 and the download grew to
every state topic in M3; `discovery` is inherent — the plugin announces itself over MQTT; and
`reconfiguration-flow` landed with the options in M3. `entity-translations` is met for the
three languages shipped. `dynamic-devices`, `stale-devices`, `repair-issues`, and the platinum
async/typing rules are M5 or later.

Two notes on what the ticks above do **not** claim.

`action-setup` reads literally as "register actions in `async_setup`". These are entity
actions, registered on the media player platform, which is where Home Assistant requires an
entity action to be registered; the rule's intent — a schema on every action, validated before
anything is published, and an action that exists whether or not a box is loaded — is met.

`test-coverage` stays open on purpose. The current 372-test suite covers every platform, every
action, the device triggers, both flows and the guarded installer, including its refusals, its
rollback and the races its durable lock can lose. The local frozen-source run measured 93%
statement coverage on Python 3.14.4 — 92% for `installer.py` — below the 95% target. What is still uncovered is concentrated in the
asyncssh transport itself, which has no receiver to talk to here.

The intermittent failure previously recorded against
`test_aborting_progress_cancels_the_transaction_and_clears_secrets` is fixed. Its cause was in
the test, not the flow: it registered a plain function as a bus listener, so Home Assistant
classified it as an executor job and ran it in a worker thread, where it set an
`asyncio.Event` off the loop and the waiter missed the wakeup. It failed 19 times in 40 runs
before the fix and 0 times in 60 after it.

None of this is evidence that receiver installation or rollback has passed live acceptance. It
has not.

## What CI enforces today

- **hassfest** — manifest, dependencies and translations are structurally valid.
- **HACS action** — the repository meets the store's requirements.
- **ruff** — lint and import order (`E`, `F`, `W`, `I`, `B`, `UP`).
- **pytest** — the test suite, against the Home Assistant release pinned by
  `pytest-homeassistant-custom-component`.
